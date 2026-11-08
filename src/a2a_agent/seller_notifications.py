import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


def _smtp_settings() -> dict[str, Any]:
    required = (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM_EMAIL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError(
            "Seller confirmation email is not configured. Set these environment "
            "variables: " + ", ".join(missing)
        )
    try:
        port = int(os.environ["SMTP_PORT"])
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535.")
    return {
        "host": os.environ["SMTP_HOST"],
        "port": port,
        "username": os.environ["SMTP_USERNAME"],
        "password": os.environ["SMTP_PASSWORD"],
        "from_email": os.environ["SMTP_FROM_EMAIL"],
    }


def smtp_configuration_available() -> bool:
    try:
        _smtp_settings()
    except ValueError:
        return False
    return True


def send_order_confirmation_email(
    *,
    buyer_email: str,
    order_confirmation: dict[str, Any],
) -> None:
    smtp = _smtp_settings()
    fulfillment_type = order_confirmation["fulfillment_type"]
    fulfillment_label = "Store pickup" if fulfillment_type == "pickup" else "Delivery"
    location_label = "Pickup location" if fulfillment_type == "pickup" else "Delivery address"
    message = EmailMessage()
    message["Subject"] = f"Mock Market order confirmed: {order_confirmation['checkout_id']}"
    message["From"] = smtp["from_email"]
    message["To"] = buyer_email
    message.set_content(
        "Your mock-store sandbox order is confirmed by the seller.\n\n"
        f"Product: {order_confirmation['product_title']}\n"
        f"UPC-A: {order_confirmation['upc']}\n"
        f"Quantity: {order_confirmation['quantity']}\n"
        f"Unit price: ${order_confirmation['unit_price'] / 100:.2f} USD\n"
        f"Items subtotal: ${order_confirmation['subtotal_amount'] / 100:.2f} USD\n"
        f"Delivery fee: ${order_confirmation['delivery_fee_amount'] / 100:.2f} USD\n"
        f"Final total: ${order_confirmation['total_amount'] / 100:.2f} USD\n"
        f"Fulfillment: {fulfillment_label}\n"
        f"{fulfillment_label} appointment: "
        f"{order_confirmation['appointment_datetime']} "
        f"({order_confirmation['timezone']})\n"
        f"{location_label}: {order_confirmation['address']}\n"
        f"Agreed delivery wait: {order_confirmation['wait_days']} day(s)\n"
        f"Delivery fee waived: {'yes' if order_confirmation['free_delivery'] else 'no'}\n"
        f"Checkout: {order_confirmation['checkout_id']}\n\n"
        "This is a test-mode payment and mock appointment. No live funds were "
        "charged and no carrier or physical pickup was booked."
    )
    context = ssl.create_default_context()
    with smtplib.SMTP(smtp["host"], smtp["port"], timeout=20) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(smtp["username"], smtp["password"])
        server.send_message(message)
