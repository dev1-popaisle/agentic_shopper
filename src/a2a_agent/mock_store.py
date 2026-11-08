import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import smtplib
from typing import Any
from uuid import uuid4

import duckdb
import uvicorn
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from a2a_agent.seller_policy import (
    DEFAULT_SELLER_POLICY_PATH,
    SellerNegotiationPolicy,
    SellerPolicyError,
)
from a2a_agent.seller_schedule import (
    DEFAULT_SELLER_CONFIG_PATH,
    SellerScheduleConfig,
)
from a2a_agent.seller_notifications import (
    send_order_confirmation_email,
    smtp_configuration_available,
)
from a2a_agent.stripe_payments import StripePaymentError, StripeTestGateway


UCP_VERSION = "2026-08-25"
CATALOG_SEARCH = "dev.ucp.shopping.catalog.search"
CHECKOUT = "dev.ucp.shopping.checkout"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "mock_store.duckdb"
DEFAULT_IMAGE_DIR = PROJECT_ROOT / "data" / "product_images"
CATALOG_SEED_VERSION = "real-products-v1"
IMAGE_LICENSE = "CC BY-SA 3.0"
IMAGE_ATTRIBUTION = "Open Food Facts contributors"
MOCK_PRODUCTS = (
    {
        "upc": "049000028911",
        "title": "Diet Coke Soft Drink, 12-Pack",
        "category": "Beverages",
        "brand": "Coke",
        "description": "Diet cola soft drink in a 12-pack of 12 fl oz cans.",
        "price_amount": 699,
        "inventory": 24,
        "source_code": "0049000028911",
        "image_source_url": "https://images.openfoodfacts.org/images/products/004/900/002/8911/front_en.24.400.jpg",
    },
    {
        "upc": "049000028904",
        "title": "Coca-Cola Original Taste Soda, 12-Pack",
        "category": "Beverages",
        "brand": "Coca-Cola",
        "description": "Coca-Cola Original Taste in a 12-pack of 12 fl oz cans.",
        "price_amount": 699,
        "inventory": 24,
        "source_code": "0049000028904",
        "image_source_url": "https://images.openfoodfacts.org/images/products/004/900/002/8904/front_en.16.400.jpg",
    },
    {
        "upc": "016000123151",
        "title": "Honey Nut Cheerios Cereal",
        "category": "Breakfast Cereal",
        "brand": "General Mills",
        "description": "Honey Nut Cheerios toasted oat cereal.",
        "price_amount": 549,
        "inventory": 18,
        "source_code": "0016000123151",
        "image_source_url": "https://images.openfoodfacts.org/images/products/001/600/012/3151/front_en.37.400.jpg",
    },
    {
        "upc": "851087000014",
        "title": "Peanut Butter & Co Smooth Operator",
        "category": "Pantry",
        "brand": "Peanut Butter & Co",
        "description": "Smooth peanut butter spread, 16 oz jar.",
        "price_amount": 699,
        "inventory": 16,
        "source_code": "0851087000014",
        "image_source_url": "https://images.openfoodfacts.org/images/products/085/108/700/0014/front_en.15.400.jpg",
    },
    {
        "upc": "851087000021",
        "title": "Peanut Butter & Co Crunchy Peanut Butter",
        "category": "Pantry",
        "brand": "Peanut Butter & Co",
        "description": "Crunchy peanut butter spread, 16 oz jar.",
        "price_amount": 699,
        "inventory": 16,
        "source_code": "0851087000021",
        "image_source_url": "https://images.openfoodfacts.org/images/products/085/108/700/0021/front_en.16.400.jpg",
    },
    {
        "upc": "851087000038",
        "title": "Peanut Butter & Co Cinnamon Raisin Swirl",
        "category": "Pantry",
        "brand": "Peanut Butter & Co",
        "description": "Cinnamon raisin swirl peanut butter, 16 oz jar.",
        "price_amount": 749,
        "inventory": 12,
        "source_code": "0851087000038",
        "image_source_url": "https://images.openfoodfacts.org/images/products/085/108/700/0038/front_en.5.400.jpg",
    },
    {
        "upc": "851087000052",
        "title": "Peanut Butter & Co White Chocolatey Wonderful",
        "category": "Pantry",
        "brand": "Peanut Butter & Co",
        "description": "White Chocolatey Wonderful peanut butter, 16 oz jar.",
        "price_amount": 749,
        "inventory": 12,
        "source_code": "0851087000052",
        "image_source_url": "https://images.openfoodfacts.org/images/products/085/108/700/0052/front_en.5.400.jpg",
    },
    {
        "upc": "040000607359",
        "title": "Snickers Minis Candy, 9.7 oz",
        "category": "Candy",
        "brand": "Snickers",
        "description": "Mini Snickers chocolate, caramel, and peanut candy bars.",
        "price_amount": 599,
        "inventory": 20,
        "source_code": "0040000607359",
        "image_source_url": "https://images.openfoodfacts.org/images/products/004/000/060/7359/front_en.14.400.jpg",
    },
    {
        "upc": "044000060237",
        "title": "OREO Chocolate Sandwich Cookies, 18.12 oz",
        "category": "Cookies",
        "brand": "Oreo",
        "description": "Chocolate sandwich cookies with creme filling.",
        "price_amount": 549,
        "inventory": 20,
        "source_code": "0044000060237",
        "image_source_url": "https://images.openfoodfacts.org/images/products/004/400/006/0237/front_en.71.400.jpg",
    },
    {
        "upc": "044000061487",
        "title": "Oreo Mini Sandwich Cookies Variety Pack",
        "category": "Cookies",
        "brand": "Oreo",
        "description": "A variety pack of mini Oreo sandwich cookies.",
        "price_amount": 1299,
        "inventory": 12,
        "source_code": "0044000061487",
        "image_source_url": "https://images.openfoodfacts.org/images/products/004/400/006/1487/front_en.10.400.jpg",
    },
)


class DuckDBCatalog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(path))
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
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
                available BOOLEAN NOT NULL,
                image_path VARCHAR,
                product_source_url VARCHAR,
                image_source_url VARCHAR,
                image_license VARCHAR,
                image_attribution VARCHAR
            )
            """
        )
        columns = {
            row[1]
            for row in self.connection.execute("PRAGMA table_info('products')").fetchall()
        }
        for column in (
            "image_path",
            "product_source_url",
            "image_source_url",
            "image_license",
            "image_attribution",
        ):
            if column not in columns:
                self.connection.execute(f"ALTER TABLE products ADD COLUMN {column} VARCHAR")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS catalog_metadata (
                key VARCHAR PRIMARY KEY,
                value VARCHAR NOT NULL
            )
            """
        )
        self._seed_catalog()

    def _seed_catalog(self) -> None:
        version = self.connection.execute(
            "SELECT value FROM catalog_metadata WHERE key = 'seed_version'"
        ).fetchone()
        if version and version[0] == CATALOG_SEED_VERSION:
            return

        self.connection.execute("BEGIN TRANSACTION")
        try:
            self.connection.execute("DELETE FROM products")
            self.connection.executemany(
                """
                INSERT INTO products (
                    product_id, variant_id, upc, title, category, brand,
                    description, price_amount, currency, inventory, available,
                    image_path, product_source_url, image_source_url,
                    image_license, image_attribution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"product-{item['upc']}",
                        item["upc"],
                        item["upc"],
                        item["title"],
                        item["category"],
                        item["brand"],
                        item["description"],
                        item["price_amount"],
                        "USD",
                        item["inventory"],
                        item["inventory"] > 0,
                        f"data/product_images/{item['upc']}.jpg",
                        f"https://world.openfoodfacts.org/product/{item['source_code']}",
                        item["image_source_url"],
                        IMAGE_LICENSE,
                        IMAGE_ATTRIBUTION,
                    )
                    for item in MOCK_PRODUCTS
                ],
            )
            self.connection.execute(
                """
                INSERT INTO catalog_metadata VALUES ('seed_version', ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                [CATALOG_SEED_VERSION],
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.connection.close()

    def search(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        pattern = f"%{query.strip()}%"
        rows = self.connection.execute(
            """
            SELECT product_id, variant_id, upc, title, category, brand,
                   description, price_amount, currency, inventory, available,
                   image_path, product_source_url, image_source_url,
                   image_license, image_attribution
            FROM products
            WHERE ? = '' OR title ILIKE ? OR category ILIKE ? OR brand ILIKE ?
               OR description ILIKE ? OR upc = ?
            ORDER BY category, title
            LIMIT ?
            """,
            [query.strip(), pattern, pattern, pattern, pattern, query.strip(), limit],
        ).fetchall()
        return [self._as_product(row) for row in rows]

    def get_by_upc(self, upc: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT product_id, variant_id, upc, title, category, brand,
                   description, price_amount, currency, inventory, available,
                   image_path, product_source_url, image_source_url,
                   image_license, image_attribution
            FROM products WHERE upc = ?
            """,
            [upc],
        ).fetchone()
        return self._as_product(row) if row else None

    def get_by_variant_id(self, variant_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT product_id, variant_id, upc, title, category, brand,
                   description, price_amount, currency, inventory, available,
                   image_path, product_source_url, image_source_url,
                   image_license, image_attribution
            FROM products WHERE variant_id = ?
            """,
            [variant_id],
        ).fetchone()
        return self._as_product(row) if row else None

    @staticmethod
    def _as_product(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = (
            "product_id",
            "variant_id",
            "upc",
            "title",
            "category",
            "brand",
            "description",
            "price_amount",
            "currency",
            "inventory",
            "available",
            "image_path",
            "product_source_url",
            "image_source_url",
            "image_license",
            "image_attribution",
        )
        return dict(zip(keys, row))


def _ucp_product(product: dict[str, Any], store_url: str) -> dict[str, Any]:
    price = {"amount": product["price_amount"], "currency": product["currency"]}
    return {
        "id": product["product_id"],
        "title": product["title"],
        "description": {"plain": product["description"]},
        "categories": [{"value": product["category"], "taxonomy": "merchant"}],
        "metadata": {
            "brand": product["brand"],
            "image_url": f"{store_url}/images/{product['upc']}",
            "product_source_url": product["product_source_url"],
            "image_source_url": product["image_source_url"],
            "image_license": product["image_license"],
            "image_attribution": product["image_attribution"],
        },
        "price_range": {"min": price, "max": price},
        "variants": [
            {
                "id": product["variant_id"],
                "title": product["title"],
                "description": {"plain": product["description"]},
                "sku": product["product_id"],
                "barcodes": [{"type": "UPC", "value": product["upc"]}],
                "price": price,
                "availability": {
                    "available": product["available"],
                    "status": "in_stock" if product["available"] else "out_of_stock",
                },
            }
        ],
    }


def _ucp_meta(stripe_gateway: StripeTestGateway | None = None) -> dict[str, Any]:
    payment_handlers = (
        {"com.stripe.payments": [stripe_gateway.payment_handler()]}
        if stripe_gateway
        else {}
    )
    return {
        "version": UCP_VERSION,
        "status": "success",
        "capabilities": {
            CATALOG_SEARCH: [{"version": UCP_VERSION}],
            CHECKOUT: [{"version": UCP_VERSION}],
        },
        "payment_handlers": payment_handlers,
    }


def _public_checkout(session: dict[str, Any]) -> dict[str, Any]:
    private_fields = {
        "payment_attempt",
        "active_idempotency_key",
        "active_token_hash",
        "payment_intent_id",
    }
    return {
        key: value
        for key, value in session.items()
        if key not in private_fields
    }


def _request_is_loopback(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


class MockMerchantExecutor(AgentExecutor):
    def __init__(
        self,
        catalog: DuckDBCatalog,
        store_url: str,
        seller_policy: SellerNegotiationPolicy,
        negotiation_quotes: dict[str, dict[str, Any]],
        checkout_sessions: dict[str, dict[str, Any]],
        schedule_offers: dict[str, dict[str, Any]],
        delivery_schedules: dict[str, dict[str, Any]],
        schedule_config: SellerScheduleConfig,
    ) -> None:
        self.catalog = catalog
        self.store_url = store_url
        self.seller_policy = seller_policy
        self.negotiation_quotes = negotiation_quotes
        self.checkout_sessions = checkout_sessions
        self.schedule_offers = schedule_offers
        self.delivery_schedules = delivery_schedules
        self.schedule_config = schedule_config

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Processing a request for the mock merchant agent..."),
        )
        text = get_message_text(context.message).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"query": text}
        if isinstance(payload, dict) and payload.get("action") == "negotiate_price":
            response = self._negotiate_price(payload)
        elif isinstance(payload, dict) and payload.get("action") == "request_default_discount":
            response = self._request_default_discount(payload)
        elif isinstance(payload, dict) and payload.get("action") == "schedule_order":
            response = self._schedule_order(payload)
        else:
            query = payload.get("query", "") if isinstance(payload, dict) else ""
            if not isinstance(query, str) or len(query) > 2_000:
                response = {
                    "status": "error",
                    "message": 'Use a JSON object with a "query" string of at most 2000 characters.',
                }
            else:
                response = {
                    "status": "success",
                    "protocol": "A2A",
                    "products": [
                        _ucp_product(item, self.store_url)
                        for item in self.catalog.search(query)
                    ],
                }

        await updater.add_artifact(
            parts=[
                new_text_part(
                    text=json.dumps(response, indent=2),
                    media_type="application/json",
                )
            ]
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Mock merchant request completed."),
        )

    def _negotiate_price(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("item_id")
        quantity = payload.get("quantity")
        target_unit_price = payload.get("target_unit_price")
        wait_days = payload.get("wait_days")
        request_free_delivery = payload.get("request_free_delivery")
        postal_code = payload.get("destination_postal_code")
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 256
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
            or isinstance(target_unit_price, bool)
            or not isinstance(target_unit_price, int)
            or target_unit_price < 1
            or isinstance(wait_days, bool)
            or wait_days not in (0, 7, 14)
            or not isinstance(request_free_delivery, bool)
            or not isinstance(postal_code, str)
            or not re.fullmatch(r"\d{5}", postal_code)
        ):
            return {
                "status": "error",
                "error": {
                    "code": "invalid_negotiation",
                    "message": "A product, quantity, maximum unit offer, wait period, and 5-digit destination ZIP are required.",
                },
            }

        product = self.catalog.get_by_variant_id(item_id)
        if product is None or not product["available"]:
            return {
                "status": "error",
                "error": {
                    "code": "product_not_available",
                    "message": "The requested product is not available in this marketplace.",
                },
            }
        if quantity > product["inventory"]:
            return {
                "status": "error",
                "error": {
                    "code": "insufficient_inventory",
                    "message": "The requested quantity is not available.",
                },
            }

        try:
            delivery_fee = self.seller_policy.delivery_fee(quantity, postal_code)
            base_discount = self.seller_policy.demand_discount_percent(product["upc"])
        except SellerPolicyError:
            return {
                "status": "error",
                "error": {
                    "code": "seller_unable_to_quote",
                    "message": "The seller cannot quote this product or delivery destination.",
                },
            }
        total_discount = min(
            100,
            base_discount + self.seller_policy.wait_discount_percent(wait_days),
        )
        list_price = product["price_amount"]
        minimum_price = (list_price * (100 - total_discount) + 99) // 100
        unit_price = min(list_price, max(minimum_price, target_unit_price))
        price_accepted = target_unit_price >= minimum_price
        delivery_request_accepted = not request_free_delivery or delivery_fee == 0
        status = (
            "accepted"
            if price_accepted and delivery_request_accepted
            else "countered"
        )
        quote_id = f"quote-{uuid4().hex}"
        quote = {
            "quote_id": quote_id,
            "item_id": product["variant_id"],
            "upc": product["upc"],
            "quantity": quantity,
            "unit_price": unit_price,
            "delivery_fee_amount": delivery_fee,
            "total_amount": unit_price * quantity + delivery_fee,
            "currency": product["currency"],
            "wait_days": wait_days,
            "destination_postal_code": postal_code,
            "free_delivery": delivery_fee == 0,
            "status": status,
            "delivery_request_accepted": delivery_request_accepted,
            "used": False,
        }
        self.negotiation_quotes[quote_id] = quote
        return {
            "status": quote["status"],
            "quote": {
                key: value
                for key, value in quote.items()
                if key not in {"used", "destination_postal_code"}
            },
            "message": (
                "The seller accepts the requested price and delivery terms."
                if quote["status"] == "accepted"
                else "The seller cannot accept all requested terms and has returned a counteroffer."
            ),
        }

    def _request_default_discount(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("item_id")
        quantity = payload.get("quantity")
        postal_code = payload.get("destination_postal_code")
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 256
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 1
            or not isinstance(postal_code, str)
            or not re.fullmatch(r"\d{5}", postal_code)
        ):
            return {
                "status": "error",
                "error": {
                    "code": "invalid_discount_request",
                    "message": "A product, positive quantity, and 5-digit destination ZIP are required.",
                },
            }

        product = self.catalog.get_by_variant_id(item_id)
        if product is None or not product["available"]:
            return {
                "status": "error",
                "error": {
                    "code": "product_not_available",
                    "message": "The requested product is not available in this marketplace.",
                },
            }
        if quantity > product["inventory"]:
            return {
                "status": "error",
                "error": {
                    "code": "insufficient_inventory",
                    "message": "The requested quantity is not available.",
                },
            }

        discount_percent = self.seller_policy.default_discount_percent(product["upc"])
        if discount_percent is None:
            return {
                "status": "no_offer",
                "message": "No published discount offer is listed for this product.",
            }
        try:
            delivery_fee = self.seller_policy.delivery_fee(quantity, postal_code)
        except SellerPolicyError:
            return {
                "status": "error",
                "error": {
                    "code": "seller_unable_to_quote",
                    "message": "The seller cannot quote delivery to this destination.",
                },
            }

        unit_price = (
            product["price_amount"] * (100 - discount_percent) + 50
        ) // 100
        quote_id = f"quote-{uuid4().hex}"
        quote = {
            "quote_id": quote_id,
            "item_id": product["variant_id"],
            "upc": product["upc"],
            "quantity": quantity,
            "unit_price": unit_price,
            "delivery_fee_amount": delivery_fee,
            "total_amount": unit_price * quantity + delivery_fee,
            "currency": product["currency"],
            "wait_days": 0,
            "destination_postal_code": postal_code,
            "free_delivery": delivery_fee == 0,
            "discount_percent": discount_percent,
            "status": "offered",
            "delivery_request_accepted": True,
            "used": False,
        }
        self.negotiation_quotes[quote_id] = quote
        return {
            "status": "offered",
            "quote": {
                key: value
                for key, value in quote.items()
                if key not in {"used", "destination_postal_code"}
            },
            "message": "The seller offers the published discount for this product.",
        }

    def _schedule_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation")
        if operation == "propose":
            return self._propose_schedule(payload)
        if operation == "accept":
            return self._accept_schedule(payload)
        if operation == "confirm":
            return self._confirm_schedule(payload)
        if operation == "release":
            return self._release_schedule(payload)
        return {
            "status": "error",
            "error": {
                "code": "invalid_schedule_operation",
                "message": "Schedule operation must be propose or confirm.",
            },
        }

    def _propose_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not smtp_configuration_available():
            return self._schedule_error(
                "confirmation_email_unavailable",
                "The seller must configure SMTP before scheduling an order "
                "that requires a final email confirmation.",
            )
        quote_id = payload.get("quote_id")
        fulfillment_type = payload.get("fulfillment_type")
        if (
            not isinstance(quote_id, str)
            or not isinstance(fulfillment_type, str)
            or fulfillment_type not in {"delivery", "pickup"}
        ):
            return self._schedule_error(
                "invalid_schedule_request",
                "A valid accepted price quote and delivery or pickup selection are required.",
            )
        quote = self.negotiation_quotes.get(quote_id)
        if quote is None or quote.get("used"):
            return self._schedule_error("invalid_price_quote", "The accepted price quote is not available.")

        earliest_date = self.schedule_config.local_today() + timedelta(
            days=max(1, quote["wait_days"] if fulfillment_type == "delivery" else 0)
        )
        if fulfillment_type == "delivery":
            preferred_text = payload.get("preferred_datetime")
            delivery_address = payload.get("delivery_address")
            if (
                not isinstance(preferred_text, str)
                or not isinstance(delivery_address, str)
                or not delivery_address.strip()
                or len(delivery_address) > 500
            ):
                return self._schedule_error(
                    "invalid_schedule_request",
                    "Delivery scheduling requires a preferred local date/time and delivery address.",
                )
            try:
                preferred = datetime.fromisoformat(preferred_text)
            except ValueError:
                return self._schedule_error(
                    "invalid_schedule_datetime",
                    "Preferred date/time must use YYYY-MM-DDTHH:MM local time.",
                )
            if preferred.tzinfo is not None or not earliest_date <= preferred.date() <= (
                self.schedule_config.local_today() + timedelta(days=90)
            ):
                return self._schedule_error(
                    "schedule_out_of_range",
                    "Delivery date must honor the agreed wait and be within the next 90 days.",
                )
        else:
            preferred = datetime.combine(earliest_date, self.schedule_config.open_time)
            delivery_address = self.schedule_config.marketplace_address

        unavailable_slots = self._reserved_schedule_slots()
        offered = self.schedule_config.nearest_slot(
            preferred,
            earliest_date,
            unavailable_slots,
        )
        if offered is None:
            return self._schedule_error("no_available_schedule", "The seller has no available appointment slots.")
        if fulfillment_type == "pickup":
            status = "auto_assigned"
        else:
            status = "available" if offered == preferred else "countered"

        schedule_id = f"schedule-{uuid4().hex}"
        offer = {
            "schedule_id": schedule_id,
            "quote_id": quote_id,
            "fulfillment_type": fulfillment_type,
            "preferred_datetime": preferred.isoformat(timespec="minutes"),
            "offered_datetime": offered.isoformat(timespec="minutes"),
            "delivery_address": delivery_address.strip(),
            "status": status,
            "hold_expires_at": self._schedule_hold_expiry().isoformat(),
        }
        self.schedule_offers[schedule_id] = offer
        response: dict[str, Any] = {
            key: value for key, value in offer.items() if key != "delivery_address"
        }
        response["timezone"] = self.schedule_config.timezone
        response["hold_expires_at"] = offer["hold_expires_at"]
        response["message"] = (
            "The seller assigned the next available pickup appointment."
            if fulfillment_type == "pickup"
            else (
                "The seller can deliver at the requested date and time."
                if status == "available"
                else "The requested time is unavailable; the seller proposes the nearest available appointment."
            )
        )
        if fulfillment_type == "pickup":
            response["pickup_address"] = self.schedule_config.marketplace_address
        return response

    def _accept_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        schedule_id = payload.get("schedule_id")
        offer = self.schedule_offers.get(schedule_id) if isinstance(schedule_id, str) else None
        if offer is None or offer.get("status") not in {"available", "countered"}:
            return self._schedule_error(
                "invalid_schedule_offer",
                "The delivery appointment offer cannot be accepted.",
            )
        offered = datetime.fromisoformat(offer["offered_datetime"])
        if offered in self._reserved_schedule_slots(exclude_schedule_id=schedule_id):
            quote = self.negotiation_quotes[offer["quote_id"]]
            earliest_date = self.schedule_config.local_today() + timedelta(
                days=max(1, quote["wait_days"])
            )
            alternative = self.schedule_config.nearest_slot(
                offered,
                earliest_date,
                self._reserved_schedule_slots(exclude_schedule_id=schedule_id),
            )
            if alternative is None:
                return self._schedule_error(
                    "no_available_schedule",
                    "The seller has no available appointment slots.",
                )
            offer["preferred_datetime"] = offer["offered_datetime"]
            offer["offered_datetime"] = alternative.isoformat(timespec="minutes")
            offer["status"] = "countered"
            offer["hold_expires_at"] = self._schedule_hold_expiry().isoformat()
            return {
                "status": "countered",
                "schedule_id": schedule_id,
                "offered_datetime": offer["offered_datetime"],
                "timezone": self.schedule_config.timezone,
                "hold_expires_at": offer["hold_expires_at"],
                "message": "That slot was just taken; the seller proposes the nearest available appointment.",
            }

        offer["status"] = "locked"
        offer["hold_expires_at"] = self._schedule_hold_expiry().isoformat()
        return {
            "status": "locked",
            "schedule_id": schedule_id,
            "offered_datetime": offer["offered_datetime"],
            "timezone": self.schedule_config.timezone,
            "hold_expires_at": offer["hold_expires_at"],
        }

    def _confirm_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        schedule_id = payload.get("schedule_id")
        checkout_id = payload.get("checkout_id")
        email = payload.get("email")
        phone = payload.get("phone")
        offer = self.schedule_offers.get(schedule_id) if isinstance(schedule_id, str) else None
        session = self.checkout_sessions.get(checkout_id) if isinstance(checkout_id, str) else None
        if offer is None or offer.get("status") == "confirmed":
            return self._schedule_error(
                "invalid_or_used_schedule",
                "The schedule offer is missing or has already been confirmed.",
            )
        if offer.get("status") not in {"locked", "auto_assigned"}:
            return self._schedule_error(
                "schedule_not_locked",
                "The buyer must accept the appointment before it can be confirmed.",
            )
        if not self._schedule_hold_is_active(offer):
            offer["status"] = "expired"
            return self._schedule_error(
                "schedule_hold_expired",
                "The appointment hold expired; request a new schedule offer.",
            )
        if (
            session is None
            or session.get("status") != "completed"
            or session.get("negotiation_id") != offer["quote_id"]
        ):
            return self._schedule_error(
                "payment_not_completed",
                "The matching checkout must be paid before the appointment is confirmed.",
            )
        if checkout_id in self.delivery_schedules:
            return self._schedule_error("schedule_already_confirmed", "This checkout already has a confirmed appointment.")
        if (
            not isinstance(email, str)
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or not isinstance(phone, str)
            or not re.fullmatch(r"[0-9+(). -]{7,30}", phone)
        ):
            return self._schedule_error("invalid_contact_details", "A valid buyer email and phone are required.")

        quote = self.negotiation_quotes[offer["quote_id"]]
        line_items = session.get("line_items")
        totals = session.get("totals")
        if (
            not isinstance(line_items, list)
            or len(line_items) != 1
            or not isinstance(line_items[0], dict)
            or not isinstance(line_items[0].get("item"), dict)
            or not isinstance(totals, list)
        ):
            return self._schedule_error(
                "invalid_paid_checkout",
                "The paid checkout does not contain a valid negotiated order.",
            )
        line = line_items[0]
        item = line["item"]
        total_by_type = {
            entry.get("type"): entry.get("amount")
            for entry in totals
            if isinstance(entry, dict)
        }
        fulfillment_type = offer["fulfillment_type"]
        if (
            not isinstance(item.get("title"), str)
            or not isinstance(item.get("price"), int)
            or not isinstance(line.get("quantity"), int)
            or any(
                isinstance(total_by_type.get(key), bool)
                or not isinstance(total_by_type.get(key), int)
                for key in ("subtotal", "delivery", "total")
            )
        ):
            return self._schedule_error(
                "invalid_paid_checkout",
                "The paid checkout is missing valid order totals.",
            )
        delivery_terms = session.get("delivery_terms")
        if not isinstance(delivery_terms, dict):
            return self._schedule_error(
                "invalid_paid_checkout",
                "The paid checkout is missing agreed delivery terms.",
            )
        order_confirmation = {
            "checkout_id": checkout_id,
            "product_title": item["title"],
            "upc": quote["upc"],
            "quantity": line["quantity"],
            "unit_price": item["price"],
            "subtotal_amount": total_by_type["subtotal"],
            "delivery_fee_amount": total_by_type["delivery"],
            "total_amount": total_by_type["total"],
            "fulfillment_type": fulfillment_type,
            "appointment_datetime": offer["offered_datetime"],
            "timezone": self.schedule_config.timezone,
            "address": (
                self.schedule_config.marketplace_address
                if fulfillment_type == "pickup"
                else offer["delivery_address"]
            ),
            "wait_days": delivery_terms["wait_days"],
            "free_delivery": delivery_terms["free_delivery"],
        }
        try:
            send_order_confirmation_email(
                buyer_email=email,
                order_confirmation=order_confirmation,
            )
        except (OSError, smtplib.SMTPException, ValueError):
            return self._schedule_error(
                "confirmation_email_failed",
                "The seller could not send the order confirmation email. "
                "Check the seller SMTP configuration and retry the A2A confirmation.",
            )

        schedule = {
            "checkout_id": checkout_id,
            "schedule_id": schedule_id,
            "status": "confirmed",
            "fulfillment_type": fulfillment_type,
            "appointment_datetime": offer["offered_datetime"],
            "timezone": self.schedule_config.timezone,
            "address": (
                self.schedule_config.marketplace_address
                if fulfillment_type == "pickup"
                else offer["delivery_address"]
            ),
            "email": email,
            "phone": phone,
            "order_confirmation": order_confirmation,
        }
        self.delivery_schedules[checkout_id] = schedule
        offer["status"] = "confirmed"
        return {
            **{
                key: value
                for key, value in schedule.items()
                if key not in {"email", "phone"}
            },
            "notice": (
                "Mock pickup appointment confirmed; no physical goods were reserved."
                if fulfillment_type == "pickup"
                else "Mock delivery appointment confirmed; no carrier was booked."
            ),
        }

    def _release_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        schedule_id = payload.get("schedule_id")
        offer = self.schedule_offers.get(schedule_id) if isinstance(schedule_id, str) else None
        if offer is None or offer.get("status") not in {"locked", "auto_assigned"}:
            return self._schedule_error(
                "invalid_schedule_hold",
                "There is no active appointment hold to release.",
            )
        offer["status"] = "released"
        return {"status": "released", "schedule_id": schedule_id}

    def _reserved_schedule_slots(
        self,
        *,
        exclude_schedule_id: str | None = None,
    ) -> set[datetime]:
        reserved: set[datetime] = set()
        for schedule_id, offer in self.schedule_offers.items():
            if schedule_id == exclude_schedule_id:
                continue
            if offer.get("status") not in {"locked", "auto_assigned", "confirmed"}:
                continue
            if offer["status"] != "confirmed" and not self._schedule_hold_is_active(offer):
                offer["status"] = "expired"
                continue
            reserved.add(datetime.fromisoformat(offer["offered_datetime"]))
        return reserved

    @staticmethod
    def _schedule_hold_expiry() -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=30)

    @staticmethod
    def _schedule_hold_is_active(offer: dict[str, Any]) -> bool:
        if offer.get("status") == "confirmed":
            return True
        expiry = offer.get("hold_expires_at")
        if not isinstance(expiry, str):
            return False
        try:
            return datetime.fromisoformat(expiry) > datetime.now(timezone.utc)
        except ValueError:
            return False

    @staticmethod
    def _schedule_error(code: str, message: str) -> dict[str, Any]:
        return {"status": "error", "error": {"code": code, "message": message}}

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancellation is not supported.")


def build_app(
    *,
    catalog: DuckDBCatalog | None = None,
    store_url: str = "http://127.0.0.1:5555",
    seller_policy: SellerNegotiationPolicy | None = None,
    seller_schedule_config: SellerScheduleConfig | None = None,
) -> Starlette:
    catalog = catalog or DuckDBCatalog(DEFAULT_DB_PATH)
    checkout_sessions: dict[str, dict[str, Any]] = {}
    negotiation_quotes: dict[str, dict[str, Any]] = {}
    schedule_offers: dict[str, dict[str, Any]] = {}
    delivery_schedules: dict[str, dict[str, Any]] = {}
    seller_policy = seller_policy or SellerNegotiationPolicy.load(DEFAULT_SELLER_POLICY_PATH)
    seller_schedule_config = seller_schedule_config or SellerScheduleConfig.load(
        DEFAULT_SELLER_CONFIG_PATH
    )
    stripe_gateway = StripeTestGateway.from_environment()
    enable_test_payment_helper = os.environ.get("STRIPE_ENABLE_TEST_HELPER") == "1"

    async def ucp_profile(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ucp": {
                    "version": UCP_VERSION,
                    "services": {
                        "dev.ucp.shopping": [
                            {
                                "version": UCP_VERSION,
                                "spec": f"https://ucp.dev/{UCP_VERSION}/specification/overview",
                                "transport": "rest",
                                "schema": (
                                    f"https://ucp.dev/{UCP_VERSION}/services/shopping/rest.openapi.json"
                                ),
                                "endpoint": f"{store_url}/ucp",
                            }
                        ]
                    },
                    "capabilities": {
                        CATALOG_SEARCH: [
                            {
                                "version": UCP_VERSION,
                                "spec": (
                                    f"https://ucp.dev/{UCP_VERSION}/specification/"
                                    "shopping/catalog/search"
                                ),
                                "schema": (
                                    f"https://ucp.dev/{UCP_VERSION}/schemas/"
                                    "shopping/catalog_search.json"
                                ),
                            }
                        ],
                        CHECKOUT: [
                            {
                                "version": UCP_VERSION,
                                "spec": (
                                    f"https://ucp.dev/{UCP_VERSION}/specification/shopping/checkout"
                                ),
                                "schema": (
                                    f"https://ucp.dev/{UCP_VERSION}/schemas/"
                                    "shopping/checkout.json"
                                ),
                            }
                        ],
                    },
                    "payment_handlers": _ucp_meta(stripe_gateway)["payment_handlers"],
                }
            }
        )

    async def storefront(request: Request) -> HTMLResponse:
        query = request.query_params.get("q", "")
        if len(query) > 2_000:
            return HTMLResponse("Search query is too long.", status_code=400)
        products = catalog.search(query, limit=50)
        cards = "".join(
            "<article class=\"product-card\">"
            f"<img src=\"/images/{html.escape(product['upc'], quote=True)}\" "
            f"alt=\"{html.escape(product['title'], quote=True)}\" loading=\"lazy\">"
            "<div class=\"product-details\">"
            f"<p class=\"category\">{html.escape(product['category'])}</p>"
            f"<h2>{html.escape(product['title'])}</h2>"
            f"<p class=\"brand\">{html.escape(product['brand'])}</p>"
            f"<p>{html.escape(product['description'])}</p>"
            f"<p class=\"price\">${product['price_amount'] / 100:.2f} USD</p>"
            f"<p class=\"upc\">UPC-A: {html.escape(product['upc'])}</p>"
            f"<p class=\"attribution\">Photo: "
            f"<a href=\"{html.escape(product['image_source_url'], quote=True)}\" "
            "target=\"_blank\" rel=\"noopener noreferrer\">Open Food Facts</a> "
            f"contributors, {html.escape(product['image_license'])}. "
            f"<a href=\"{html.escape(product['product_source_url'], quote=True)}\" "
            "target=\"_blank\" rel=\"noopener noreferrer\">Product record</a></p>"
            "</div></article>"
            for product in products
        )
        result_message = (
            f"{len(products)} product{'s' if len(products) != 1 else ''}"
            if not query
            else f"{len(products)} result{'s' if len(products) != 1 else ''} for "
            f"&ldquo;{html.escape(query)}&rdquo;"
        )
        return HTMLResponse(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>A2A Mock Market</title><style>"
            "body{margin:0;background:#f4f6f8;color:#18212b;font:16px/1.5 system-ui,sans-serif}"
            "header{background:#123d36;color:#fff;padding:2rem max(1.25rem,calc((100% - 1120px)/2))}"
            "header h1{margin:0;font-size:2rem}header p{margin:.5rem 0 0;color:#d9ebe5}"
            "main{max-width:1120px;margin:2rem auto;padding:0 1.25rem}"
            "form{display:flex;gap:.5rem;margin-bottom:1rem}"
            "input{flex:1;min-width:0;padding:.75rem;border:1px solid #bbc5cc;border-radius:.5rem;font:inherit}"
            "button{padding:.75rem 1rem;border:0;border-radius:.5rem;background:#176b58;color:#fff;font:inherit;cursor:pointer}"
            ".results{color:#53616c}.catalog{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}"
            ".product-card{overflow:hidden;background:#fff;border:1px solid #e0e5e9;border-radius:.75rem;box-shadow:0 2px 8px #1830400b}"
            ".product-card img{width:100%;height:220px;object-fit:contain;background:#fff}"
            ".product-details{padding:1rem}.product-details h2{margin:.15rem 0;font-size:1.15rem}"
            ".product-details p{margin:.45rem 0}.category,.brand,.upc,.attribution{font-size:.875rem;color:#53616c}"
            ".price{font-weight:700;font-size:1.15rem;color:#176b58}.attribution{border-top:1px solid #e6eaed;padding-top:.7rem}"
            "a{color:#176b58}footer{max-width:1120px;margin:2rem auto;padding:0 1.25rem 2rem;color:#53616c;font-size:.875rem}"
            "</style></head><body><header><h1>A2A Mock Market</h1>"
            "<p>Test storefront for A2A and UCP product discovery. Prices and stock are illustrative.</p>"
            "</header><main><form method=\"get\" action=\"/\">"
            f"<input type=\"search\" name=\"q\" value=\"{html.escape(query, quote=True)}\" "
            "placeholder=\"Search products, brands, or UPC-A\" aria-label=\"Search products\">"
            "<button type=\"submit\">Search</button></form>"
            f"<p class=\"results\">{result_message}</p>"
            f"<section class=\"catalog\" aria-label=\"Products\">{cards}</section>"
            + (
                "<p>No products matched your search.</p>"
                if not products
                else ""
            )
            + "</main><footer>Product data and photos are from "
            "<a href=\"https://world.openfoodfacts.org/\" target=\"_blank\" "
            "rel=\"noopener noreferrer\">Open Food Facts</a>. Photos are attributed "
            "to Open Food Facts contributors under CC BY-SA 3.0. See the "
            "<a href=\"https://world.openfoodfacts.org/terms-of-use\" target=\"_blank\" "
            "rel=\"noopener noreferrer\">Open Food Facts terms</a> for image-rights "
            "caveats. This store has no card-entry form. Agent purchases are test "
            "mode only and are not fulfilled.</footer>"
            "</body></html>"
        )

    async def product_image(request: Request) -> Response:
        product = catalog.get_by_upc(request.path_params["upc"])
        if product is None:
            return JSONResponse({"error": "product_not_found"}, status_code=404)
        image_path = (PROJECT_ROOT / product["image_path"]).resolve()
        if not image_path.is_relative_to(DEFAULT_IMAGE_DIR.resolve()):
            return JSONResponse({"error": "invalid_catalog_image_path"}, status_code=500)
        if not image_path.is_file():
            return JSONResponse({"error": "catalog_image_missing"}, status_code=500)
        return FileResponse(image_path, media_type="image/jpeg")

    async def list_products(request: Request) -> JSONResponse:
        query = request.query_params.get("query", "")
        products = catalog.search(query, limit=50)
        return JSONResponse({"products": products, "count": len(products)})

    async def product_by_upc(request: Request) -> Response:
        product = catalog.get_by_upc(request.path_params["upc"])
        if product is None:
            return JSONResponse({"error": "product_not_found"}, status_code=404)
        return JSONResponse(product)

    async def ucp_catalog_search(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "request_must_be_object"}, status_code=400)
        query = payload.get("query", "")
        filters = payload.get("filters", {})
        if not isinstance(query, str) or len(query) > 2_000:
            return JSONResponse({"error": "invalid_query"}, status_code=400)
        pagination = payload.get("pagination", {})
        requested_limit = pagination.get("limit", 10) if isinstance(pagination, dict) else None
        catalog_browse = (
            not query.strip()
            and isinstance(pagination, dict)
            and not isinstance(requested_limit, bool)
            and isinstance(requested_limit, int)
            and requested_limit > 0
        )
        if not query.strip() and (not isinstance(filters, dict) or not filters) and not catalog_browse:
            return JSONResponse({"error": "query_or_filters_required"}, status_code=400)

        results = catalog.search(query, limit=50)
        if isinstance(filters, dict):
            categories = filters.get("categories")
            if isinstance(categories, list) and all(isinstance(item, str) for item in categories):
                results = [item for item in results if item["category"] in categories]
            price_filter = filters.get("price")
            if isinstance(price_filter, dict):
                minimum = price_filter.get("min")
                maximum = price_filter.get("max")
                if isinstance(minimum, dict) and isinstance(minimum.get("amount"), int):
                    results = [
                        item for item in results
                        if item["price_amount"] >= minimum["amount"]
                        and minimum.get("currency", item["currency"]) == item["currency"]
                    ]
                if isinstance(maximum, dict) and isinstance(maximum.get("amount"), int):
                    results = [
                        item for item in results
                        if item["price_amount"] <= maximum["amount"]
                        and maximum.get("currency", item["currency"]) == item["currency"]
                    ]

        limit = requested_limit if isinstance(pagination, dict) else 10
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return JSONResponse({"error": "invalid_pagination_limit"}, status_code=400)
        limit = min(limit, 50)
        cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit():
                return JSONResponse({"error": "invalid_pagination_cursor"}, status_code=400)
            offset = int(cursor)
        page = results[offset : offset + limit]
        next_offset = offset + len(page)
        has_next_page = next_offset < len(results)
        response: dict[str, Any] = {
            "ucp": _ucp_meta(),
            "products": [_ucp_product(item, store_url) for item in page],
            "pagination": {"has_next_page": has_next_page},
        }
        if has_next_page:
            response["pagination"]["cursor"] = str(next_offset)
        return JSONResponse(response)

    async def create_checkout_session(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "request_must_be_object"}, status_code=400)
        line_items = payload.get("line_items")
        if not isinstance(line_items, list) or not line_items or len(line_items) > 50:
            return JSONResponse({"error": "invalid_line_items"}, status_code=400)
        quote_id = payload.get("negotiation_id")
        quote = negotiation_quotes.get(quote_id) if isinstance(quote_id, str) else None
        fulfillment_type = payload.get("fulfillment_type", "delivery")
        if not isinstance(fulfillment_type, str) or fulfillment_type not in {"delivery", "pickup"}:
            return JSONResponse({"error": "invalid_fulfillment_type"}, status_code=400)
        if quote_id is not None and (
            quote is None or quote.get("used") or len(line_items) != 1
        ):
            return JSONResponse({"error": "invalid_or_used_negotiation"}, status_code=409)

        checkout_lines: list[dict[str, Any]] = []
        subtotal = 0
        for index, line in enumerate(line_items):
            if not isinstance(line, dict) or not isinstance(line.get("item"), dict):
                return JSONResponse({"error": "invalid_line_item"}, status_code=400)
            variant_id = line["item"].get("id")
            quantity = line.get("quantity", 1)
            if (
                not isinstance(variant_id, str)
                or isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity < 1
            ):
                return JSONResponse({"error": "invalid_line_item"}, status_code=400)
            product = catalog.get_by_variant_id(variant_id)
            if product is None:
                return JSONResponse(
                    {
                        "ucp": {**_ucp_meta(), "status": "error"},
                        "messages": [
                            {
                                "type": "error",
                                "code": "not_found",
                                "content": f"Unknown mock product ID: {variant_id}",
                            }
                        ],
                    },
                    status_code=404,
                )
            if quantity > product["inventory"]:
                return JSONResponse(
                    {
                        "ucp": {**_ucp_meta(), "status": "error"},
                        "messages": [
                            {
                                "type": "error",
                                "code": "out_of_stock",
                                "content": f"Requested quantity is unavailable for {product['title']}.",
                            }
                        ],
                    },
                    status_code=409,
                )
            unit_price = product["price_amount"]
            if quote is not None:
                if (
                    quote["item_id"] != variant_id
                    or quote["quantity"] != quantity
                ):
                    return JSONResponse({"error": "negotiation_item_mismatch"}, status_code=409)
                unit_price = quote["unit_price"]
            line_total = unit_price * quantity
            subtotal += line_total
            checkout_lines.append(
                {
                    "id": f"line-{index + 1}",
                    "item": {
                        "id": product["variant_id"],
                        "title": product["title"],
                        "price": unit_price,
                    },
                    "quantity": quantity,
                    "totals": [
                        {"type": "subtotal", "amount": line_total},
                        {"type": "total", "amount": line_total},
                    ],
                }
            )

        delivery_fee = (
            quote["delivery_fee_amount"]
            if quote is not None and fulfillment_type == "delivery"
            else 0
        )
        total_amount = subtotal + delivery_fee
        checkout_id = f"mock-{uuid4().hex[:12]}"
        session = {
            "ucp": _ucp_meta(stripe_gateway),
            "id": checkout_id,
            "status": "incomplete",
            "currency": "USD",
            "line_items": checkout_lines,
            "totals": [
                {"type": "subtotal", "amount": subtotal},
                {"type": "delivery", "amount": delivery_fee},
                {"type": "total", "amount": total_amount},
            ],
            "delivery_terms": (
                {
                    "wait_days": quote["wait_days"],
                    "free_delivery": (
                        fulfillment_type == "pickup" or quote["free_delivery"]
                    ),
                    "fulfillment_type": fulfillment_type,
                }
                if quote is not None
                else None
            ),
            "fulfillment_type": fulfillment_type,
            "links": [
                {"type": "terms_of_service", "url": f"{store_url}/terms"},
                {"type": "privacy_policy", "url": f"{store_url}/privacy"},
            ],
            "messages": [
                {
                    "type": "info",
                    "code": "mock_checkout",
                    "content": (
                        "Stripe test mode is enabled. No real payment method or "
                        "live funds will be used."
                        if stripe_gateway
                        else "Mock session only. No payment or order will be created."
                    ),
                }
            ],
            "continue_url": f"{store_url}/checkout/{checkout_id}",
            "payment_intent_id": None,
            "negotiation_id": quote_id,
            "payment_attempt": 0,
            "active_idempotency_key": None,
            "active_token_hash": None,
        }
        checkout_sessions[checkout_id] = session
        if quote is not None:
            quote["used"] = True
        return JSONResponse(_public_checkout(session), status_code=201)

    async def create_test_payment_instrument(request: Request) -> JSONResponse:
        if stripe_gateway is None or not enable_test_payment_helper:
            return JSONResponse({"error": "test_payment_helper_disabled"}, status_code=404)
        if not _request_is_loopback(request):
            return JSONResponse({"error": "test_payment_helper_local_only"}, status_code=404)
        checkout_id = request.path_params["checkout_id"]
        session = checkout_sessions.get(checkout_id)
        if session is None:
            return JSONResponse({"error": "checkout_not_found"}, status_code=404)
        if session["status"] != "incomplete":
            return JSONResponse({"error": "checkout_not_payable"}, status_code=409)
        try:
            token = await stripe_gateway.create_test_spt(
                session["totals"][-1]["amount"],
                session["currency"],
            )
        except StripePaymentError as exc:
            return JSONResponse(
                {
                    "error": exc.code,
                    "message": str(exc),
                },
                status_code=502,
            )
        return JSONResponse(
            {
                "payment": {
                    "instruments": [
                        {
                            "id": f"instr_{checkout_id}",
                            "handler_id": "stripe_payments",
                            "type": "card",
                            "selected": True,
                            "credential": {
                                "type": "stripe_payment_token",
                                "token": token,
                            },
                        }
                    ]
                }
            }
        )

    async def complete_checkout(request: Request) -> JSONResponse:
        if stripe_gateway is None:
            return JSONResponse({"error": "stripe_test_mode_not_configured"}, status_code=503)
        checkout_id = request.path_params["checkout_id"]
        session = checkout_sessions.get(checkout_id)
        if session is None:
            return JSONResponse({"error": "checkout_not_found"}, status_code=404)
        if session["status"] == "completed":
            return JSONResponse(_public_checkout(session))
        if session["status"] == "processing":
            return JSONResponse({"error": "checkout_payment_in_progress"}, status_code=409)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "request_must_be_object"}, status_code=400)
        payment = payload.get("payment")
        instruments = payment.get("instruments") if isinstance(payment, dict) else None
        if not isinstance(instruments, list) or len(instruments) != 1:
            return JSONResponse({"error": "exactly_one_payment_instrument_required"}, status_code=400)
        instrument = instruments[0]
        credential = instrument.get("credential") if isinstance(instrument, dict) else None
        token = credential.get("token") if isinstance(credential, dict) else None
        if (
            not isinstance(instrument, dict)
            or instrument.get("handler_id") != "stripe_payments"
            or instrument.get("type") != "card"
            or instrument.get("selected") is not True
            or not isinstance(credential, dict)
            or credential.get("type") != "stripe_payment_token"
            or not isinstance(token, str)
            or not token.startswith("spt_")
            or len(token) > 512
        ):
            return JSONResponse({"error": "invalid_stripe_payment_instrument"}, status_code=400)
        if session["status"] not in {"incomplete", "payment_outcome_unknown"}:
            return JSONResponse({"error": "checkout_not_payable"}, status_code=409)

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if session["status"] == "payment_outcome_unknown":
            if token_hash != session["active_token_hash"]:
                return JSONResponse(
                    {
                        "error": "payment_outcome_unknown",
                        "message": "Retry with the same payment token to safely resolve this attempt.",
                    },
                    status_code=409,
                )
            idempotency_key = session["active_idempotency_key"]
        else:
            session["payment_attempt"] += 1
            idempotency_key = f"{checkout_id}-{session['payment_attempt']}"
            session["active_idempotency_key"] = idempotency_key
            session["active_token_hash"] = token_hash

        session["status"] = "processing"
        try:
            intent = await stripe_gateway.charge_spt(
                amount=session["totals"][-1]["amount"],
                currency=session["currency"],
                token=token,
                idempotency_key=idempotency_key,
                checkout_id=checkout_id,
            )
        except StripePaymentError as exc:
            session["status"] = "payment_outcome_unknown" if exc.retryable else "incomplete"
            if not exc.retryable:
                session["active_idempotency_key"] = None
                session["active_token_hash"] = None
            return JSONResponse(
                {
                    "ucp": {**_ucp_meta(stripe_gateway), "status": "error"},
                    "messages": [
                        {
                            "type": "error",
                            "code": "payment_failed",
                            "severity": "recoverable",
                            "content": str(exc),
                        }
                    ],
                    "stripe_error_code": exc.code,
                },
                status_code=402 if not exc.retryable else 503,
            )

        if intent.get("status") != "succeeded" or not isinstance(intent.get("id"), str):
            session["status"] = "incomplete"
            session["active_idempotency_key"] = None
            session["active_token_hash"] = None
            return JSONResponse(
                {
                    "ucp": {**_ucp_meta(stripe_gateway), "status": "error"},
                    "messages": [
                        {
                            "type": "error",
                            "code": "payment_failed",
                            "severity": "recoverable",
                            "content": (
                                "Stripe requires an additional payment action or "
                                "could not confirm this payment."
                            ),
                        }
                    ],
                },
                status_code=402,
            )

        session["status"] = "completed"
        session["payment_intent_id"] = intent["id"]
        session["active_idempotency_key"] = None
        session["active_token_hash"] = None
        session["messages"] = [
            {
                "type": "info",
                "code": "payment_succeeded",
                "content": "Stripe test payment succeeded. No real payment was taken.",
            }
        ]
        return JSONResponse(_public_checkout(session))

    async def checkout_page(request: Request) -> HTMLResponse:
        session = checkout_sessions.get(request.path_params["checkout_id"])
        if session is None:
            return HTMLResponse("<h1>Checkout session not found</h1>", status_code=404)
        items = "".join(
            f"<li>{html.escape(line['item']['title'])} "
            f"× {line['quantity']} — ${line['item']['price'] * line['quantity'] / 100:.2f}"
            f" (UPC {html.escape(line['item']['id'])})</li>"
            for line in session["line_items"]
        )
        amount = session["totals"][-1]["amount"] / 100
        return HTMLResponse(
            "<!doctype html><html><head><title>Mock checkout</title></head><body>"
            "<h1>Mock merchant checkout</h1>"
            f"<p>Checkout status: {html.escape(session['status'])}</p>"
            f"<ul>{items}</ul><p>Total: ${amount:.2f} USD</p>"
            f"<p>Session: {html.escape(session['id'])}</p>"
            "<p>Payments are accepted only in Stripe test mode; no real payment "
            "method or card details are entered here.</p></body></html>"
        )

    async def terms_page(request: Request) -> HTMLResponse:
        return HTMLResponse(
            "<h1>Mock store terms</h1>"
            "<p>Stripe sandbox payments are for testing only. This demonstration "
            "store does not accept live payments or fulfill real orders.</p>"
        )

    async def privacy_page(request: Request) -> HTMLResponse:
        return HTMLResponse(
            "<h1>Mock store privacy</h1>"
            "<p>This local demonstration does not collect or transmit personal data.</p>"
        )

    skill = AgentSkill(
        id="search_mock_catalog",
        name="Search mock product catalog",
        description=(
            "Searches this mock merchant's DuckDB catalog and returns product "
            "descriptions, prices, stock, and unique UPC identifiers."
        ),
        tags=["catalog", "products", "shopping", "upc"],
        examples=['{"query":"cereal"}'],
        input_modes=["text/plain"],
        output_modes=["application/json"],
    )
    negotiation_skill = AgentSkill(
        id="negotiate_product_price",
        name="Negotiate product price and delivery terms",
        description=(
            "Receives a buyer's proposed maximum unit price, quantity, requested "
            "delivery wait (0, 7, or 14 days), request for free delivery, and "
            "destination ZIP code. Buyers may also explicitly ask whether a "
            "published UPC-specific discount is available. Returns an accepted "
            "offer, published discount, or seller counteroffer and delivery fee "
            "without disclosing internal seller pricing policy."
        ),
        tags=["shopping", "price negotiation", "counteroffer", "delivery", "A2A"],
        examples=[
            '{"action":"negotiate_price","item_id":"049000028911","quantity":3,"target_unit_price":599,"wait_days":7,"request_free_delivery":true,"destination_postal_code":"94109"}',
            '{"action":"request_default_discount","item_id":"049000028911","quantity":1,"destination_postal_code":"94109"}',
        ],
        input_modes=["text/plain"],
        output_modes=["application/json"],
    )
    schedule_skill = AgentSkill(
        id="schedule_product_delivery",
        name="Schedule delivery or store pickup",
        description=(
            "Accepts a delivery or pickup scheduling request after price agreement. "
            "For delivery, confirms the requested local date and time when available "
            "or returns the nearest available counteroffer. For pickup, assigns the "
            "next available appointment without negotiation. Confirms the appointment "
            "over A2A after the buyer locks the slot and the matching checkout is paid. "
            "Sends the final item, price, fulfillment, appointment, and address confirmation "
            "to the buyer's email using the seller's configured SMTP service."
        ),
        tags=["shopping", "delivery scheduling", "pickup", "appointment", "A2A"],
        examples=[
            '{"action":"schedule_order","operation":"propose","quote_id":"quote_123","fulfillment_type":"delivery","preferred_datetime":"2026-11-09T10:00","delivery_address":"1499 California Street, San Francisco, CA 94109"}',
            '{"action":"schedule_order","operation":"propose","quote_id":"quote_123","fulfillment_type":"pickup"}',
        ],
        input_modes=["text/plain"],
        output_modes=["application/json"],
    )
    card = AgentCard(
        name="Mock UCP Merchant",
        description=(
            "A test-only ecommerce merchant exposing the same seeded DuckDB "
            "product catalog over A2A, UCP REST, and a read-only JSON HTTP API."
        ),
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=store_url,
                protocol_version="1.0",
            )
        ],
        skills=[skill, negotiation_skill, schedule_skill],
    )
    handler = DefaultRequestHandler(
        agent_executor=MockMerchantExecutor(
            catalog,
            store_url,
            seller_policy,
            negotiation_quotes,
            checkout_sessions,
            schedule_offers,
            delivery_schedules,
            seller_schedule_config,
        ),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/"),
            Route("/", storefront, methods=["GET"]),
            Route("/.well-known/ucp", ucp_profile, methods=["GET"]),
            Route("/healthz", lambda request: JSONResponse({"status": "ok"}), methods=["GET"]),
            Route("/api/products", list_products, methods=["GET"]),
            Route("/api/products/{upc:str}", product_by_upc, methods=["GET"]),
            Route("/images/{upc:str}", product_image, methods=["GET"]),
            Route("/ucp/catalog/search", ucp_catalog_search, methods=["POST"]),
            Route("/ucp/checkout-sessions", create_checkout_session, methods=["POST"]),
            Route(
                "/ucp/checkout-sessions/{checkout_id:str}/complete",
                complete_checkout,
                methods=["POST"],
            ),
            Route(
                "/ucp/test-payment-instruments/{checkout_id:str}",
                create_test_payment_instrument,
                methods=["POST"],
            ),
            Route("/checkout/{checkout_id:str}", checkout_page, methods=["GET"]),
            Route("/terms", terms_page, methods=["GET"]),
            Route("/privacy", privacy_page, methods=["GET"]),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local A2A/UCP mock merchant.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    store_url = os.environ.get("MOCK_STORE_URL", f"http://127.0.0.1:{args.port}")
    catalog = DuckDBCatalog(args.db)
    try:
        uvicorn.run(build_app(catalog=catalog, store_url=store_url), host=args.host, port=args.port)
    finally:
        catalog.close()


if __name__ == "__main__":
    main()
