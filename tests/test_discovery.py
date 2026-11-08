import json
import os
import unittest
from unittest.mock import patch

import httpx

from a2a_agent.discovery import (
    DiscoveryError,
    _summarize_agent_card,
    validate_store_url,
)
from a2a_agent.agent_executor import _parse_discovery_request
from a2a_agent.server import build_app


class StoreUrlTests(unittest.TestCase):
    def test_https_store_url_is_reduced_to_its_origin(self) -> None:
        self.assertEqual(
            validate_store_url("https://Shop.Example/products?q=lamp"),
            "https://shop.example",
        )

    def test_public_ipv6_literal_keeps_brackets(self) -> None:
        self.assertEqual(
            validate_store_url("https://[2606:4700:4700::1111]/item"),
            "https://[2606:4700:4700::1111]",
        )

    def test_rejects_non_https_and_local_targets(self) -> None:
        for url in (
            "http://shop.example",
            "https://localhost",
            "https://127.0.0.1",
            "https://[::1]",
            "https://shop.example:8443",
            "https://user:pass@shop.example",
        ):
            with self.subTest(url=url), self.assertRaises(DiscoveryError):
                validate_store_url(url)

    def test_loopback_store_requires_explicit_test_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A2A_ALLOW_LOCAL_TEST_STORE", None)
            with self.assertRaises(DiscoveryError):
                validate_store_url("http://127.0.0.1:8001")
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            self.assertEqual(
                validate_store_url("http://127.0.0.1:8001"),
                "http://127.0.0.1:8001",
            )
            with self.assertRaises(DiscoveryError):
                validate_store_url("http://192.168.1.10:8001")


class DiscoveryReportTests(unittest.TestCase):
    def test_request_requires_json_with_a_store_url(self) -> None:
        parsed = _parse_discovery_request(
            '{"store_url":"https://shop.example","goal":"Check delivery"}'
        )
        self.assertEqual(parsed["store_url"], "https://shop.example")
        self.assertEqual(parsed["goal"], "Check delivery")
        with self.assertRaises(DiscoveryError):
            _parse_discovery_request("https://shop.example")

    def test_negotiate_price_is_a_supported_action_with_validated_fields(self) -> None:
        parsed = _parse_discovery_request(
            json.dumps(
                {
                    "store_url": "https://shop.example",
                    "action": "negotiate_price",
                    "item_id": "variant-1",
                    "quantity": 3,
                    "target_unit_price": 599,
                    "wait_days": 7,
                    "request_free_delivery": True,
                    "destination_postal_code": "94109",
                }
            )
        )
        self.assertEqual(parsed["action"], "negotiate_price")
        self.assertEqual(parsed["wait_days"], 7)
        self.assertTrue(parsed["request_free_delivery"])

    def test_default_discount_request_is_a_supported_action(self) -> None:
        parsed = _parse_discovery_request(
            json.dumps(
                {
                    "store_url": "https://shop.example",
                    "action": "request_default_discount",
                    "item_id": "variant-1",
                    "quantity": 1,
                    "destination_postal_code": "94109",
                }
            )
        )
        self.assertEqual(parsed["action"], "request_default_discount")
        self.assertEqual(parsed["destination_postal_code"], "94109")

    def test_schedule_proposal_is_a_supported_action(self) -> None:
        parsed = _parse_discovery_request(
            json.dumps(
                {
                    "store_url": "http://127.0.0.1:5555",
                    "action": "schedule_order",
                    "operation": "propose",
                    "quote_id": "quote-123",
                    "fulfillment_type": "delivery",
                    "preferred_datetime": "2026-11-09T10:00",
                    "delivery_address": "1499 California Street, San Francisco, CA 94109",
                }
            )
        )
        self.assertEqual(parsed["action"], "schedule_order")
        self.assertEqual(parsed["operation"], "propose")
        self.assertEqual(parsed["fulfillment_type"], "delivery")

    def test_capabilities_are_reported_as_advertised_not_verified(self) -> None:
        report = _summarize_agent_card(
            "https://shop.example",
            "Check price and delivery",
            {
                "name": "Store Agent",
                "supportedInterfaces": [
                    {
                        "url": "https://agent.shop.example/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "skills": [
                    {
                        "id": "quote",
                        "name": "Request a price quote",
                        "description": "Get product pricing and delivery scheduling.",
                        "tags": ["shopping"],
                    }
                ],
            },
        )
        advertised = {
            entry["capability"]
            for entry in report["a2a"]["advertised_commerce_capabilities"]
        }
        self.assertEqual(
            advertised,
            {"price_quotes", "shipping_and_delivery", "delivery_scheduling"},
        )
        self.assertEqual(report["mcp"]["status"], "not_discovered")
        self.assertFalse(report["safety"]["remote_agent_contacted"])
        self.assertTrue(report["a2a"]["advertised_interface_urls_are_untrusted"])
        self.assertEqual(
            report["a2a"]["interfaces"][0]["advertised_url"],
            "https://agent.shop.example/a2a",
        )
        self.assertIn("not invoked or verified", report["a2a"]["capability_assessment"])


class BuyerAgentCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_card_advertises_price_and_delivery_negotiation(self) -> None:
        app = build_app("http://127.0.0.1:9999")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:9999",
        ) as client:
            response = await client.get("/.well-known/agent-card.json")

        self.assertEqual(response.status_code, 200)
        skills = {skill["id"]: skill for skill in response.json()["skills"]}
        negotiation = skills["negotiate_buyer_price_and_delivery"]
        self.assertIn("maximum unit price", negotiation["description"])
        self.assertIn("delivery wait", negotiation["description"])
        self.assertIn("free delivery", negotiation["description"])
        self.assertIn('"action":"negotiate_price"', negotiation["examples"][0])
        self.assertIn("schedule_delivery_or_pickup", skills)


if __name__ == "__main__":
    unittest.main()
