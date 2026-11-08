import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
import typer
from a2a.client import A2ACardResolver
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from a2a_agent.discovery import DiscoveryError
from a2a_agent.openrouter import OpenRouterError, match_catalog_question
from a2a_agent.ucp import (
    accept_mock_schedule,
    browse_catalog,
    confirm_mock_schedule,
    discover_ucp_store,
    negotiate_mock_price,
    propose_mock_schedule,
    request_default_discount,
    release_mock_schedule,
    test_purchase,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "Config" / "buyer.json"
DEFAULT_STORE_URL = "http://127.0.0.1:5555"

app = typer.Typer(
    name="buyer-agent",
    help="Discover products and place a Stripe sandbox order with the mock merchant.",
    invoke_without_command=True,
    no_args_is_help=False,
)
console = Console()


def _validate_local_store_url(store_url: str) -> str:
    try:
        parsed = urlsplit(store_url)
        hostname = parsed.hostname
        port = parsed.port
        is_loopback = hostname is not None and ipaddress.ip_address(hostname).is_loopback
    except ValueError as exc:
        raise DiscoveryError("invalid_url", "Mock store URL is invalid.") from exc
    if (
        os.environ.get("A2A_ALLOW_LOCAL_TEST_STORE") != "1"
        or parsed.scheme != "http"
        or not is_loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DiscoveryError(
            "unsafe_test_purchase",
            "Buyer CLI purchases require A2A_ALLOW_LOCAL_TEST_STORE=1 and a literal loopback URL.",
        )
    return f"http://{hostname}:{port}"


def _load_buyer_profile(config_path: Path) -> dict[str, str]:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read buyer profile: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Buyer profile must contain valid JSON.") from exc
    if not isinstance(raw, dict):
        raise ValueError("Buyer profile must be a JSON object.")
    profile = {
        key: raw.get(key)
        for key in ("email", "phone", "delivery_address")
    }
    if any(not isinstance(value, str) or not value.strip() for value in profile.values()):
        raise ValueError("Buyer profile must provide email, phone, and delivery_address.")
    return {key: value.strip() for key, value in profile.items()}


def _dollars_to_cents(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Offer must be a valid USD amount.") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("Offer must be positive and have no more than two decimal places.")
    return int(amount * 100)


async def _discover_a2a_card(store_url: str) -> Any:
    async with httpx.AsyncClient(
        timeout=8.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        resolver = A2ACardResolver(httpx_client=client, base_url=store_url)
        return await resolver.get_agent_card()


async def _run_purchase(store_url: str, config_path: Path) -> None:
    store_url = _validate_local_store_url(store_url)
    buyer = _load_buyer_profile(config_path)

    console.print("[bold]Discovering mock merchant over A2A and UCP...[/bold]")
    card = await _discover_a2a_card(store_url)
    console.print(f"A2A merchant: [bold]{card.name}[/bold] ({card.version})")
    profile = await discover_ucp_store(store_url)
    if profile.get("status") != "profile_found" or not profile.get("checkout_available"):
        raise DiscoveryError("checkout_unavailable", "Merchant does not offer UCP checkout.")
    if "com.stripe.payments" not in profile.get("payment_handlers", []):
        raise DiscoveryError(
            "stripe_unavailable",
            "Merchant does not advertise Stripe Payments. Configure test-mode Stripe credentials on the mock store.",
        )

    question = Prompt.ask(
        "Ask a shopping question about products in this marketplace",
        default="Peanut Butter",
    )
    result = await browse_catalog(store_url)
    catalog = result.get("catalog", {})
    products = catalog.get("products", []) if isinstance(catalog, dict) else []
    catalog_items: list[dict[str, Any]] = []
    for product in products:
        variants = product.get("variants", []) if isinstance(product, dict) else []
        if not variants:
            continue
        variant = variants[0]
        price = variant.get("price", {})
        if (
            not isinstance(price, dict)
            or isinstance(price.get("amount"), bool)
            or not isinstance(price.get("amount"), int)
        ):
            continue
        catalog_items.append(
            {
                "title": product.get("title", "Unnamed product"),
                "item_id": variant.get("id"),
                "description": (
                    product.get("description", {}).get("plain", "")
                    if isinstance(product.get("description"), dict)
                    else ""
                ),
                "category": (
                    product.get("categories", [{}])[0].get("value", "")
                    if product.get("categories")
                    and isinstance(product["categories"][0], dict)
                    else ""
                ),
                "brand": (
                    product.get("metadata", {}).get("brand", "")
                    if isinstance(product.get("metadata"), dict)
                    else ""
                ),
                "price_amount": price["amount"],
                "currency": price.get("currency", "USD"),
                "available": (
                    variant.get("availability", {}).get("available", False)
                    if isinstance(variant.get("availability"), dict)
                    else False
                ),
                "upc": next(
                    (
                        barcode.get("value")
                        for barcode in variant.get("barcodes", [])
                        if barcode.get("type") == "UPC"
                    ),
                    "",
                ),
            }
        )
    if not catalog_items:
        console.print("[yellow]The marketplace catalog is empty.[/yellow]")
        return

    matching_ids = set(await match_catalog_question(question, catalog_items))
    available = [
        item
        for item in catalog_items
        if item["available"] and item["item_id"] in matching_ids
    ]
    if not available:
        console.print(
            "[yellow]Product not found in this marketplace catalog. "
            "Try asking about one of the products listed by the mock store.[/yellow]"
        )
        return

    table = Table(title="Matching products from the marketplace catalog")
    table.add_column("#", justify="right")
    table.add_column("Product")
    table.add_column("UPC-A")
    table.add_column("Price", justify="right")
    for index, product in enumerate(available, 1):
        table.add_row(
            str(index),
            str(product["title"]),
            str(product["upc"]),
            f"${product['price_amount'] / 100:.2f}",
        )
    console.print(table)
    console.print(
        "[dim]These results are the only products matched from the merchant's "
        "current catalog. Prices and availability are taken from the merchant.[/dim]"
    )
    if not typer.confirm("Would you like to purchase one of these products?", default=False):
        console.print("Marketplace search completed; no purchase was started.")
        return
    selected = int(Prompt.ask("Choose a product number", default="1"))
    if selected < 1 or selected > len(available):
        raise ValueError("Choose a listed product number.")
    product = available[selected - 1]
    quantity = int(Prompt.ask("Quantity", default="1"))
    if quantity < 1:
        raise ValueError("Quantity must be positive.")
    postal_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", buyer["delivery_address"])
    if postal_match is None:
        raise ValueError("Buyer delivery address must contain a 5-digit ZIP code for the seller quote.")
    destination_postal_code = postal_match.group(1)
    accepted_quote: dict[str, Any] | None = None
    if typer.confirm(
        "Ask the seller whether a published discount is available?",
        default=False,
    ):
        offer_response = await request_default_discount(
            store_url,
            product["item_id"],
            quantity,
            destination_postal_code=destination_postal_code,
        )
        if offer_response.get("status") == "offered":
            offer = offer_response["quote"]
            console.print(
                f"The seller offers {offer['discount_percent']}% off: "
                f"${offer['unit_price'] / 100:.2f} each, "
                f"${offer['total_amount'] / 100:.2f} total, "
                f"{'free delivery' if offer['delivery_fee_amount'] == 0 else f'${offer['delivery_fee_amount'] / 100:.2f} delivery'}."
            )
            if typer.confirm("Accept this published offer?", default=False):
                accepted_quote = offer
        elif offer_response.get("status") == "no_offer":
            console.print("The seller has no published discount for this product.")

    if accepted_quote is None:
        if not typer.confirm(
            "Would you like to make an explicit offer to negotiate price and delivery terms?",
            default=False,
        ):
            console.print("No offer accepted and no purchase was started.")
            return
        wait_days = int(
            Prompt.ask(
                "How long are you willing to wait for delivery? (days: 0, 7, or 14)",
                choices=["0", "7", "14"],
                default="0",
            )
        )
        request_free_delivery = typer.confirm(
            "Ask the seller to include delivery at no extra charge?",
            default=True,
        )
        default_offer = product["price_amount"] * 90 // 100
        target_price = _dollars_to_cents(
            Prompt.ask(
                "Your maximum offer per item in USD",
                default=f"{default_offer / 100:.2f}",
            )
        )

        for round_number in range(1, 6):
            response = await negotiate_mock_price(
                store_url,
                product["item_id"],
                quantity,
                target_price,
                wait_days=wait_days,
                request_free_delivery=request_free_delivery,
                destination_postal_code=destination_postal_code,
            )
            quote = response.get("quote")
            if not isinstance(quote, dict):
                raise DiscoveryError("invalid_quote", "Merchant returned an invalid A2A negotiation quote.")
            quoted_amount = quote.get("unit_price")
            delivery_fee = quote.get("delivery_fee_amount")
            total_amount = quote.get("total_amount")
            quote_id = quote.get("quote_id")
            if (
                isinstance(quoted_amount, bool)
                or not isinstance(quoted_amount, int)
                or isinstance(delivery_fee, bool)
                or not isinstance(delivery_fee, int)
                or isinstance(total_amount, bool)
                or not isinstance(total_amount, int)
                or not isinstance(quote_id, str)
            ):
                raise DiscoveryError("invalid_quote", "Merchant returned an invalid A2A negotiation quote.")
            console.print(
                f"\nA2A negotiation round {round_number}: seller "
                f"[bold]{response.get('status')}[/bold] — "
                f"${quoted_amount / 100:.2f} each, "
                f"{'free delivery' if delivery_fee == 0 else f'${delivery_fee / 100:.2f} delivery'}, "
                f"${total_amount / 100:.2f} total; "
                f"delivery wait {quote.get('wait_days')} days."
            )
            if typer.confirm("Accept the seller's quoted terms?", default=False):
                accepted_quote = quote
                break
            if round_number == 5 or not typer.confirm("Make another offer?", default=True):
                console.print("Negotiation ended; no checkout or payment was started.")
                return
            target_price = _dollars_to_cents(
                Prompt.ask(
                    "Your next maximum offer per item in USD",
                    default=f"{quoted_amount / 100:.2f}",
                )
            )
            wait_days = int(
                Prompt.ask(
                    "Requested delivery wait? (days: 0, 7, or 14)",
                    choices=["0", "7", "14"],
                    default=str(wait_days),
                )
            )
            request_free_delivery = typer.confirm(
                "Ask again for delivery at no extra charge?",
                default=request_free_delivery,
            )

    if accepted_quote is None:
        console.print("Negotiation ended without an accepted quote.")
        return

    fulfillment_type = Prompt.ask(
        "Fulfillment method",
        choices=["delivery", "pickup"],
        default="delivery",
    )
    schedule_offer: dict[str, Any] | None = None
    if fulfillment_type == "pickup":
        schedule_offer = await propose_mock_schedule(
            store_url,
            quote_id=accepted_quote["quote_id"],
            fulfillment_type="pickup",
        )
        appointment_datetime = schedule_offer["offered_datetime"]
        fulfillment_address = schedule_offer["pickup_address"]
        console.print(
            f"The seller assigned store pickup for {appointment_datetime} "
            f"({schedule_offer['timezone']}) at {fulfillment_address}."
        )
    else:
        earliest_date = date.today() + timedelta(
            days=max(1, accepted_quote["wait_days"])
        )
        preferred_datetime = Prompt.ask(
            "Preferred delivery date and time (YYYY-MM-DD HH:MM, local merchant time)",
            default=f"{earliest_date.isoformat()} 10:00",
        )
        for round_number in range(1, 6):
            try:
                preferred = datetime.fromisoformat(
                    preferred_datetime.replace(" ", "T", 1)
                )
            except ValueError as exc:
                raise ValueError("Enter delivery date and time as YYYY-MM-DD HH:MM.") from exc
            if (
                preferred.tzinfo is not None
                or not earliest_date <= preferred.date() <= date.today() + timedelta(days=90)
            ):
                raise ValueError(
                    "Delivery date must honor the agreed wait and be within 90 days."
                )
            schedule_offer = await propose_mock_schedule(
                store_url,
                quote_id=accepted_quote["quote_id"],
                fulfillment_type="delivery",
                preferred_datetime=preferred.isoformat(timespec="minutes"),
                delivery_address=buyer["delivery_address"],
            )
            appointment_datetime = schedule_offer["offered_datetime"]
            if schedule_offer["status"] == "countered":
                console.print(
                    f"The requested time is unavailable. The seller proposes "
                    f"{appointment_datetime} ({schedule_offer['timezone']})."
                )
            else:
                console.print(
                    f"The seller can deliver at {appointment_datetime} "
                    f"({schedule_offer['timezone']})."
                )
            if typer.confirm("Accept this delivery appointment?", default=False):
                lock = await accept_mock_schedule(
                    store_url,
                    schedule_id=schedule_offer["schedule_id"],
                )
                if lock["status"] == "locked":
                    schedule_offer.update(lock)
                    fulfillment_address = buyer["delivery_address"]
                    break
                schedule_offer.update(lock)
                console.print(
                    f"That appointment was just taken. The seller proposes "
                    f"{schedule_offer['offered_datetime']} "
                    f"({schedule_offer['timezone']})."
                )
                if not typer.confirm("Accept the revised appointment?", default=False):
                    if round_number == 5 or not typer.confirm(
                        "Propose another delivery time?", default=True
                    ):
                        console.print("Scheduling ended; no checkout or payment was started.")
                        return
                    preferred_datetime = Prompt.ask(
                        "Another preferred date and time (YYYY-MM-DD HH:MM)",
                        default=appointment_datetime.replace("T", " "),
                    )
                    continue
                lock = await accept_mock_schedule(
                    store_url,
                    schedule_id=schedule_offer["schedule_id"],
                )
                if lock["status"] != "locked":
                    schedule_offer.update(lock)
                    console.print(
                        "The revised appointment is no longer available. "
                        "Please propose another delivery time."
                    )
                    preferred_datetime = lock["offered_datetime"].replace("T", " ")
                    continue
                schedule_offer.update(lock)
                fulfillment_address = buyer["delivery_address"]
                break
            if round_number == 5 or not typer.confirm(
                "Propose another delivery time?", default=True
            ):
                console.print("Scheduling ended; no checkout or payment was started.")
                return
            preferred_datetime = Prompt.ask(
                "Another preferred date and time (YYYY-MM-DD HH:MM)",
                default=appointment_datetime.replace("T", " "),
            )
        else:
            console.print("No delivery appointment was accepted; no purchase was started.")
            return

    if schedule_offer is None:
        raise DiscoveryError("invalid_a2a_response", "Merchant did not return a schedule offer.")
    appointment_datetime = schedule_offer["offered_datetime"]
    fulfillment_address = (
        schedule_offer["pickup_address"]
        if fulfillment_type == "pickup"
        else buyer["delivery_address"]
    )
    delivery_fee_amount = (
        0
        if fulfillment_type == "pickup"
        else accepted_quote["delivery_fee_amount"]
    )
    checkout_total_amount = (
        accepted_quote["unit_price"] * quantity + delivery_fee_amount
    )

    console.print("\n[bold]Order summary[/bold]")
    console.print(f"Product: {product['title']} × {quantity}")
    console.print(f"Sandbox total: ${checkout_total_amount / 100:.2f} USD")
    console.print(
        f"Delivery fee: ${delivery_fee_amount / 100:.2f} USD"
    )
    console.print(
        f"{'Pickup' if fulfillment_type == 'pickup' else 'Delivery'} appointment: "
        f"{appointment_datetime} ({schedule_offer['timezone']})"
    )
    console.print(f"Buyer email: {buyer['email']}")
    console.print(
        f"{'Pickup location' if fulfillment_type == 'pickup' else 'Delivery address'}: "
        f"{fulfillment_address}"
    )
    if not typer.confirm("Authorize this Stripe TEST-MODE payment?", default=False):
        await release_mock_schedule(
            store_url,
            schedule_id=schedule_offer["schedule_id"],
        )
        console.print("Purchase cancelled; no payment was attempted.")
        return

    payment = await test_purchase(
        store_url,
        [{"item_id": product["item_id"], "quantity": quantity}],
        negotiation_id=accepted_quote["quote_id"],
        fulfillment_type=fulfillment_type,
    )
    checkout = payment.get("checkout", {})
    checkout_id = checkout.get("id") if isinstance(checkout, dict) else None
    if not payment.get("purchase_completed") or not isinstance(checkout_id, str):
        raise DiscoveryError("payment_incomplete", "Stripe sandbox payment was not confirmed.")
    console.print(f"[green]Stripe sandbox payment confirmed.[/green] Checkout: {checkout_id}")

    schedule = await confirm_mock_schedule(
        store_url,
        schedule_id=schedule_offer["schedule_id"],
        checkout_id=checkout_id,
        email=buyer["email"],
        phone=buyer["phone"],
    )
    if schedule.get("status") != "confirmed":
        raise DiscoveryError(
            "delivery_schedule_failed",
            "Payment succeeded, but the seller did not confirm the order over A2A. "
            "Check the merchant's SMTP configuration and retry confirmation.",
        )
    confirmation = schedule["order_confirmation"]
    console.print(
        "\n[bold]Seller confirmation received over A2A[/bold]\n"
        f"Product: {confirmation['product_title']} × {confirmation['quantity']}\n"
        f"UPC-A: {confirmation['upc']}\n"
        f"Unit price: ${confirmation['unit_price'] / 100:.2f} USD\n"
        f"Delivery fee: ${confirmation['delivery_fee_amount'] / 100:.2f} USD\n"
        f"Final total: ${confirmation['total_amount'] / 100:.2f} USD\n"
        f"Fulfillment: {confirmation['fulfillment_type']}\n"
        f"Appointment: {confirmation['appointment_datetime']} "
        f"({confirmation['timezone']})\n"
        f"Address: {confirmation['address']}\n"
        f"Agreed delivery wait: {confirmation['wait_days']} day(s)\n"
        f"Delivery fee waived: {'yes' if confirmation['free_delivery'] else 'no'}\n"
        f"Seller sent the confirmation email to {buyer['email']}.\n"
        "[yellow]Reminder: payment and delivery are sandbox/mock only; "
        "no real goods, carrier service, or physical pickup are involved.[/yellow]"
    )


@app.callback()
def run_cli(
    ctx: typer.Context,
    store_url: str = typer.Option(DEFAULT_STORE_URL, help="Loopback mock-store URL."),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", help="Local buyer profile JSON."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    try:
        asyncio.run(_run_purchase(store_url, config))
    except (DiscoveryError, OpenRouterError, httpx.HTTPError, ValueError, OSError) as exc:
        console.print(f"[red]Buyer agent stopped: {exc}[/red]")
        raise typer.Exit(code=1) from exc


def main() -> None:
    app()


if __name__ == "__main__":
    main()
