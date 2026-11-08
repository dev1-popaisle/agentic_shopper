import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import duckdb
import httpx

from a2a_agent.mock_store import DuckDBCatalog, UCP_VERSION, build_app
from a2a_agent.discovery import DiscoveryError
from a2a_agent.seller_policy import SellerNegotiationPolicy
from a2a_agent.stripe_payments import StripePaymentError, StripeTestGateway
from a2a_agent.seller_schedule import SellerScheduleConfig
from a2a_agent.ucp import (
    accept_mock_schedule,
    confirm_mock_schedule,
    negotiate_mock_price,
    propose_mock_schedule,
    release_mock_schedule,
    request_default_discount,
)


class MockMerchantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.smtp_environment = patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "seller",
                "SMTP_PASSWORD": "test-password",
                "SMTP_FROM_EMAIL": "orders@example.com",
            },
        )
        self.smtp_environment.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.catalog = DuckDBCatalog(Path(self.temp_dir.name) / "catalog.duckdb")
        self.seller_policy = SellerNegotiationPolicy.load()
        self.app = build_app(
            catalog=self.catalog,
            store_url="http://127.0.0.1:5555",
            seller_policy=self.seller_policy,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://127.0.0.1:5555",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.catalog.close()
        self.temp_dir.cleanup()
        self.smtp_environment.stop()

    async def test_duckdb_seed_has_real_upcs_and_attributed_local_images(self) -> None:
        products = self.catalog.search()
        upcs = [product["upc"] for product in products]

        self.assertEqual(len(products), 10)
        self.assertEqual(len(set(upcs)), 10)
        for product in products:
            upc = product["upc"]
            self.assertEqual(len(upc), 12)
            self.assertTrue(upc.isdigit())
            weighted_sum = sum(
                int(digit) * (3 if index % 2 == 0 else 1)
                for index, digit in enumerate(upc[:11])
            )
            self.assertEqual((10 - weighted_sum % 10) % 10, int(upc[-1]))
            self.assertTrue((Path(__file__).parents[1] / product["image_path"]).is_file())
            self.assertEqual(product["image_license"], "CC BY-SA 3.0")
            self.assertEqual(product["image_attribution"], "Open Food Facts contributors")
            self.assertTrue(product["product_source_url"].startswith(
                "https://world.openfoodfacts.org/product/"
            ))
            self.assertTrue(product["image_source_url"].startswith(
                "https://images.openfoodfacts.org/"
            ))

    async def test_profile_advertises_catalog_and_checkout(self) -> None:
        response = await self.client.get("/.well-known/ucp")
        profile = response.json()["ucp"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(profile["version"], UCP_VERSION)
        self.assertEqual(
            profile["services"]["dev.ucp.shopping"][0]["endpoint"],
            "http://127.0.0.1:5555/ucp",
        )
        self.assertIn("dev.ucp.shopping.catalog.search", profile["capabilities"])
        self.assertIn("dev.ucp.shopping.checkout", profile["capabilities"])

    async def test_seller_agent_card_advertises_negotiation_without_policy_details(self) -> None:
        card = (await self.client.get("/.well-known/agent-card.json")).json()
        skills = {skill["id"]: skill for skill in card["skills"]}

        self.assertIn("negotiate_product_price", skills)
        self.assertIn("schedule_product_delivery", skills)
        description = skills["negotiate_product_price"]["description"]
        self.assertIn("maximum unit price", description)
        self.assertNotIn("25%", description)
        self.assertNotIn("daily sales", description)
        self.assertNotIn("discount strategy", description)

    async def test_storefront_search_and_local_product_images(self) -> None:
        storefront = await self.client.get("/")
        self.assertEqual(storefront.status_code, 200)
        self.assertIn("A2A Mock Market", storefront.text)
        self.assertIn("CC BY-SA 3.0", storefront.text)
        self.assertEqual(storefront.text.count('class="product-card"'), 10)

        search = await self.client.get("/", params={"q": "cheerios"})
        self.assertIn("Honey Nut Cheerios", search.text)
        self.assertNotIn("Diet Coke", search.text)

        image = await self.client.get("/images/016000123151")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/jpeg")
        self.assertTrue(image.content.startswith(b"\xff\xd8"))
        self.assertEqual((await self.client.get("/images/000000000000")).status_code, 404)

    async def test_product_http_api_and_ucp_search_query_duckdb(self) -> None:
        response = await self.client.get("/api/products", params={"query": "peanut butter"})
        products = response.json()["products"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(products), 4)

        by_upc = await self.client.get(f"/api/products/{products[0]['upc']}")
        self.assertEqual(by_upc.json()["upc"], products[0]["upc"])

        search = await self.client.post(
            "/ucp/catalog/search",
            json={"query": "Cheerios"},
        )
        self.assertEqual(search.status_code, 200)
        body = search.json()
        self.assertEqual(body["ucp"]["version"], UCP_VERSION)
        self.assertEqual(body["products"][0]["title"], "Honey Nut Cheerios Cereal")
        variant = body["products"][0]["variants"][0]
        self.assertEqual(variant["barcodes"][0], {"type": "UPC", "value": variant["id"]})
        self.assertEqual(
            body["products"][0]["metadata"]["image_url"],
            "http://127.0.0.1:5555/images/016000123151",
        )
        self.assertEqual(body["products"][0]["metadata"]["image_license"], "CC BY-SA 3.0")

    async def test_ucp_catalog_search_supports_browsing_the_complete_catalog(self) -> None:
        response = await self.client.post(
            "/ucp/catalog/search",
            json={"query": "", "pagination": {"limit": 50}},
        )

        self.assertEqual(response.status_code, 200)
        products = response.json()["products"]
        self.assertEqual(len(products), 10)
        self.assertEqual(
            {product["variants"][0]["barcodes"][0]["value"] for product in products},
            {product["upc"] for product in self.catalog.search()},
        )

    async def test_checkout_is_test_only_and_does_not_make_an_order(self) -> None:
        product = self.catalog.search()[0]
        response = await self.client.post(
            "/ucp/checkout-sessions",
            json={
                "line_items": [
                    {"item": {"id": product["variant_id"]}, "quantity": 2}
                ]
            },
        )
        checkout = response.json()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(checkout["status"], "incomplete")
        self.assertEqual(checkout["line_items"][0]["item"]["id"], product["upc"])
        self.assertEqual(len(checkout["links"]), 2)
        self.assertIn("No payment or order", checkout["messages"][0]["content"])
        self.assertEqual(
            (await self.client.get(checkout["continue_url"].replace("http://127.0.0.1:5555", ""))).status_code,
            200,
        )

    async def test_a2a_price_negotiation_quote_is_applied_once_to_checkout(self) -> None:
        product = self.catalog.search()[0]
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            response = await negotiate_mock_price(
                "http://127.0.0.1:5555",
                product["variant_id"],
                2,
                1,
                wait_days=0,
                request_free_delivery=False,
                destination_postal_code="94109",
                httpx_client=self.client,
            )
        quote = response["quote"]
        discount = self.seller_policy.demand_discount_percent(product["upc"])
        floor_price = (product["price_amount"] * (100 - discount) + 99) // 100
        self.assertEqual(response["status"], "countered")
        self.assertEqual(quote["unit_price"], floor_price)
        self.assertEqual(quote["delivery_fee_amount"], 1000)
        self.assertEqual(quote["total_amount"], floor_price * 2 + 1000)
        self.assertNotIn("daily_sales", str(response))
        self.assertNotIn("discount_percent", str(response))

        created = await self.client.post(
            "/ucp/checkout-sessions",
            json={
                "line_items": [{"item": {"id": product["variant_id"]}, "quantity": 2}],
                "negotiation_id": quote["quote_id"],
            },
        )
        checkout = created.json()
        self.assertEqual(created.status_code, 201)
        self.assertEqual(checkout["line_items"][0]["item"]["price"], floor_price)
        self.assertEqual(checkout["totals"][-1]["amount"], floor_price * 2 + 1000)
        self.assertEqual(checkout["totals"][1], {"type": "delivery", "amount": 1000})

        repeated = await self.client.post(
            "/ucp/checkout-sessions",
            json={
                "line_items": [{"item": {"id": product["variant_id"]}, "quantity": 2}],
                "negotiation_id": quote["quote_id"],
            },
        )
        self.assertEqual(repeated.status_code, 409)

    async def test_a2a_negotiation_wait_and_three_item_city_delivery_terms(self) -> None:
        product = self.catalog.get_by_upc("049000028911")
        self.assertIsNotNone(product)
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            response = await negotiate_mock_price(
                "http://127.0.0.1:5555",
                product["variant_id"],
                3,
                1,
                wait_days=14,
                request_free_delivery=True,
                destination_postal_code="94109",
                httpx_client=self.client,
            )

        quote = response["quote"]
        discount = (
            self.seller_policy.demand_discount_percent(product["upc"])
            + self.seller_policy.wait_discount_percent(14)
        )
        expected_price = (product["price_amount"] * (100 - discount) + 99) // 100
        self.assertEqual(response["status"], "countered")
        self.assertTrue(quote["free_delivery"])
        self.assertEqual(quote["delivery_fee_amount"], 0)
        self.assertEqual(quote["wait_days"], 14)
        self.assertEqual(quote["unit_price"], expected_price)
        self.assertEqual(quote["total_amount"], expected_price * 3)

    async def test_a2a_default_discount_is_only_offered_for_configured_upc_and_applied(self) -> None:
        product = self.catalog.get_by_upc("049000028911")
        self.assertIsNotNone(product)
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            offer_response = await request_default_discount(
                "http://127.0.0.1:5555",
                product["variant_id"],
                1,
                destination_postal_code="94109",
                httpx_client=self.client,
            )
            no_offer_response = await request_default_discount(
                "http://127.0.0.1:5555",
                self.catalog.get_by_upc("049000028904")["variant_id"],
                1,
                destination_postal_code="94109",
                httpx_client=self.client,
            )

        quote = offer_response["quote"]
        self.assertEqual(offer_response["status"], "offered")
        self.assertEqual(quote["discount_percent"], 5)
        self.assertEqual(quote["unit_price"], 664)
        self.assertEqual(quote["delivery_fee_amount"], 1000)
        self.assertEqual(quote["total_amount"], 1664)
        self.assertEqual(no_offer_response["status"], "no_offer")
        self.assertNotIn("quote", no_offer_response)

        checkout_response = await self.client.post(
            "/ucp/checkout-sessions",
            json={
                "line_items": [{"item": {"id": product["variant_id"]}, "quantity": 1}],
                "negotiation_id": quote["quote_id"],
            },
        )
        checkout = checkout_response.json()
        self.assertEqual(checkout_response.status_code, 201)
        self.assertEqual(checkout["line_items"][0]["item"]["price"], 664)
        self.assertEqual(checkout["totals"][-1]["amount"], 1664)

    async def test_http_negotiation_route_is_not_available(self) -> None:
        response = await self.client.post(
            "/ucp/mock/negotiations",
            json={"item_id": "049000028911"},
        )
        self.assertEqual(response.status_code, 404)

    async def test_delivery_schedule_is_only_available_through_a2a(self) -> None:
        response = await self.client.post(
            "/api/orders/mock-123/schedule",
            json={"delivery_date": "2026-11-09"},
        )
        self.assertEqual(response.status_code, 404)

    async def test_a2a_delivery_schedule_counteroffers_and_confirms_only_after_payment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_test_example",
                "STRIPE_BUSINESS_PROFILE_ID": "profile_test_example",
                "STRIPE_ENABLE_TEST_HELPER": "1",
            },
        ):
            app = build_app(catalog=self.catalog)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:5555",
        )
        try:
            with (
                patch.object(
                    StripeTestGateway,
                    "create_test_spt",
                    new=AsyncMock(return_value="spt_test_example"),
                ),
                patch.object(
                    StripeTestGateway,
                    "charge_spt",
                    new=AsyncMock(
                        return_value={"id": "pi_test_example", "status": "succeeded"}
                    ),
                ),
            ):
                product = self.catalog.get_by_upc("049000028911")
                with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
                    price_offer = await negotiate_mock_price(
                        "http://127.0.0.1:5555",
                        product["variant_id"],
                        1,
                        1,
                        wait_days=0,
                        request_free_delivery=False,
                        destination_postal_code="94109",
                        httpx_client=client,
                    )
                    quote_id = price_offer["quote"]["quote_id"]
                    schedule_config = SellerScheduleConfig.load()
                    first_slot = schedule_config.next_slot(
                        schedule_config.local_today() + timedelta(days=1)
                    )
                    self.assertIsNotNone(first_slot)
                    available = await propose_mock_schedule(
                        "http://127.0.0.1:5555",
                        quote_id=quote_id,
                        fulfillment_type="delivery",
                        preferred_datetime=first_slot.isoformat(timespec="minutes"),
                        delivery_address="1 Example Street",
                        httpx_client=client,
                    )
                    self.assertEqual(available["status"], "available")
                    self.assertEqual(available["offered_datetime"], first_slot.isoformat(timespec="minutes"))

                    earliest = schedule_config.local_today() + timedelta(days=1)
                    sunday = earliest + timedelta(days=(6 - earliest.weekday()) % 7)
                    countered = await propose_mock_schedule(
                        "http://127.0.0.1:5555",
                        quote_id=quote_id,
                        fulfillment_type="delivery",
                        preferred_datetime=f"{sunday.isoformat()}T12:00",
                        delivery_address="1 Example Street",
                        httpx_client=client,
                    )
                    self.assertEqual(countered["status"], "countered")
                    self.assertNotEqual(countered["offered_datetime"], f"{sunday.isoformat()}T12:00")

                    checkout = (
                        await client.post(
                            "/ucp/checkout-sessions",
                            json={
                                "line_items": [{"item": {"id": product["variant_id"]}}],
                                "negotiation_id": quote_id,
                            },
                        )
                    ).json()
                    with self.assertRaisesRegex(
                        DiscoveryError,
                        "buyer must accept the appointment",
                    ):
                        await confirm_mock_schedule(
                            "http://127.0.0.1:5555",
                            schedule_id=available["schedule_id"],
                            checkout_id=checkout["id"],
                            email="buyer@example.com",
                            phone="415 555 0100",
                            httpx_client=client,
                        )
                    locked = await accept_mock_schedule(
                        "http://127.0.0.1:5555",
                        schedule_id=available["schedule_id"],
                        httpx_client=client,
                    )
                    self.assertEqual(locked["status"], "locked")
                    with self.assertRaisesRegex(
                        DiscoveryError,
                        "matching checkout must be paid",
                    ):
                        await confirm_mock_schedule(
                            "http://127.0.0.1:5555",
                            schedule_id=available["schedule_id"],
                            checkout_id=checkout["id"],
                            email="buyer@example.com",
                            phone="415 555 0100",
                            httpx_client=client,
                        )

                    instrument = (
                        await client.post(
                            f"/ucp/test-payment-instruments/{checkout['id']}",
                            json={},
                        )
                    ).json()["payment"]["instruments"][0]
                    completed = await client.post(
                        f"/ucp/checkout-sessions/{checkout['id']}/complete",
                        json={"payment": {"instruments": [instrument]}},
                    )
                    self.assertEqual(completed.json()["status"], "completed")

                    with patch(
                        "a2a_agent.mock_store.send_order_confirmation_email",
                        side_effect=OSError("SMTP unavailable"),
                    ):
                        with self.assertRaisesRegex(
                            DiscoveryError,
                            "could not send the order confirmation email",
                        ):
                            await confirm_mock_schedule(
                                "http://127.0.0.1:5555",
                                schedule_id=available["schedule_id"],
                                checkout_id=checkout["id"],
                                email="buyer@example.com",
                                phone="415 555 0100",
                                httpx_client=client,
                            )

                    with patch(
                        "a2a_agent.mock_store.send_order_confirmation_email"
                    ) as send_email:
                        scheduled = await confirm_mock_schedule(
                            "http://127.0.0.1:5555",
                            schedule_id=available["schedule_id"],
                            checkout_id=checkout["id"],
                            email="buyer@example.com",
                            phone="415 555 0100",
                            httpx_client=client,
                        )
                    self.assertEqual(scheduled["status"], "confirmed")
                    self.assertEqual(scheduled["fulfillment_type"], "delivery")
                    self.assertIn("no carrier", scheduled["notice"])
                    confirmation = scheduled["order_confirmation"]
                    self.assertEqual(confirmation["product_title"], product["title"])
                    self.assertEqual(confirmation["upc"], product["upc"])
                    self.assertEqual(confirmation["quantity"], 1)
                    self.assertEqual(confirmation["appointment_datetime"], available["offered_datetime"])
                    self.assertEqual(confirmation["address"], "1 Example Street")
                    self.assertEqual(send_email.call_args.kwargs["buyer_email"], "buyer@example.com")
                    self.assertEqual(
                        send_email.call_args.kwargs["order_confirmation"],
                        confirmation,
                    )
        finally:
            await client.aclose()

    async def test_a2a_pickup_is_auto_assigned_at_seller_address(self) -> None:
        product = self.catalog.get_by_upc("049000028911")
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            price_offer = await negotiate_mock_price(
                "http://127.0.0.1:5555",
                product["variant_id"],
                1,
                1,
                wait_days=0,
                request_free_delivery=False,
                destination_postal_code="94109",
                httpx_client=self.client,
            )
            pickup = await propose_mock_schedule(
                "http://127.0.0.1:5555",
                quote_id=price_offer["quote"]["quote_id"],
                fulfillment_type="pickup",
                httpx_client=self.client,
            )
            checkout_response = await self.client.post(
                "/ucp/checkout-sessions",
                json={
                    "line_items": [{"item": {"id": product["variant_id"]}, "quantity": 1}],
                    "negotiation_id": price_offer["quote"]["quote_id"],
                    "fulfillment_type": "pickup",
                },
            )
            checkout = checkout_response.json()

        self.assertEqual(pickup["status"], "auto_assigned")
        self.assertEqual(
            pickup["pickup_address"],
            "1 Ferry Building, San Francisco, CA, 94105",
        )
        self.assertTrue(pickup["offered_datetime"])
        self.assertEqual(checkout_response.status_code, 201)
        self.assertEqual(checkout["totals"][1], {"type": "delivery", "amount": 0})
        self.assertEqual(checkout["totals"][-1]["amount"], price_offer["quote"]["unit_price"])

    async def test_scheduling_is_blocked_when_seller_email_is_not_configured(self) -> None:
        product = self.catalog.get_by_upc("049000028911")
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            price_offer = await negotiate_mock_price(
                "http://127.0.0.1:5555",
                product["variant_id"],
                1,
                1,
                wait_days=0,
                request_free_delivery=False,
                destination_postal_code="94109",
                httpx_client=self.client,
            )
        with patch.dict(
            os.environ,
            {"A2A_ALLOW_LOCAL_TEST_STORE": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(DiscoveryError, "configure SMTP"):
                await propose_mock_schedule(
                    "http://127.0.0.1:5555",
                    quote_id=price_offer["quote"]["quote_id"],
                    fulfillment_type="pickup",
                    httpx_client=self.client,
                )

    async def test_a2a_schedule_hold_can_be_released_when_buyer_cancels(self) -> None:
        product = self.catalog.get_by_upc("049000028911")
        schedule_config = SellerScheduleConfig.load()
        preferred = schedule_config.next_slot(
            schedule_config.local_today() + timedelta(days=1)
        )
        with patch.dict(os.environ, {"A2A_ALLOW_LOCAL_TEST_STORE": "1"}):
            price_offer = await negotiate_mock_price(
                "http://127.0.0.1:5555",
                product["variant_id"],
                1,
                1,
                wait_days=0,
                request_free_delivery=False,
                destination_postal_code="94109",
                httpx_client=self.client,
            )
            schedule = await propose_mock_schedule(
                "http://127.0.0.1:5555",
                quote_id=price_offer["quote"]["quote_id"],
                fulfillment_type="delivery",
                preferred_datetime=preferred.isoformat(timespec="minutes"),
                delivery_address="1 Example Street",
                httpx_client=self.client,
            )
            locked = await accept_mock_schedule(
                "http://127.0.0.1:5555",
                schedule_id=schedule["schedule_id"],
                httpx_client=self.client,
            )
            released = await release_mock_schedule(
                "http://127.0.0.1:5555",
                schedule_id=schedule["schedule_id"],
                httpx_client=self.client,
            )
            replacement = await propose_mock_schedule(
                "http://127.0.0.1:5555",
                quote_id=price_offer["quote"]["quote_id"],
                fulfillment_type="delivery",
                preferred_datetime=preferred.isoformat(timespec="minutes"),
                delivery_address="1 Example Street",
                httpx_client=self.client,
            )

        self.assertEqual(locked["status"], "locked")
        self.assertEqual(released["status"], "released")
        self.assertEqual(replacement["status"], "available")
        self.assertEqual(replacement["offered_datetime"], preferred.isoformat(timespec="minutes"))

    async def test_stripe_sandbox_checkout_creates_spt_and_completes_payment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_test_example",
                "STRIPE_BUSINESS_PROFILE_ID": "profile_test_example",
                "STRIPE_ENABLE_TEST_HELPER": "1",
            },
        ):
            app = build_app(
                catalog=self.catalog,
                store_url="http://127.0.0.1:5555",
            )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:5555",
        )
        try:
            with (
                patch.object(
                    StripeTestGateway,
                    "create_test_spt",
                    new=AsyncMock(return_value="spt_test_example"),
                ) as create_spt,
                patch.object(
                    StripeTestGateway,
                    "charge_spt",
                    new=AsyncMock(
                        return_value={"id": "pi_test_example", "status": "succeeded"}
                    ),
                ) as charge_spt,
            ):
                profile = (await client.get("/.well-known/ucp")).json()["ucp"]
                handler = profile["payment_handlers"]["com.stripe.payments"][0]
                self.assertEqual(handler["config"]["environment"], "testmode")
                self.assertEqual(
                    handler["config"]["business_profile"],
                    "profile_test_example",
                )

                product = self.catalog.search()[0]
                created = await client.post(
                    "/ucp/checkout-sessions",
                    json={"line_items": [{"item": {"id": product["variant_id"]}}]},
                )
                checkout = created.json()
                self.assertEqual(created.status_code, 201)
                self.assertEqual(
                    checkout["ucp"]["payment_handlers"]["com.stripe.payments"][0]["id"],
                    "stripe_payments",
                )

                instrument_response = await client.post(
                    f"/ucp/test-payment-instruments/{checkout['id']}",
                    json={},
                )
                instrument = instrument_response.json()["payment"]["instruments"][0]
                self.assertEqual(instrument["credential"]["token"], "spt_test_example")
                create_spt.assert_awaited_once_with(
                    product["price_amount"],
                    "USD",
                )

                completed = await client.post(
                    f"/ucp/checkout-sessions/{checkout['id']}/complete",
                    json={"payment": {"instruments": [instrument]}},
                )
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["status"], "completed")
                self.assertNotIn("payment_intent_id", completed.json())
                self.assertNotIn("active_token_hash", completed.json())
                charge_spt.assert_awaited_once()
                self.assertEqual(
                    charge_spt.await_args.kwargs["amount"],
                    product["price_amount"],
                )
                self.assertEqual(
                    charge_spt.await_args.kwargs["token"],
                    "spt_test_example",
                )

                repeated = await client.post(
                    f"/ucp/checkout-sessions/{checkout['id']}/complete",
                    json={"payment": {"instruments": [instrument]}},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(repeated.json()["status"], "completed")
                charge_spt.assert_awaited_once()
        finally:
            await client.aclose()

    async def test_stripe_payment_failure_does_not_complete_checkout(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_test_example",
                "STRIPE_BUSINESS_PROFILE_ID": "profile_test_example",
                "STRIPE_ENABLE_TEST_HELPER": "1",
            },
        ):
            app = build_app(catalog=self.catalog)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:5555",
        )
        try:
            with (
                patch.object(
                    StripeTestGateway,
                    "create_test_spt",
                    new=AsyncMock(return_value="spt_test_example"),
                ),
                patch.object(
                    StripeTestGateway,
                    "charge_spt",
                    new=AsyncMock(
                        side_effect=StripePaymentError(
                            "card_declined",
                            "Stripe could not complete the payment.",
                        )
                    ),
                ),
            ):
                checkout = (
                    await client.post(
                        "/ucp/checkout-sessions",
                        json={
                            "line_items": [
                                {"item": {"id": self.catalog.search()[0]["variant_id"]}}
                            ]
                        },
                    )
                ).json()
                instrument = (
                    await client.post(
                        f"/ucp/test-payment-instruments/{checkout['id']}",
                        json={},
                    )
                ).json()["payment"]["instruments"][0]
                failed = await client.post(
                    f"/ucp/checkout-sessions/{checkout['id']}/complete",
                    json={"payment": {"instruments": [instrument]}},
                )
                self.assertEqual(failed.status_code, 402)
                self.assertEqual(
                    failed.json()["messages"][0]["code"],
                    "payment_failed",
                )
                self.assertNotEqual(failed.json().get("status"), "completed")
        finally:
            await client.aclose()

    async def test_legacy_database_is_migrated_to_the_real_catalog(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.duckdb"
        connection = duckdb.connect(str(legacy_path))
        connection.execute(
            """
            CREATE TABLE products (
                product_id VARCHAR PRIMARY KEY,
                variant_id VARCHAR UNIQUE NOT NULL,
                upc VARCHAR UNIQUE NOT NULL,
                title VARCHAR NOT NULL,
                category VARCHAR NOT NULL,
                brand VARCHAR NOT NULL,
                description VARCHAR NOT NULL,
                price_amount BIGINT NOT NULL,
                currency VARCHAR NOT NULL,
                inventory INTEGER NOT NULL,
                available BOOLEAN NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO products VALUES (
                'old-product', '200100000010', '200100000010',
                'Fictional Trail Shoes', 'Shoes', 'Mock Brand',
                'Old test record', 1000, 'USD', 2, TRUE
            )
            """
        )
        connection.close()

        migrated_catalog = DuckDBCatalog(legacy_path)
        try:
            products = migrated_catalog.search()
            self.assertEqual(len(products), 10)
            self.assertNotIn("Fictional Trail Shoes", [item["title"] for item in products])
            self.assertTrue(all(item["image_path"] for item in products))
        finally:
            migrated_catalog.close()


if __name__ == "__main__":
    unittest.main()
