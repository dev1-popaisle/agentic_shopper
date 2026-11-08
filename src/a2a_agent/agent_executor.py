import json
from typing import Any

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

from a2a_agent.discovery import DiscoveryError, discover_store
from a2a_agent.ucp import (
    accept_mock_schedule,
    complete_checkout,
    create_checkout,
    discover_ucp_store,
    negotiate_mock_price,
    confirm_mock_schedule,
    propose_mock_schedule,
    release_mock_schedule,
    request_default_discount,
    search_catalog,
    test_purchase,
)


class ShoppingAgentExecutor(AgentExecutor):
    """Discover UCP merchants, search catalogs, and prepare checkout sessions."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Discovering store capabilities..."),
        )

        try:
            request = _parse_discovery_request(get_message_text(context.message))
            action = request["action"]
            if action == "discover":
                result = await _discover_all(request)
            elif action == "a2a_discover":
                result = await discover_store(
                    store_url=request["store_url"],
                    goal=request["goal"],
                )
            elif action == "ucp_discover":
                result = await discover_ucp_store(request["store_url"])
            elif action == "search":
                result = await search_catalog(
                    store_url=request["store_url"],
                    query=request["query"],
                )
            elif action == "negotiate_price":
                result = await negotiate_mock_price(
                    store_url=request["store_url"],
                    item_id=request["item_id"],
                    quantity=request["quantity"],
                    target_unit_price=request["target_unit_price"],
                    wait_days=request["wait_days"],
                    request_free_delivery=request["request_free_delivery"],
                    destination_postal_code=request["destination_postal_code"],
                )
            elif action == "request_default_discount":
                result = await request_default_discount(
                    store_url=request["store_url"],
                    item_id=request["item_id"],
                    quantity=request["quantity"],
                    destination_postal_code=request["destination_postal_code"],
                )
            elif action == "schedule_order":
                if request["operation"] == "propose":
                    result = await propose_mock_schedule(
                        store_url=request["store_url"],
                        quote_id=request["quote_id"],
                        fulfillment_type=request["fulfillment_type"],
                        preferred_datetime=request.get("preferred_datetime"),
                        delivery_address=request.get("delivery_address"),
                    )
                elif request["operation"] == "accept":
                    result = await accept_mock_schedule(
                        store_url=request["store_url"],
                        schedule_id=request["schedule_id"],
                    )
                elif request["operation"] == "release":
                    result = await release_mock_schedule(
                        store_url=request["store_url"],
                        schedule_id=request["schedule_id"],
                    )
                else:
                    result = await confirm_mock_schedule(
                        store_url=request["store_url"],
                        schedule_id=request["schedule_id"],
                        checkout_id=request["checkout_id"],
                        email=request["email"],
                        phone=request["phone"],
                    )
            elif action == "test_purchase":
                result = await test_purchase(
                    store_url=request["store_url"],
                    items=request["items"],
                )
            elif action == "complete_checkout":
                result = await complete_checkout(
                    store_url=request["store_url"],
                    checkout_id=request["checkout_id"],
                    payment_instrument=request["payment_instrument"],
                )
            else:
                result = await create_checkout(
                    store_url=request["store_url"],
                    items=request["items"],
                )
        except DiscoveryError as exc:
            result = {
                "status": "error",
                "error": {"code": exc.code, "message": str(exc)},
            }

        response = json.dumps(result, indent=2, ensure_ascii=False)
        await updater.add_artifact(
            parts=[new_text_part(text=response, media_type="application/json")]
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Shopping request completed."),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Shopping requests are short-lived and cannot be cancelled yet."""
        raise NotImplementedError("Cancellation is not supported.")


async def _discover_all(request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "discovery_completed"}
    try:
        result["a2a"] = await discover_store(
            store_url=request["store_url"],
            goal=request["goal"],
        )
    except DiscoveryError as exc:
        result["a2a"] = {"status": "error", "error": {"code": exc.code, "message": str(exc)}}
    try:
        result["ucp"] = await discover_ucp_store(request["store_url"])
    except DiscoveryError as exc:
        result["ucp"] = {"status": "error", "error": {"code": exc.code, "message": str(exc)}}
    return result


def _parse_discovery_request(text: str) -> dict[str, Any]:
    if len(text) > 8_192:
        raise DiscoveryError("invalid_request", "Request must be at most 8192 characters.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(
            "invalid_request",
            'Send a JSON object with "store_url" and a supported action: discover, a2a_discover, ucp_discover, search, request_default_discount, negotiate_price, schedule_order, create_checkout, complete_checkout, or test_purchase.',
        ) from exc

    if not isinstance(payload, dict):
        raise DiscoveryError("invalid_request", "Request must be a JSON object.")

    store_url = payload.get("store_url")
    action = payload.get("action", "discover")
    goal = payload.get("goal", "Identify advertised product, price, delivery, and scheduling capabilities.")
    if not isinstance(store_url, str) or not store_url.strip():
        raise DiscoveryError("invalid_request", '"store_url" must be a non-empty URL string.')
    if not isinstance(action, str) or action not in {
        "discover",
        "a2a_discover",
        "ucp_discover",
        "search",
        "request_default_discount",
        "create_checkout",
        "complete_checkout",
        "test_purchase",
        "negotiate_price",
        "schedule_order",
    }:
        raise DiscoveryError("invalid_request", '"action" is not a supported shopping action.')
    if not isinstance(goal, str) or not goal.strip():
        raise DiscoveryError("invalid_request", '"goal" must be a non-empty string.')
    if len(goal) > 2_000:
        raise DiscoveryError("invalid_request", '"goal" must be at most 2000 characters.')

    request: dict[str, Any] = {
        "action": action,
        "store_url": store_url.strip(),
        "goal": goal.strip(),
    }
    if action == "search":
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise DiscoveryError("invalid_request", '"query" must be a non-empty string for search.')
        request["query"] = query.strip()
    if action == "create_checkout":
        items = payload.get("items")
        if not isinstance(items, list):
            raise DiscoveryError("invalid_request", '"items" must be a list for checkout.')
        request["items"] = items
    if action == "complete_checkout":
        checkout_id = payload.get("checkout_id")
        instrument = payload.get("payment_instrument")
        if not isinstance(checkout_id, str) or not isinstance(instrument, dict):
            raise DiscoveryError(
                "invalid_request",
                '"checkout_id" and "payment_instrument" are required for completion.',
            )
        request["checkout_id"] = checkout_id
        request["payment_instrument"] = instrument
    if action == "test_purchase":
        items = payload.get("items")
        if not isinstance(items, list):
            raise DiscoveryError("invalid_request", '"items" must be a list for test purchase.')
        request["items"] = items
    if action == "negotiate_price":
        item_id = payload.get("item_id")
        quantity = payload.get("quantity")
        target_unit_price = payload.get("target_unit_price")
        wait_days = payload.get("wait_days")
        request_free_delivery = payload.get("request_free_delivery")
        destination_postal_code = payload.get("destination_postal_code")
        if (
            not isinstance(item_id, str)
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or isinstance(target_unit_price, bool)
            or not isinstance(target_unit_price, int)
            or isinstance(wait_days, bool)
            or not isinstance(wait_days, int)
            or not isinstance(request_free_delivery, bool)
            or not isinstance(destination_postal_code, str)
        ):
            raise DiscoveryError(
                "invalid_request",
                "A negotiation needs item_id, quantity, target_unit_price, wait_days, request_free_delivery, and destination_postal_code.",
            )
        request.update(
            {
                "item_id": item_id,
                "quantity": quantity,
                "target_unit_price": target_unit_price,
                "wait_days": wait_days,
                "request_free_delivery": request_free_delivery,
                "destination_postal_code": destination_postal_code,
            }
        )
    if action == "request_default_discount":
        item_id = payload.get("item_id")
        quantity = payload.get("quantity")
        destination_postal_code = payload.get("destination_postal_code")
        if (
            not isinstance(item_id, str)
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not isinstance(destination_postal_code, str)
        ):
            raise DiscoveryError(
                "invalid_request",
                "A default discount request needs item_id, quantity, and destination_postal_code.",
            )
        request.update(
            {
                "item_id": item_id,
                "quantity": quantity,
                "destination_postal_code": destination_postal_code,
            }
        )
    if action == "schedule_order":
        operation = payload.get("operation")
        if operation == "propose":
            quote_id = payload.get("quote_id")
            fulfillment_type = payload.get("fulfillment_type")
            if not isinstance(quote_id, str) or not isinstance(fulfillment_type, str):
                raise DiscoveryError(
                    "invalid_request",
                    "A schedule proposal needs quote_id and fulfillment_type.",
                )
            request.update(
                {
                    "operation": operation,
                    "quote_id": quote_id,
                    "fulfillment_type": fulfillment_type,
                }
            )
            if "preferred_datetime" in payload:
                preferred_datetime = payload["preferred_datetime"]
                if not isinstance(preferred_datetime, str):
                    raise DiscoveryError("invalid_request", '"preferred_datetime" must be a string.')
                request["preferred_datetime"] = preferred_datetime
            if "delivery_address" in payload:
                delivery_address = payload["delivery_address"]
                if not isinstance(delivery_address, str):
                    raise DiscoveryError("invalid_request", '"delivery_address" must be a string.')
                request["delivery_address"] = delivery_address
        elif operation == "confirm":
            required = ("schedule_id", "checkout_id", "email", "phone")
            if any(not isinstance(payload.get(key), str) for key in required):
                raise DiscoveryError(
                    "invalid_request",
                    "A schedule confirmation needs schedule_id, checkout_id, email, and phone.",
                )
            request.update({"operation": operation, **{key: payload[key] for key in required}})
        elif isinstance(operation, str) and operation in {"accept", "release"}:
            schedule_id = payload.get("schedule_id")
            if not isinstance(schedule_id, str):
                raise DiscoveryError("invalid_request", "Schedule acceptance needs a schedule_id.")
            request.update({"operation": operation, "schedule_id": schedule_id})
        else:
            raise DiscoveryError("invalid_request", '"operation" must be "propose" or "confirm".')
    return request
