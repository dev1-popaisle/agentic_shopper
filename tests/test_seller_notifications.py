import os
import unittest
from unittest.mock import patch

from a2a_agent.seller_notifications import (
    _smtp_settings,
    send_order_confirmation_email,
)


class SellerNotificationTests(unittest.TestCase):
    def test_smtp_settings_require_seller_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SMTP_HOST"):
                _smtp_settings()

    def test_seller_email_confirms_price_item_and_fulfillment(self) -> None:
        environment = {
            "SMTP_HOST": "smtp.example",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "seller",
            "SMTP_PASSWORD": "not-a-real-password",
            "SMTP_FROM_EMAIL": "orders@example.com",
        }
        confirmation = {
            "checkout_id": "mock-123",
            "product_title": "Mock product",
            "upc": "049000028911",
            "quantity": 2,
            "unit_price": 629,
            "subtotal_amount": 1258,
            "delivery_fee_amount": 1000,
            "total_amount": 2258,
            "fulfillment_type": "delivery",
            "appointment_datetime": "2026-11-09T10:00",
            "timezone": "America/Los_Angeles",
            "address": "1 Example Street, San Francisco, CA 94109",
            "wait_days": 7,
            "free_delivery": False,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("a2a_agent.seller_notifications.smtplib.SMTP") as smtp_class,
        ):
            send_order_confirmation_email(
                buyer_email="buyer@example.com",
                order_confirmation=confirmation,
            )

        smtp_class.assert_called_once_with("smtp.example", 587, timeout=20)
        server = smtp_class.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("seller", "not-a-real-password")
        message = server.send_message.call_args.args[0]
        body = message.get_content()
        self.assertEqual(message["To"], "buyer@example.com")
        self.assertIn("Product: Mock product", body)
        self.assertIn("Unit price: $6.29 USD", body)
        self.assertIn("Delivery fee: $10.00 USD", body)
        self.assertIn("Final total: $22.58 USD", body)
        self.assertIn("Delivery appointment: 2026-11-09T10:00", body)
        self.assertIn("1 Example Street, San Francisco, CA 94109", body)
        self.assertIn("Agreed delivery wait: 7 day(s)", body)
