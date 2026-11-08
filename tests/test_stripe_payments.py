import os
import unittest
from unittest.mock import AsyncMock, patch

from a2a_agent.stripe_payments import StripeTestGateway


class StripeConfigurationTests(unittest.TestCase):
    def test_stripe_gateway_requires_a_test_key_and_test_profile(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_test_example",
                "STRIPE_BUSINESS_PROFILE_ID": "profile_test_example",
            },
        ):
            gateway = StripeTestGateway.from_environment()

        self.assertIsNotNone(gateway)
        assert gateway is not None
        self.assertEqual(gateway.payment_handler()["config"]["environment"], "testmode")
        self.assertEqual(
            gateway.payment_handler()["config"]["business_profile"],
            "profile_test_example",
        )

    def test_live_key_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_live_never_use",
                "STRIPE_BUSINESS_PROFILE_ID": "profile_test_example",
            },
        ):
            with self.assertRaisesRegex(ValueError, "test-mode"):
                StripeTestGateway.from_environment()

    def test_partial_stripe_configuration_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "",
                "STRIPE_BUSINESS_PROFILE_ID": "profile_test_example",
            },
        ):
            with self.assertRaisesRegex(ValueError, "test-mode secret"):
                StripeTestGateway.from_environment()


class StripeRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_spt_is_limited_to_checkout_amount_and_expiration(self) -> None:
        gateway = StripeTestGateway("sk_test_example", "profile_test_example")
        with patch.object(
            StripeTestGateway,
            "_post",
            new=AsyncMock(return_value={"id": "spt_test_example"}),
        ) as post:
            token = await gateway.create_test_spt(699, "USD")

        self.assertEqual(token, "spt_test_example")
        path, data = post.await_args.args
        self.assertEqual(path, "/v1/test_helpers/shared_payment/granted_tokens")
        self.assertEqual(data["payment_method"], "pm_card_visa")
        self.assertEqual(data["usage_limits[currency]"], "usd")
        self.assertEqual(data["usage_limits[max_amount]"], "699")
        self.assertGreater(int(data["usage_limits[expires_at]"]), 0)

    async def test_charge_uses_spt_not_raw_card_details(self) -> None:
        gateway = StripeTestGateway("sk_test_example", "profile_test_example")
        with patch.object(
            StripeTestGateway,
            "_post",
            new=AsyncMock(return_value={"id": "pi_test_example", "status": "succeeded"}),
        ) as post:
            intent = await gateway.charge_spt(
                amount=699,
                currency="USD",
                token="spt_test_example",
                idempotency_key="checkout-1",
                checkout_id="checkout-1",
            )

        self.assertEqual(intent["status"], "succeeded")
        data = post.await_args.args[1]
        self.assertEqual(data["payment_method_data[shared_payment_granted_token]"], "spt_test_example")
        self.assertEqual(data["amount"], "699")
        self.assertEqual(data["currency"], "usd")
        self.assertEqual(data["confirm"], "true")
        self.assertFalse(any("card" in key.lower() for key in data))
        self.assertEqual(post.await_args.kwargs["idempotency_key"], "checkout-1")


if __name__ == "__main__":
    unittest.main()
