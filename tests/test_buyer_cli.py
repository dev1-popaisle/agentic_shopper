from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from a2a_agent.buyer_cli import (
    _dollars_to_cents,
    _run_purchase,
)


class BuyerCliTests(unittest.TestCase):
    def test_dollars_to_cents_uses_exact_decimal_amounts(self) -> None:
        self.assertEqual(_dollars_to_cents("6.29"), 629)
        self.assertEqual(_dollars_to_cents("1"), 100)
        for invalid in ("0", "-1.00", "1.001", "not money"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _dollars_to_cents(invalid)

class BuyerCliFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmatched_catalog_question_stops_before_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "buyer.json"
            config.write_text(
                json.dumps(
                    {
                        "email": "buyer@example.com",
                        "phone": "415 555 0100",
                        "delivery_address": "1 Example Street, San Francisco, CA 94109",
                    }
                ),
                encoding="utf-8",
            )
            card = SimpleNamespace(name="Mock Merchant", version="1.0")
            catalog_result = {
                "catalog": {
                    "products": [
                        {
                            "title": "Cereal",
                            "variants": [
                                {
                                    "id": "variant-1",
                                    "price": {"amount": 599, "currency": "USD"},
                                    "availability": {"available": True},
                                    "barcodes": [],
                                }
                            ],
                        }
                    ]
                }
            }
            with (
                patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}),
                patch(
                    "a2a_agent.buyer_cli._discover_a2a_card",
                    new=AsyncMock(return_value=card),
                ),
                patch(
                    "a2a_agent.buyer_cli.discover_ucp_store",
                    new=AsyncMock(
                        return_value={
                            "status": "profile_found",
                            "checkout_available": True,
                            "payment_handlers": ["com.stripe.payments"],
                        }
                    ),
                ),
                patch(
                    "a2a_agent.buyer_cli.browse_catalog",
                    new=AsyncMock(return_value=catalog_result),
                ),
                patch(
                    "a2a_agent.buyer_cli.match_catalog_question",
                    new=AsyncMock(return_value=[]),
                ),
                patch("a2a_agent.buyer_cli.Prompt.ask", return_value="Do you sell laptops?"),
                patch("a2a_agent.buyer_cli.negotiate_mock_price", new=AsyncMock()) as negotiate,
                patch("a2a_agent.buyer_cli.test_purchase", new=AsyncMock()) as purchase,
            ):
                await _run_purchase("http://127.0.0.1:5555", config)

            negotiate.assert_not_awaited()
            purchase.assert_not_awaited()

    async def test_accepting_published_discount_uses_offer_without_bargaining(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "buyer.json"
            config.write_text(
                json.dumps(
                    {
                        "email": "buyer@example.com",
                        "phone": "415 555 0100",
                        "delivery_address": "1 Example Street, San Francisco, CA 94109",
                    }
                ),
                encoding="utf-8",
            )
            card = SimpleNamespace(name="Mock Merchant", version="1.0")
            catalog_result = {
                "catalog": {
                    "products": [
                        {
                            "title": "Diet Coke",
                            "variants": [
                                {
                                    "id": "variant-1",
                                    "price": {"amount": 699, "currency": "USD"},
                                    "availability": {"available": True},
                                    "barcodes": [{"type": "UPC", "value": "049000028911"}],
                                }
                            ],
                        }
                    ]
                }
            }
            offer = {
                "status": "offered",
                "quote": {
                    "quote_id": "quote-promo",
                    "item_id": "variant-1",
                    "quantity": 1,
                    "unit_price": 664,
                    "delivery_fee_amount": 1000,
                    "total_amount": 1664,
                    "wait_days": 0,
                    "discount_percent": 5,
                    "free_delivery": False,
                },
            }
            purchase = {"purchase_completed": True, "checkout": {"id": "mock-promo"}}

            with (
                patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}),
                patch("a2a_agent.buyer_cli._discover_a2a_card", new=AsyncMock(return_value=card)),
                patch(
                    "a2a_agent.buyer_cli.discover_ucp_store",
                    new=AsyncMock(
                        return_value={
                            "status": "profile_found",
                            "checkout_available": True,
                            "payment_handlers": ["com.stripe.payments"],
                        }
                    ),
                ),
                patch(
                    "a2a_agent.buyer_cli.browse_catalog",
                    new=AsyncMock(return_value=catalog_result),
                ),
                patch(
                    "a2a_agent.buyer_cli.match_catalog_question",
                    new=AsyncMock(return_value=["variant-1"]),
                ),
                patch(
                    "a2a_agent.buyer_cli.request_default_discount",
                    new=AsyncMock(return_value=offer),
                ) as request_discount,
                patch("a2a_agent.buyer_cli.negotiate_mock_price", new=AsyncMock()) as negotiate,
                patch(
                    "a2a_agent.buyer_cli.test_purchase",
                    new=AsyncMock(return_value=purchase),
                ) as test_purchase,
                patch(
                    "a2a_agent.buyer_cli.propose_mock_schedule",
                    new=AsyncMock(
                        return_value={
                            "status": "auto_assigned",
                            "schedule_id": "schedule-promo",
                            "offered_datetime": f"{date.today() + timedelta(days=1)}T10:00",
                            "timezone": "America/Los_Angeles",
                            "pickup_address": "1 Ferry Building, San Francisco, CA, 94105",
                        }
                    ),
                ),
                patch(
                    "a2a_agent.buyer_cli.confirm_mock_schedule",
                    new=AsyncMock(
                        return_value={
                            "status": "confirmed",
                            "fulfillment_type": "pickup",
                            "address": "1 Ferry Building, San Francisco, CA, 94105",
                            "order_confirmation": {
                                "checkout_id": "mock-promo",
                                "product_title": "Diet Coke",
                                "upc": "049000028911",
                                "quantity": 1,
                                "unit_price": 664,
                                "subtotal_amount": 664,
                                "delivery_fee_amount": 0,
                                "total_amount": 664,
                                "fulfillment_type": "pickup",
                                "appointment_datetime": "2026-11-08T10:00",
                                "timezone": "America/Los_Angeles",
                                "address": "1 Ferry Building, San Francisco, CA, 94105",
                                "wait_days": 0,
                                "free_delivery": True,
                            },
                        }
                    ),
                ) as confirm_schedule,
                patch(
                    "a2a_agent.buyer_cli.Prompt.ask",
                    side_effect=["Diet Coke", "1", "1", "pickup"],
                ),
                patch(
                    "a2a_agent.buyer_cli.typer.confirm",
                    side_effect=[True, True, True, True],
                ),
            ):
                await _run_purchase("http://127.0.0.1:5555", config)

            request_discount.assert_awaited_once_with(
                "http://127.0.0.1:5555",
                "variant-1",
                1,
                destination_postal_code="94109",
            )
            negotiate.assert_not_awaited()
            test_purchase.assert_awaited_once_with(
                "http://127.0.0.1:5555",
                [{"item_id": "variant-1", "quantity": 1}],
                negotiation_id="quote-promo",
                fulfillment_type="pickup",
            )
            confirm_schedule.assert_awaited_once_with(
                "http://127.0.0.1:5555",
                schedule_id="schedule-promo",
                checkout_id="mock-promo",
                email="buyer@example.com",
                phone="415 555 0100",
            )

    async def test_seller_confirmation_is_received_over_a2a_after_payment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "buyer.json"
            config.write_text(
                json.dumps(
                    {
                        "email": "buyer@example.com",
                        "phone": "415 555 0100",
                        "delivery_address": "1 Example Street, San Francisco, CA 94109",
                    }
                ),
                encoding="utf-8",
            )
            card = SimpleNamespace(name="Mock Merchant", version="1.0")
            catalog_result = {
                "catalog": {
                    "products": [
                        {
                            "title": "Mock product",
                            "variants": [
                                {
                                    "id": "variant-1",
                                    "price": {"amount": 699, "currency": "USD"},
                                    "availability": {"available": True},
                                    "barcodes": [{"type": "UPC", "value": "049000028911"}],
                                }
                            ],
                        }
                    ]
                }
            }
            quote = {
                "status": "accepted",
                "quote": {
                    "quote_id": "quote-123",
                    "item_id": "variant-1",
                    "quantity": 3,
                    "unit_price": 629,
                    "delivery_fee_amount": 0,
                    "total_amount": 1887,
                    "wait_days": 7,
                    "free_delivery": True,
                },
            }
            purchase = {
                "purchase_completed": True,
                "checkout": {"id": "mock-123"},
            }
            future_date = (date.today() + timedelta(days=7)).isoformat()
            schedule = {
                "status": "confirmed",
                "order_confirmation": {
                    "checkout_id": "mock-123",
                    "product_title": "Mock product",
                    "upc": "049000028911",
                    "quantity": 3,
                    "unit_price": 629,
                    "subtotal_amount": 1887,
                    "delivery_fee_amount": 0,
                    "total_amount": 1887,
                    "fulfillment_type": "delivery",
                    "appointment_datetime": f"{future_date}T10:00",
                    "timezone": "America/Los_Angeles",
                    "address": "1 Example Street, San Francisco, CA 94109",
                    "wait_days": 7,
                    "free_delivery": True,
                },
            }

            with (
                patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}),
                patch("a2a_agent.buyer_cli._discover_a2a_card", new=AsyncMock(return_value=card)),
                patch(
                    "a2a_agent.buyer_cli.discover_ucp_store",
                    new=AsyncMock(
                        return_value={
                            "status": "profile_found",
                            "checkout_available": True,
                            "payment_handlers": ["com.stripe.payments"],
                        }
                    ),
                ),
                patch(
                    "a2a_agent.buyer_cli.browse_catalog",
                    new=AsyncMock(return_value=catalog_result),
                ),
                patch(
                    "a2a_agent.buyer_cli.match_catalog_question",
                    new=AsyncMock(return_value=["variant-1"]),
                ) as match_question,
                patch(
                    "a2a_agent.buyer_cli.negotiate_mock_price",
                    new=AsyncMock(return_value=quote),
                ) as negotiate,
                patch(
                    "a2a_agent.buyer_cli.test_purchase",
                    new=AsyncMock(return_value=purchase),
                ) as test_purchase,
                patch(
                    "a2a_agent.buyer_cli.propose_mock_schedule",
                    new=AsyncMock(
                        return_value={
                            "status": "available",
                            "schedule_id": "schedule-123",
                            "offered_datetime": f"{future_date}T10:00",
                            "timezone": "America/Los_Angeles",
                        }
                    ),
                ),
                patch(
                    "a2a_agent.buyer_cli.accept_mock_schedule",
                    new=AsyncMock(
                        return_value={
                            "status": "locked",
                            "schedule_id": "schedule-123",
                            "offered_datetime": f"{future_date}T10:00",
                            "timezone": "America/Los_Angeles",
                        }
                    ),
                ) as accept_schedule,
                patch(
                    "a2a_agent.buyer_cli.confirm_mock_schedule",
                    new=AsyncMock(return_value=schedule),
                ) as schedule_delivery,
                patch(
                    "a2a_agent.buyer_cli.Prompt.ask",
                    side_effect=[
                        "Peanut Butter",
                        "1",
                        "3",
                        "7",
                        "5.00",
                        "delivery",
                        f"{future_date} 10:00",
                    ],
                ),
                patch(
                    "a2a_agent.buyer_cli.typer.confirm",
                    side_effect=[True, False, True, True, True, True, True],
                ),
            ):
                await _run_purchase("http://127.0.0.1:5555", config)

            negotiate.assert_awaited_once_with(
                "http://127.0.0.1:5555",
                "variant-1",
                3,
                500,
                wait_days=7,
                request_free_delivery=True,
                destination_postal_code="94109",
            )
            match_question.assert_awaited_once()
            test_purchase.assert_awaited_once_with(
                "http://127.0.0.1:5555",
                [{"item_id": "variant-1", "quantity": 3}],
                negotiation_id="quote-123",
                fulfillment_type="delivery",
            )
            schedule_delivery.assert_awaited_once()
            accept_schedule.assert_awaited_once_with(
                "http://127.0.0.1:5555",
                schedule_id="schedule-123",
            )
            self.assertEqual(schedule_delivery.await_args.kwargs["schedule_id"], "schedule-123")
            self.assertEqual(schedule_delivery.await_args.kwargs["email"], "buyer@example.com")


if __name__ == "__main__":
    unittest.main()
