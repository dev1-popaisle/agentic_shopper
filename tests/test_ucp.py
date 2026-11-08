import os
import unittest
from unittest.mock import AsyncMock, patch

from a2a_agent.agent_executor import _parse_discovery_request
from a2a_agent.discovery import DiscoveryError
from a2a_agent.ucp import (
    CATALOG_SEARCH,
    CHECKOUT,
    UCP_VERSION,
    _validate_checkout_items,
    _validate_service_endpoint,
    browse_catalog,
    complete_checkout,
    create_checkout,
    discover_ucp_store,
    search_catalog,
    test_purchase,
)


def _profile(
    *,
    capabilities: tuple[str, ...] = (CATALOG_SEARCH, CHECKOUT),
    endpoint: str = "https://api.shop.example/ucp",
    payment_handlers: tuple[str, ...] = ("com.example.pay",),
) -> dict:
    return {
        "ucp": {
            "version": UCP_VERSION,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": UCP_VERSION,
                        "transport": "rest",
                        "endpoint": endpoint,
                    }
                ]
            },
            "capabilities": {
                capability: [{"version": UCP_VERSION}] for capability in capabilities
            },
            "payment_handlers": {
                name: [{"version": UCP_VERSION}] for name in payment_handlers
            },
        }
    }


class UCPRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_browse_catalog_requests_full_ucp_catalog_page(self) -> None:
        profile = _profile(
            capabilities=(CATALOG_SEARCH,),
            endpoint="http://127.0.0.1:5555/ucp",
        )
        request = AsyncMock(
            side_effect=[
                profile,
                {"products": [{"id": "product-1"}], "pagination": {"has_next_page": False}},
            ]
        )
        with (
            patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}),
            patch("a2a_agent.ucp._request_json", new=request),
        ):
            result = await browse_catalog("http://127.0.0.1:5555")

        self.assertEqual(result["catalog"]["products"][0]["id"], "product-1")
        self.assertEqual(
            request.await_args_list[1].kwargs["payload"],
            {
                "query": "",
                "pagination": {"limit": 50},
            },
        )

    async def test_discovery_reports_advertised_capabilities(self) -> None:
        with patch(
            "a2a_agent.ucp._request_json",
            new=AsyncMock(return_value=_profile()),
        ):
            result = await discover_ucp_store("https://shop.example")

        self.assertEqual(result["status"], "profile_found")
        self.assertTrue(result["catalog_search_available"])
        self.assertTrue(result["checkout_available"])
        self.assertEqual(result["payment_handlers"], ["com.example.pay"])

    async def test_search_uses_advertised_rest_catalog_capability(self) -> None:
        request = AsyncMock(
            side_effect=[
                _profile(capabilities=(CATALOG_SEARCH,)),
                {"ucp": {"version": UCP_VERSION}, "products": [{"id": "variant-1"}]},
            ]
        )
        with patch("a2a_agent.ucp._request_json", new=request):
            result = await search_catalog("https://shop.example", "desk lamp")

        self.assertEqual(result["catalog"]["products"][0]["id"], "variant-1")
        self.assertEqual(request.await_args_list[1].kwargs["payload"], {"query": "desk lamp"})
        self.assertTrue(request.await_args_list[1].args[0].endswith("/catalog/search"))

    async def test_checkout_creates_session_without_completing_purchase(self) -> None:
        request = AsyncMock(
            side_effect=[
                _profile(capabilities=(CHECKOUT,)),
                {
                    "id": "checkout-1",
                    "status": "incomplete",
                    "continue_url": "https://shop.example/checkout",
                },
            ]
        )
        with patch("a2a_agent.ucp._request_json", new=request):
            result = await create_checkout(
                "https://shop.example",
                [{"item_id": "variant-1", "quantity": 2}],
            )

        self.assertFalse(result["purchase_completed"])
        self.assertEqual(
            request.await_args_list[1].kwargs["payload"],
            {"line_items": [{"item": {"id": "variant-1"}, "quantity": 2}]},
        )
        self.assertTrue(request.await_args_list[1].args[0].endswith("/checkout-sessions"))
        self.assertNotIn("/complete", request.await_args_list[1].args[0])

    async def test_operation_requires_matching_advertised_capability(self) -> None:
        with patch(
            "a2a_agent.ucp._request_json",
            new=AsyncMock(return_value=_profile(capabilities=())),
        ):
            with self.assertRaises(DiscoveryError) as raised:
                await search_catalog("https://shop.example", "lamp")

        self.assertEqual(raised.exception.code, "unsupported_capability")

    async def test_complete_checkout_sends_validated_spt_instrument(self) -> None:
        instrument = {
            "id": "instr_1",
            "handler_id": "stripe_payments",
            "type": "card",
            "selected": True,
            "credential": {
                "type": "stripe_payment_token",
                "token": "spt_test_example",
            },
        }
        request = AsyncMock(
            side_effect=[
                _profile(
                    capabilities=(CHECKOUT,),
                    payment_handlers=("com.stripe.payments",),
                ),
                {"id": "chk_123", "status": "completed"},
            ]
        )
        with patch("a2a_agent.ucp._request_json", new=request):
            result = await complete_checkout(
                "https://shop.example",
                "chk_123",
                instrument,
            )

        self.assertTrue(result["purchase_completed"])
        self.assertEqual(result["status"], "checkout_completed")
        self.assertEqual(
            request.await_args_list[1].kwargs["payload"],
            {"payment": {"instruments": [instrument]}},
        )
        self.assertTrue(
            request.await_args_list[1].args[0].endswith(
                "/checkout-sessions/chk_123/complete"
            )
        )

    async def test_test_purchase_is_loopback_only_and_uses_test_token(self) -> None:
        local_profile = _profile(
            capabilities=(CHECKOUT,),
            endpoint="http://127.0.0.1:5555/ucp",
            payment_handlers=("com.stripe.payments",),
        )
        instrument = {
            "id": "instr_test",
            "handler_id": "stripe_payments",
            "type": "card",
            "selected": True,
            "credential": {
                "type": "stripe_payment_token",
                "token": "spt_test_example",
            },
        }
        request = AsyncMock(
            side_effect=[
                local_profile,
                {"id": "chk_123", "status": "incomplete"},
                {"payment": {"instruments": [instrument]}},
                local_profile,
                {"id": "chk_123", "status": "completed"},
            ]
        )
        with (
            patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}),
            patch("a2a_agent.ucp._request_json", new=request),
        ):
            result = await test_purchase(
                "http://127.0.0.1:5555",
                [{"item_id": "variant-1"}],
                negotiation_id="quote-test123",
            )

        self.assertTrue(result["purchase_completed"])
        self.assertEqual(request.await_count, 5)
        self.assertEqual(
            request.await_args_list[1].kwargs["payload"]["negotiation_id"],
            "quote-test123",
        )
        self.assertEqual(
            request.await_args_list[1].kwargs["payload"]["fulfillment_type"],
            "delivery",
        )
        self.assertTrue(
            request.await_args_list[2].args[0].endswith(
                "/test-payment-instruments/chk_123"
            )
        )
        self.assertNotIn("spt_test_example", str(result))

    async def test_test_purchase_rejects_non_loopback_store(self) -> None:
        with (
            patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}),
            self.assertRaises(DiscoveryError) as raised,
        ):
            await test_purchase("https://shop.example", [{"item_id": "variant-1"}])

        self.assertEqual(raised.exception.code, "unsafe_test_purchase")


class UCPValidationTests(unittest.TestCase):
    def test_rejects_unsafe_service_endpoints(self) -> None:
        for endpoint in (
            "http://api.shop.example/ucp",
            "https://127.0.0.1/ucp",
            "https://api.shop.example:8443/ucp",
            "https://user:pass@api.shop.example/ucp",
            "https://api.shop.example/../private",
            "https://api.shop.example/ucp?next=internal",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(DiscoveryError):
                _validate_service_endpoint(endpoint)

    def test_loopback_endpoints_require_explicit_test_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A2A_ALLOW_LOCAL_TEST_STORE", None)
            with self.assertRaises(DiscoveryError):
                _validate_service_endpoint(
                    "http://127.0.0.1:8001/ucp",
                    "http://127.0.0.1:8001",
                )
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            self.assertEqual(
                _validate_service_endpoint(
                    "http://127.0.0.1:8001/ucp",
                    "http://127.0.0.1:8001",
                ),
                "http://127.0.0.1:8001/ucp",
            )
            with self.assertRaises(DiscoveryError):
                _validate_service_endpoint(
                    "http://192.168.1.10:8001/ucp",
                    "http://127.0.0.1:8001",
                )

    def test_validates_checkout_items(self) -> None:
        self.assertEqual(
            _validate_checkout_items([{"item_id": "variant-1"}]),
            [{"item": {"id": "variant-1"}, "quantity": 1}],
        )
        for items in (
            [],
            [{"item_id": "variant-1", "quantity": True}],
            [{"item_id": "", "quantity": 1}],
        ):
            with self.subTest(items=items), self.assertRaises(DiscoveryError):
                _validate_checkout_items(items)

    def test_parses_search_and_checkout_a2a_requests(self) -> None:
        search = _parse_discovery_request(
            '{"store_url":"https://shop.example","action":"search","query":"lamp"}'
        )
        checkout = _parse_discovery_request(
            '{"store_url":"https://shop.example","action":"create_checkout","items":[{"item_id":"v1"}]}'
        )
        test_purchase_request = _parse_discovery_request(
            '{"store_url":"http://127.0.0.1:5555","action":"test_purchase","items":[{"item_id":"v1"}]}'
        )
        self.assertEqual(search["query"], "lamp")
        self.assertEqual(checkout["items"][0]["item_id"], "v1")
        self.assertEqual(test_purchase_request["action"], "test_purchase")


if __name__ == "__main__":
    unittest.main()
