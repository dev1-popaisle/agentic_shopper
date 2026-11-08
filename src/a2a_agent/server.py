import argparse
import os

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette

from a2a_agent.agent_executor import ShoppingAgentExecutor


def build_app(agent_url: str) -> Starlette:
    skills = [
        AgentSkill(
            id="discover_commerce_capabilities",
            name="Discover commerce capabilities",
            description=(
                "Safely checks a store's public A2A Agent Card and UCP profile, "
                "reporting advertised capabilities without invoking remote A2A agents."
            ),
            tags=["shopping", "ecommerce", "discovery", "a2a", "ucp"],
            examples=['{"store_url":"https://shop.example","action":"discover"}'],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
        AgentSkill(
            id="search_ucp_catalog",
            name="Search a UCP product catalog",
            description=(
                "Searches a merchant's advertised UCP catalog using its supported "
                "REST capability. Results are not a price or availability guarantee."
            ),
            tags=["shopping", "ecommerce", "ucp", "catalog", "product search"],
            examples=[
                '{"store_url":"https://shop.example","action":"search","query":"blue running shoes"}'
            ],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
        AgentSkill(
            id="prepare_ucp_checkout",
            name="Prepare a UCP checkout session",
            description=(
                "Creates a merchant checkout session for selected catalog item IDs "
                "and returns the merchant response for user review. It does not "
                "submit payment or finalize an order."
            ),
            tags=["shopping", "ecommerce", "ucp", "checkout"],
            examples=[
                '{"store_url":"https://shop.example","action":"create_checkout","items":[{"item_id":"variant-123","quantity":1}]}'
            ],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
        AgentSkill(
            id="complete_ucp_checkout",
            name="Complete an authorized UCP checkout",
            description=(
                "Submits a buyer-authorized Stripe card Shared Payment Token (SPT) "
                "to the merchant's UCP checkout. Never accepts raw card details."
            ),
            tags=["shopping", "ecommerce", "ucp", "stripe", "payment", "spt"],
            examples=[
                '{"store_url":"https://shop.example","action":"complete_checkout","checkout_id":"chk_123","payment_instrument":{"id":"instr_1","handler_id":"stripe_payments","type":"card","selected":true,"credential":{"type":"stripe_payment_token","token":"spt_..."}}}'
            ],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
        AgentSkill(
            id="test_local_stripe_purchase",
            name="Test a local Stripe sandbox purchase",
            description=(
                "Creates a mock-store checkout and completes it with a Stripe "
                "test-helper SPT. Available only for explicitly enabled loopback "
                "test stores; it cannot charge a live payment method."
            ),
            tags=["shopping", "ecommerce", "ucp", "stripe", "sandbox", "test"],
            examples=[
                '{"store_url":"http://127.0.0.1:5555","action":"test_purchase","items":[{"item_id":"049000028911","quantity":1}]}'
            ],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
        AgentSkill(
            id="negotiate_buyer_price_and_delivery",
            name="Negotiate buyer price and delivery terms",
            description=(
                "The buyer agent can propose a maximum unit price, choose a "
                "delivery wait of 0, 7, or 14 days, request free delivery, and "
                "specify quantity and destination postal code. It can explicitly "
                "ask whether the merchant has a published UPC-specific discount "
                "before choosing whether to bargain. It can exchange A2A offers "
                "and counteroffers with the merchant, then ask the "
                "buyer to accept or make another offer. It does not disclose "
                "seller policy or authorize payment without buyer confirmation."
            ),
            tags=[
                "shopping",
                "price negotiation",
                "counteroffer",
                "delivery wait",
                "shipping",
                "A2A",
            ],
            examples=[
                '{"store_url":"http://127.0.0.1:5555","action":"negotiate_price","item_id":"049000028911","quantity":3,"target_unit_price":599,"wait_days":7,"request_free_delivery":true,"destination_postal_code":"94109"}'
            ],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
        AgentSkill(
            id="schedule_delivery_or_pickup",
            name="Arrange a delivery or pickup appointment",
            description=(
                "After a price quote is accepted, asks the merchant over A2A to "
                "confirm a requested delivery date and time or counter with its "
                "nearest available slot. For pickup, accepts the merchant's "
                "automatically assigned appointment. Locks an accepted slot with "
                "the merchant before payment. After payment, receives the seller's "
                "final order confirmation and email-notification status over A2A."
            ),
            tags=["shopping", "delivery scheduling", "store pickup", "A2A"],
            examples=[
                '{"store_url":"http://127.0.0.1:5555","action":"schedule_order","operation":"propose","quote_id":"quote-123","fulfillment_type":"delivery","preferred_datetime":"2026-11-09T10:00","delivery_address":"1499 California Street, San Francisco, CA 94109"}',
                '{"store_url":"http://127.0.0.1:5555","action":"schedule_order","operation":"propose","quote_id":"quote-123","fulfillment_type":"pickup"}',
            ],
            input_modes=["text/plain"],
            output_modes=["application/json"],
        ),
    ]
    agent_card = AgentCard(
        name="A2A UCP Shopping Agent",
        description=(
            "Discovers A2A and UCP capabilities, searches UCP merchant catalogs, "
            "prepares checkout sessions, and completes checkout only with a "
            "buyer-authorized Stripe payment token. Local Stripe sandbox purchases "
            "use Stripe test credentials and never charge live payment methods."
        ),
        version="0.3.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=agent_url,
                protocol_version="1.0",
            )
        ],
        skills=skills,
    )
    handler = DefaultRequestHandler(
        agent_executor=ShoppingAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    return Starlette(
        routes=[
            *create_agent_card_routes(agent_card),
            *create_jsonrpc_routes(handler, "/"),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the A2A UCP shopping agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()

    agent_url = os.environ.get(
        "A2A_PUBLIC_URL", f"http://127.0.0.1:{args.port}"
    )
    uvicorn.run(build_app(agent_url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
