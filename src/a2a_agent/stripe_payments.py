import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


STRIPE_API_VERSION = "2026-09-30.preview"
STRIPE_API_URL = "https://api.stripe.com"
STRIPE_TIMEOUT_SECONDS = 12.0
STRIPE_HANDLER_ID = "stripe_payments"
STRIPE_HANDLER_VERSION = "2026-06-25"


class StripePaymentError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class StripeTestGateway:
    secret_key: str
    business_profile_id: str

    @classmethod
    def from_environment(cls) -> "StripeTestGateway | None":
        secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
        profile_id = os.environ.get("STRIPE_BUSINESS_PROFILE_ID", "").strip()
        if not secret_key and not profile_id:
            return None
        if not secret_key.startswith(("sk_test_", "rk_test_")):
            raise ValueError(
                "STRIPE_SECRET_KEY must be a Stripe test-mode secret or restricted key."
            )
        if not profile_id.startswith("profile_test_"):
            raise ValueError(
                "STRIPE_BUSINESS_PROFILE_ID must be a Stripe test-mode profile ID."
            )
        return cls(secret_key=secret_key, business_profile_id=profile_id)

    def payment_handler(self) -> dict[str, Any]:
        return {
            "id": STRIPE_HANDLER_ID,
            "version": STRIPE_HANDLER_VERSION,
            "spec": (
                "https://docs.stripe.com/agentic-commerce/"
                "protocols/ucp/stripe-payments-handler"
            ),
            "schema": "https://ucp.stripe.com/payments/2026-06-25/schema.json",
            "available_instruments": [{"type": "card"}],
            "config": {
                "environment": "testmode",
                "business_profile": self.business_profile_id,
            },
        }

    async def create_test_spt(self, amount: int, currency: str) -> str:
        result = await self._post(
            "/v1/test_helpers/shared_payment/granted_tokens",
            {
                "payment_method": "pm_card_visa",
                "usage_limits[currency]": currency.lower(),
                "usage_limits[max_amount]": str(amount),
                "usage_limits[expires_at]": str(int(time.time()) + 900),
            },
        )
        token = result.get("id")
        if not isinstance(token, str) or not token.startswith("spt_"):
            raise StripePaymentError(
                "stripe_invalid_response",
                "Stripe did not return a valid test payment token.",
            )
        return token

    async def charge_spt(
        self,
        *,
        amount: int,
        currency: str,
        token: str,
        idempotency_key: str,
        checkout_id: str,
    ) -> dict[str, Any]:
        return await self._post(
            "/v1/payment_intents",
            {
                "amount": str(amount),
                "currency": currency.lower(),
                "payment_method_data[shared_payment_granted_token]": token,
                "confirm": "true",
                "metadata[checkout_session_id]": checkout_id,
            },
            idempotency_key=idempotency_key,
        )

    async def _post(
        self,
        path: str,
        data: dict[str, str],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Stripe-Version": STRIPE_API_VERSION}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                timeout=STRIPE_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{STRIPE_API_URL}{path}",
                    data=data,
                    headers=headers,
                    auth=(self.secret_key, ""),
                )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise StripePaymentError(
                "stripe_unavailable",
                "Could not confirm the Stripe request outcome.",
                retryable=True,
            ) from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise StripePaymentError(
                "stripe_invalid_response",
                "Stripe returned an invalid response.",
                retryable=response.status_code >= 500,
            ) from exc
        if not isinstance(result, dict):
            raise StripePaymentError(
                "stripe_invalid_response",
                "Stripe returned an invalid response.",
                retryable=response.status_code >= 500,
            )
        if not response.is_success:
            error = result.get("error")
            raw_code = error.get("code") if isinstance(error, dict) else None
            code = (
                raw_code[:80]
                if isinstance(raw_code, str) and raw_code
                else "stripe_request_failed"
            )
            raise StripePaymentError(
                code,
                "Stripe could not complete the payment.",
                retryable=response.status_code >= 500,
            )
        return result
