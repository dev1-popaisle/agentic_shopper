import ipaddress
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest

from a2a_agent.discovery import (
    DiscoveryError,
    PublicHttpsTransport,
    validate_store_url,
)


UCP_PROFILE_PATH = "/.well-known/ucp"
UCP_VERSION = "2026-08-25"
CATALOG_SEARCH = "dev.ucp.shopping.catalog.search"
CHECKOUT = "dev.ucp.shopping.checkout"
STRIPE_HANDLER_ID = "stripe_payments"
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_SECONDS = 8.0
MAX_SEARCH_QUERY_LENGTH = 2_000
MAX_CHECKOUT_ITEMS = 50


@dataclass(frozen=True)
class UCPBusiness:
    store_url: str
    profile_version: str
    endpoint: str | None
    capabilities: frozenset[str]
    payment_handlers: tuple[str, ...]


async def discover_ucp_store(store_url: str) -> dict[str, Any]:
    business = await _load_business(store_url)
    if business is None:
        return {
            "status": "not_found",
            "profile_url": f"{validate_store_url(store_url)}{UCP_PROFILE_PATH}",
        }

    return {
        "status": "profile_found",
        "profile_url": f"{validate_store_url(store_url)}{UCP_PROFILE_PATH}",
        "profile_version": business.profile_version,
        "supported_client_version": UCP_VERSION,
        "rest_endpoint": business.endpoint,
        "capabilities": sorted(business.capabilities),
        "catalog_search_available": (
            business.profile_version == UCP_VERSION
            and business.endpoint is not None
            and CATALOG_SEARCH in business.capabilities
        ),
        "checkout_available": (
            business.profile_version == UCP_VERSION
            and business.endpoint is not None
            and CHECKOUT in business.capabilities
        ),
        "payment_handlers": list(business.payment_handlers),
    }


async def search_catalog(store_url: str, query: str) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise DiscoveryError("invalid_request", '"query" must be a non-empty string.')
    if len(query) > MAX_SEARCH_QUERY_LENGTH:
        raise DiscoveryError("invalid_request", '"query" must be at most 2000 characters.')

    business = await _require_capability(store_url, CATALOG_SEARCH)
    result = await _request_json(
        _service_url(business, "/catalog/search"),
        method="POST",
        payload={"query": query.strip()},
    )
    return {
        "status": "success",
        "store_url": business.store_url,
        "ucp_version": UCP_VERSION,
        "catalog": result,
    }


async def browse_catalog(store_url: str) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    business = await _require_capability(origin, CATALOG_SEARCH)
    result = await _request_json(
        _service_url(business, "/catalog/search"),
        method="POST",
        payload={
            "query": "",
            "pagination": {"limit": 50},
        },
    )
    return {
        "status": "success",
        "store_url": business.store_url,
        "ucp_version": UCP_VERSION,
        "catalog": result,
    }


async def create_checkout(store_url: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_items = _validate_checkout_items(items)
    business = await _require_capability(store_url, CHECKOUT)
    result = await _request_json(
        _service_url(business, "/checkout-sessions"),
        method="POST",
        payload={"line_items": normalized_items},
        headers={"Idempotency-Key": str(uuid4())},
    )
    return {
        "status": "checkout_session_created",
        "store_url": business.store_url,
        "ucp_version": UCP_VERSION,
        "checkout": result,
        "purchase_completed": False,
        "next_step": (
            "Obtain buyer-authorized payment credentials, then submit the selected "
            "instrument to the merchant's UCP complete-checkout operation."
        ),
    }


async def complete_checkout(
    store_url: str,
    checkout_id: str,
    payment_instrument: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(checkout_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", checkout_id):
        raise DiscoveryError("invalid_request", '"checkout_id" is invalid.')
    _validate_stripe_instrument(payment_instrument)
    business = await _require_capability(store_url, CHECKOUT)
    if "com.stripe.payments" not in business.payment_handlers:
        raise DiscoveryError(
            "unsupported_payment_handler",
            "The merchant does not advertise Stripe Payments for UCP checkout.",
        )
    result = await _request_json(
        _service_url(business, f"/checkout-sessions/{checkout_id}/complete"),
        method="POST",
        payload={"payment": {"instruments": [payment_instrument]}},
        headers={"Idempotency-Key": f"complete-{checkout_id}"},
    )
    return {
        "status": "checkout_completed" if result.get("status") == "completed" else "payment_pending",
        "store_url": business.store_url,
        "ucp_version": UCP_VERSION,
        "checkout": result,
        "purchase_completed": result.get("status") == "completed",
    }


async def test_purchase(
    store_url: str,
    items: list[dict[str, Any]],
    *,
    negotiation_id: str | None = None,
    fulfillment_type: str = "delivery",
) -> dict[str, Any]:
    if not isinstance(fulfillment_type, str) or fulfillment_type not in {"delivery", "pickup"}:
        raise DiscoveryError("invalid_request", "Fulfillment must be delivery or pickup.")
    origin = _require_loopback_test_store(store_url)

    normalized_items = _validate_checkout_items(items)
    business = await _require_capability(origin, CHECKOUT)
    if "com.stripe.payments" not in business.payment_handlers:
        raise DiscoveryError(
            "unsupported_payment_handler",
            "The local merchant does not advertise Stripe Payments.",
        )
    checkout_payload: dict[str, Any] = {
        "line_items": normalized_items,
        "fulfillment_type": fulfillment_type,
    }
    if negotiation_id is not None:
        if not re.fullmatch(r"quote-[A-Za-z0-9_-]{1,128}", negotiation_id):
            raise DiscoveryError("invalid_request", "Negotiation ID is invalid.")
        checkout_payload["negotiation_id"] = negotiation_id
    checkout = await _request_json(
        _service_url(business, "/checkout-sessions"),
        method="POST",
        payload=checkout_payload,
        headers={"Idempotency-Key": str(uuid4())},
    )
    checkout_id = checkout.get("id") if checkout else None
    if not isinstance(checkout_id, str):
        raise DiscoveryError("invalid_ucp_response", "Merchant did not return a checkout ID.")

    test_instrument_response = await _request_json(
        _service_url(business, f"/test-payment-instruments/{checkout_id}"),
        method="POST",
        payload={},
    )
    payment = test_instrument_response.get("payment") if test_instrument_response else None
    instruments = payment.get("instruments") if isinstance(payment, dict) else None
    if not isinstance(instruments, list) or len(instruments) != 1:
        raise DiscoveryError(
            "invalid_ucp_response",
            "Merchant did not return a valid Stripe sandbox payment instrument.",
        )
    instrument = instruments[0]
    _validate_stripe_instrument(instrument)
    return await complete_checkout(origin, checkout_id, instrument)


async def negotiate_mock_price(
    store_url: str,
    item_id: str,
    quantity: int,
    target_unit_price: int,
    *,
    wait_days: int,
    request_free_delivery: bool,
    destination_postal_code: str,
    httpx_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    if (
        not isinstance(item_id, str)
        or not item_id.strip()
        or len(item_id) > 256
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity < 1
        or isinstance(target_unit_price, bool)
        or not isinstance(target_unit_price, int)
        or target_unit_price < 1
        or isinstance(wait_days, bool)
        or wait_days not in (0, 7, 14)
        or not isinstance(request_free_delivery, bool)
        or not isinstance(destination_postal_code, str)
        or not re.fullmatch(r"\d{5}", destination_postal_code)
    ):
        raise DiscoveryError("invalid_request", "Price negotiation details are invalid.")
    request_payload = {
        "action": "negotiate_price",
        "item_id": item_id.strip(),
        "quantity": quantity,
        "target_unit_price": target_unit_price,
        "wait_days": wait_days,
        "request_free_delivery": request_free_delivery,
        "destination_postal_code": destination_postal_code,
    }
    response = await _send_a2a_action(origin, request_payload, httpx_client)
    if response.get("status") == "error":
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise DiscoveryError(
            code if isinstance(code, str) else "negotiation_failed",
            message if isinstance(message, str) else "The merchant rejected the negotiation request.",
        )
    quote = response.get("quote")
    if (
        response.get("status") not in {"accepted", "countered"}
        or not isinstance(quote, dict)
        or not isinstance(quote.get("quote_id"), str)
        or quote.get("item_id") != item_id
        or quote.get("quantity") != quantity
        or quote.get("wait_days") != wait_days
        or any(
            isinstance(quote.get(field), bool) or not isinstance(quote.get(field), int)
            for field in ("unit_price", "delivery_fee_amount", "total_amount", "wait_days")
        )
    ):
        raise DiscoveryError("invalid_a2a_response", "Merchant returned an invalid negotiation quote.")
    return response


async def request_default_discount(
    store_url: str,
    item_id: str,
    quantity: int,
    *,
    destination_postal_code: str,
    httpx_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    if (
        not isinstance(item_id, str)
        or not item_id.strip()
        or len(item_id) > 256
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity < 1
        or not isinstance(destination_postal_code, str)
        or not re.fullmatch(r"\d{5}", destination_postal_code)
    ):
        raise DiscoveryError("invalid_request", "Default discount request details are invalid.")

    response = await _send_a2a_action(
        origin,
        {
            "action": "request_default_discount",
            "item_id": item_id.strip(),
            "quantity": quantity,
            "destination_postal_code": destination_postal_code,
        },
        httpx_client,
    )
    if response.get("status") == "no_offer":
        return response
    quote = response.get("quote")
    if (
        response.get("status") != "offered"
        or not isinstance(quote, dict)
        or not isinstance(quote.get("quote_id"), str)
        or quote.get("item_id") != item_id
        or quote.get("quantity") != quantity
        or quote.get("wait_days") != 0
        or isinstance(quote.get("discount_percent"), bool)
        or not isinstance(quote.get("discount_percent"), int)
        or not 1 <= quote["discount_percent"] <= 100
        or any(
            isinstance(quote.get(field), bool) or not isinstance(quote.get(field), int)
            for field in ("unit_price", "delivery_fee_amount", "total_amount")
        )
    ):
        raise DiscoveryError("invalid_a2a_response", "Merchant returned an invalid published discount offer.")
    return response


async def _send_a2a_action(
    origin: str,
    request_payload: dict[str, Any],
    httpx_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    response_text: str | None = None
    try:
        if httpx_client is None:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as owned_httpx_client:
                response_text = await _send_a2a_negotiation(
                    origin,
                    request_payload,
                    owned_httpx_client,
                    close_client=True,
                )
        else:
            response_text = await _send_a2a_negotiation(
                origin,
                request_payload,
                httpx_client,
                close_client=False,
            )
    except DiscoveryError:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise DiscoveryError("a2a_timeout", "Timed out while contacting the merchant agent.") from exc
    except (httpx.RequestError, OSError, ValueError) as exc:
        raise DiscoveryError("a2a_request_failed", "Could not contact the merchant's A2A agent.") from exc

    if not response_text:
        raise DiscoveryError("invalid_a2a_response", "Merchant agent returned no result.")
    try:
        response = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise DiscoveryError("invalid_a2a_response", "Merchant A2A response was not valid JSON.") from exc
    if not isinstance(response, dict):
        raise DiscoveryError("invalid_a2a_response", "Merchant A2A response must be a JSON object.")
    if response.get("status") == "error":
        error = response.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise DiscoveryError(
            code if isinstance(code, str) else "a2a_request_failed",
            message if isinstance(message, str) else "The merchant rejected the A2A request.",
        )
    return response


async def _send_a2a_negotiation(
    origin: str,
    request_payload: dict[str, Any],
    httpx_client: httpx.AsyncClient,
    *,
    close_client: bool,
) -> str:
    resolver = A2ACardResolver(httpx_client=httpx_client, base_url=origin)
    card = await resolver.get_agent_card()
    skills = {skill.id for skill in card.skills}
    required_skill = (
        "schedule_product_delivery"
        if request_payload.get("action") == "schedule_order"
        else "negotiate_product_price"
    )
    if required_skill not in skills:
        raise DiscoveryError(
            "unsupported_a2a_skill",
            (
                "The merchant Agent Card does not advertise delivery scheduling."
                if required_skill == "schedule_product_delivery"
                else "The merchant Agent Card does not advertise price negotiation."
            ),
        )
    client = await create_client(
        agent=card,
        client_config=ClientConfig(
            streaming=True,
            httpx_client=httpx_client,
        ),
    )
    request = SendMessageRequest(
        message=new_text_message(
            json.dumps(request_payload),
            role=Role.ROLE_USER,
        )
    )
    response_text: str | None = None
    try:
        async for event in client.send_message(request):
            artifacts = []
            if event.HasField("task"):
                artifacts.extend(event.task.artifacts)
            if event.HasField("artifact_update"):
                artifacts.append(event.artifact_update.artifact)
            for artifact in artifacts:
                for part in artifact.parts:
                    if part.text:
                        response_text = part.text
    finally:
        if close_client:
            await client.close()
    if response_text is None:
        raise DiscoveryError("invalid_a2a_response", "Merchant agent returned no negotiation result.")
    return response_text


async def propose_mock_schedule(
    store_url: str,
    *,
    quote_id: str,
    fulfillment_type: str,
    preferred_datetime: str | None = None,
    delivery_address: str | None = None,
    httpx_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    if not isinstance(quote_id, str) or not re.fullmatch(r"quote-[A-Za-z0-9_-]{1,128}", quote_id):
        raise DiscoveryError("invalid_request", "Accepted price quote ID is invalid.")
    if not isinstance(fulfillment_type, str) or fulfillment_type not in {"delivery", "pickup"}:
        raise DiscoveryError("invalid_request", "Fulfillment must be delivery or pickup.")
    if fulfillment_type == "delivery":
        if (
            not isinstance(preferred_datetime, str)
            or not isinstance(delivery_address, str)
            or not delivery_address.strip()
            or len(delivery_address) > 500
        ):
            raise DiscoveryError(
                "invalid_request",
                "Delivery scheduling requires a preferred local date/time and delivery address.",
            )
        try:
            parsed_datetime = datetime.fromisoformat(preferred_datetime)
        except ValueError as exc:
            raise DiscoveryError(
                "invalid_request",
                "Preferred date/time must use YYYY-MM-DDTHH:MM local time.",
            ) from exc
        if parsed_datetime.tzinfo is not None:
            raise DiscoveryError("invalid_request", "Preferred date/time must be local and timezone-naive.")
    elif preferred_datetime is not None or delivery_address is not None:
        raise DiscoveryError("invalid_request", "Pickup scheduling is assigned by the seller.")

    response = await _send_a2a_action(
        origin,
        {
            "action": "schedule_order",
            "operation": "propose",
            "quote_id": quote_id,
            "fulfillment_type": fulfillment_type,
            **(
                {
                    "preferred_datetime": preferred_datetime,
                    "delivery_address": delivery_address.strip(),
                }
                if fulfillment_type == "delivery"
                else {}
            ),
        },
        httpx_client,
    )
    schedule_id = response.get("schedule_id")
    schedule_status = response.get("status")
    if (
        not isinstance(schedule_id, str)
        or (
            fulfillment_type == "delivery"
            and schedule_status not in {"available", "countered"}
        )
        or (fulfillment_type == "pickup" and schedule_status != "auto_assigned")
        or not isinstance(response.get("offered_datetime"), str)
        or not isinstance(response.get("timezone"), str)
        or (
            fulfillment_type == "pickup"
            and not isinstance(response.get("pickup_address"), str)
        )
    ):
        raise DiscoveryError("invalid_a2a_response", "Merchant returned an invalid schedule offer.")
    return response


async def confirm_mock_schedule(
    store_url: str,
    *,
    schedule_id: str,
    checkout_id: str,
    email: str,
    phone: str,
    httpx_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    if (
        not isinstance(schedule_id, str)
        or not re.fullmatch(r"schedule-[A-Za-z0-9_-]{1,128}", schedule_id)
        or not isinstance(checkout_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", checkout_id)
        or not isinstance(email, str)
        or not isinstance(phone, str)
    ):
        raise DiscoveryError("invalid_request", "Schedule confirmation details are invalid.")
    response = await _send_a2a_action(
        origin,
        {
            "action": "schedule_order",
            "operation": "confirm",
            "schedule_id": schedule_id,
            "checkout_id": checkout_id,
            "email": email,
            "phone": phone,
        },
        httpx_client,
    )
    if (
        response.get("status") != "confirmed"
        or response.get("schedule_id") != schedule_id
        or response.get("checkout_id") != checkout_id
    ):
        raise DiscoveryError("invalid_a2a_response", "Merchant did not confirm the appointment.")
    confirmation = response.get("order_confirmation")
    required_confirmation_fields = (
        "checkout_id",
        "product_title",
        "upc",
        "quantity",
        "unit_price",
        "subtotal_amount",
        "delivery_fee_amount",
        "total_amount",
        "fulfillment_type",
        "appointment_datetime",
        "timezone",
        "address",
        "wait_days",
        "free_delivery",
    )
    if (
        not isinstance(confirmation, dict)
        or any(key not in confirmation for key in required_confirmation_fields)
        or confirmation.get("checkout_id") != checkout_id
        or not isinstance(confirmation.get("product_title"), str)
        or not isinstance(confirmation.get("upc"), str)
        or not isinstance(confirmation.get("address"), str)
        or not isinstance(confirmation.get("appointment_datetime"), str)
        or not isinstance(confirmation.get("timezone"), str)
        or confirmation.get("fulfillment_type") not in {"delivery", "pickup"}
        or any(
            isinstance(confirmation.get(key), bool)
            or not isinstance(confirmation.get(key), int)
            for key in (
                "quantity",
                "unit_price",
                "subtotal_amount",
                "delivery_fee_amount",
                "total_amount",
                "wait_days",
            )
        )
        or not isinstance(confirmation.get("free_delivery"), bool)
    ):
        raise DiscoveryError(
            "invalid_a2a_response",
            "Merchant confirmed the appointment without a valid order confirmation.",
        )
    return response


async def accept_mock_schedule(
    store_url: str,
    *,
    schedule_id: str,
    httpx_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    if not isinstance(schedule_id, str) or not re.fullmatch(
        r"schedule-[A-Za-z0-9_-]{1,128}", schedule_id
    ):
        raise DiscoveryError("invalid_request", "Schedule ID is invalid.")
    response = await _send_a2a_action(
        origin,
        {
            "action": "schedule_order",
            "operation": "accept",
            "schedule_id": schedule_id,
        },
        httpx_client,
    )
    if (
        response.get("status") not in {"locked", "countered"}
        or response.get("schedule_id") != schedule_id
        or not isinstance(response.get("offered_datetime"), str)
        or not isinstance(response.get("timezone"), str)
    ):
        raise DiscoveryError("invalid_a2a_response", "Merchant did not lock the appointment.")
    return response


async def release_mock_schedule(
    store_url: str,
    *,
    schedule_id: str,
    httpx_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    origin = _require_loopback_test_store(store_url)
    if not isinstance(schedule_id, str) or not re.fullmatch(
        r"schedule-[A-Za-z0-9_-]{1,128}", schedule_id
    ):
        raise DiscoveryError("invalid_request", "Schedule ID is invalid.")
    response = await _send_a2a_action(
        origin,
        {
            "action": "schedule_order",
            "operation": "release",
            "schedule_id": schedule_id,
        },
        httpx_client,
    )
    if response.get("status") != "released" or response.get("schedule_id") != schedule_id:
        raise DiscoveryError("invalid_a2a_response", "Merchant did not release the appointment hold.")
    return response


def _require_loopback_test_store(store_url: str) -> str:
    origin = validate_store_url(store_url)
    parsed = urlsplit(origin)
    try:
        is_loopback = parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = False
    if (
        os.environ.get("A2A_ALLOW_LOCAL_TEST_STORE") != "1"
        or parsed.scheme != "http"
        or not is_loopback
        or parsed.port is None
    ):
        raise DiscoveryError(
            "unsafe_test_purchase",
            "Mock merchant operations require explicit loopback-only test mode.",
        )
    return origin


def _validate_stripe_instrument(instrument: Any) -> None:
    if not isinstance(instrument, dict):
        raise DiscoveryError("invalid_request", "Payment instrument must be an object.")
    credential = instrument.get("credential")
    token = credential.get("token") if isinstance(credential, dict) else None
    if (
        instrument.get("handler_id") != STRIPE_HANDLER_ID
        or instrument.get("type") != "card"
        or instrument.get("selected") is not True
        or not isinstance(credential, dict)
        or credential.get("type") != "stripe_payment_token"
        or not isinstance(token, str)
        or not token.startswith("spt_")
        or len(token) > 512
    ):
        raise DiscoveryError(
            "invalid_request",
            "Payment instrument must contain a selected Stripe card SPT.",
        )


async def _load_business(store_url: str) -> UCPBusiness | None:
    origin = validate_store_url(store_url)
    profile_url = f"{origin}{UCP_PROFILE_PATH}"
    try:
        profile = await _request_json(profile_url, method="GET", not_found_ok=True)
    except DiscoveryError:
        raise
    if profile is None:
        return None

    ucp = profile.get("ucp")
    if not isinstance(ucp, dict) or not isinstance(ucp.get("version"), str):
        raise DiscoveryError("invalid_ucp_profile", "UCP profile is missing its version.")

    services = ucp.get("services")
    service_entries = services.get("dev.ucp.shopping", []) if isinstance(services, dict) else []
    if not isinstance(service_entries, list):
        raise DiscoveryError("invalid_ucp_profile", "UCP shopping services are malformed.")

    endpoint: str | None = None
    for service in service_entries:
        if not isinstance(service, dict) or service.get("transport") != "rest":
            continue
        if ucp["version"] != UCP_VERSION:
            continue
        if service.get("version") != UCP_VERSION:
            continue
        advertised_endpoint = service.get("endpoint")
        if not isinstance(advertised_endpoint, str):
            continue
        endpoint = _validate_service_endpoint(advertised_endpoint, origin)
        break

    raw_capabilities = ucp.get("capabilities")
    if not isinstance(raw_capabilities, dict):
        raw_capabilities = {}

    supported = frozenset(
        name
        for name in (CATALOG_SEARCH, CHECKOUT)
        if ucp["version"] == UCP_VERSION
        and _capability_has_version(raw_capabilities.get(name), UCP_VERSION)
    )
    handlers = ucp.get("payment_handlers")
    payment_handlers = tuple(
        sorted(key for key in handlers if isinstance(key, str))[:50]
    ) if isinstance(handlers, dict) else ()
    return UCPBusiness(
        store_url=origin,
        profile_version=ucp["version"],
        endpoint=endpoint,
        capabilities=supported,
        payment_handlers=payment_handlers,
    )


async def _require_capability(store_url: str, capability: str) -> UCPBusiness:
    business = await _load_business(store_url)
    if business is None:
        raise DiscoveryError("ucp_not_found", "The store does not publish a UCP profile.")
    if business.profile_version != UCP_VERSION:
        raise DiscoveryError(
            "unsupported_ucp_version",
            f"The store publishes UCP {business.profile_version}; this agent supports {UCP_VERSION}.",
        )
    if business.endpoint is None:
        raise DiscoveryError(
            "unsupported_ucp_version",
            f"The store does not advertise a REST endpoint for UCP {UCP_VERSION}.",
        )
    if capability not in business.capabilities:
        raise DiscoveryError(
            "unsupported_capability",
            f"The store does not advertise {capability} for UCP {UCP_VERSION}.",
        )
    return business


def _capability_has_version(value: Any, version: str) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("version") == version
        for entry in value
    )


def _validate_service_endpoint(endpoint: str, store_origin: str | None = None) -> str:
    if len(endpoint) > 2_048:
        raise DiscoveryError("invalid_ucp_profile", "UCP service endpoint is too long.")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryError("invalid_ucp_profile", "UCP service endpoint is malformed.") from exc

    decoded_path = unquote(parsed.path)
    if (
        store_origin is not None
        and urlsplit(store_origin).scheme == "http"
        and os.environ.get("A2A_ALLOW_LOCAL_TEST_STORE") == "1"
    ):
        origin_parts = urlsplit(store_origin)
        try:
            endpoint_ip = ipaddress.ip_address(parsed.hostname or "")
            origin_ip = ipaddress.ip_address(origin_parts.hostname or "")
        except ValueError as exc:
            raise DiscoveryError(
                "unsafe_url",
                "Local UCP test endpoints must use literal loopback IP addresses.",
            ) from exc
        if (
            parsed.scheme.lower() != "http"
            or not endpoint_ip.is_loopback
            or endpoint_ip != origin_ip
            or parsed.port != origin_parts.port
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in decoded_path
            or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        ):
            raise DiscoveryError(
                "unsafe_url",
                "Local UCP endpoints must use the same loopback host and port as the test store.",
            )
        authority = (
            f"[{parsed.hostname}]"
            if endpoint_ip.version == 6
            else parsed.hostname
        )
        return f"http://{authority}:{parsed.port}{parsed.path.rstrip('/')}"

    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or "\\" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        raise DiscoveryError(
            "invalid_ucp_profile",
            "UCP service endpoint must be an HTTPS URL on the standard port without credentials, query, or fragment.",
        )

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise DiscoveryError("unsafe_url", "UCP service endpoint cannot use a local hostname.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise DiscoveryError("invalid_ucp_profile", "UCP endpoint hostname is invalid.") from exc
        authority = host
    else:
        if not address.is_global:
            raise DiscoveryError("unsafe_url", "UCP service endpoint cannot use a non-public IP.")
        authority = f"[{host}]" if address.version == 6 else host
    return f"https://{authority}{parsed.path.rstrip('/')}"


def _service_url(business: UCPBusiness, path: str) -> str:
    if business.endpoint is None:
        raise DiscoveryError("unsupported_capability", "The store has no UCP REST endpoint.")
    return f"{business.endpoint}{path}"


async def _request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    not_found_ok: bool = False,
) -> dict[str, Any] | None:
    parsed = urlsplit(url)
    is_local = (
        os.environ.get("A2A_ALLOW_LOCAL_TEST_STORE") == "1"
        and parsed.scheme == "http"
        and parsed.hostname is not None
        and parsed.port is not None
    )
    if is_local:
        try:
            is_local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_local = False
    if not parsed.hostname or (parsed.scheme != "https" and not is_local):
        raise DiscoveryError("unsafe_url", "UCP requests require HTTPS, except for explicit loopback testing.")

    transport = PublicHttpsTransport(allow_loopback=is_local)
    try:
        await transport.network_backend.resolve_public(
            parsed.hostname,
            parsed.port or 443,
        )
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        profile_url = os.environ.get("UCP_AGENT_PROFILE_URL")
        if profile_url:
            _validate_profile_reference(profile_url)
            request_headers["UCP-Agent"] = f'profile="{profile_url}"'
        if headers:
            request_headers.update(headers)

        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            async with client.stream(
                method,
                url,
                json=payload,
                headers=request_headers,
            ) as response:
                if not_found_ok and response.status_code == 404:
                    return None
                if response.status_code not in (200, 201):
                    raise DiscoveryError(
                        "ucp_request_failed",
                        f"UCP endpoint returned HTTP {response.status_code}.",
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise DiscoveryError(
                        "invalid_ucp_response",
                        "UCP endpoint did not return a JSON content type.",
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise DiscoveryError(
                            "invalid_ucp_response",
                            "UCP response exceeded the 1 MB size limit.",
                        )
    except DiscoveryError:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise DiscoveryError("ucp_timeout", "Timed out while contacting the UCP endpoint.") from exc
    except httpx.RequestError as exc:
        raise DiscoveryError("ucp_request_failed", "Could not contact the UCP endpoint.") from exc
    except OSError as exc:
        raise DiscoveryError("ucp_request_failed", "Could not resolve or connect to the UCP endpoint.") from exc

    try:
        result = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DiscoveryError("invalid_ucp_response", "UCP response was not valid JSON.") from exc
    if not isinstance(result, dict):
        raise DiscoveryError("invalid_ucp_response", "UCP response must be a JSON object.")
    return result


def _validate_profile_reference(profile_url: str) -> None:
    try:
        parsed = urlsplit(profile_url)
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryError("invalid_configuration", "UCP_AGENT_PROFILE_URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or any(character in profile_url for character in ('"', "\\", "\r", "\n"))
    ):
        raise DiscoveryError(
            "invalid_configuration",
            "UCP_AGENT_PROFILE_URL must be an HTTPS URL on the standard port without credentials, query, or fragment.",
        )


def _validate_checkout_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CHECKOUT_ITEMS:
        raise DiscoveryError(
            "invalid_request",
            f'"items" must contain between 1 and {MAX_CHECKOUT_ITEMS} items.',
        )
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise DiscoveryError("invalid_request", "Each checkout item must be an object.")
        item_id = item.get("item_id")
        quantity = item.get("quantity", 1)
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 256:
            raise DiscoveryError(
                "invalid_request",
                'Each checkout item needs a non-empty "item_id" of at most 256 characters.',
            )
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise DiscoveryError(
                "invalid_request",
                'Each checkout item "quantity" must be a positive integer.',
            )
        normalized.append(
            {"item": {"id": item_id.strip()}, "quantity": quantity}
        )
    return normalized
