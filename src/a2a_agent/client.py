import argparse
import asyncio
import json

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import display_agent_card, new_text_message
from a2a.types import Role, SendMessageRequest


async def run_client(
    url: str,
    message: str | None,
    streaming: bool,
    show_card: bool,
    store_url: str | None,
    goal: str,
    action: str,
    query: str | None,
    item_id: str | None,
    quantity: int,
) -> None:
    async with httpx.AsyncClient() as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=url)
        agent_card = await resolver.get_agent_card()

    if show_card:
        display_agent_card(agent_card)
        return

    client = await create_client(
        agent=agent_card,
        client_config=ClientConfig(streaming=streaming),
    )
    try:
        if store_url:
            payload: dict[str, object] = {
                "action": action,
                "store_url": store_url,
                "goal": goal,
            }
            if query is not None:
                payload["query"] = query
            if item_id is not None:
                payload["items"] = [{"item_id": item_id, "quantity": quantity}]
            request_text = json.dumps(payload)
        else:
            request_text = message or "Send --discover STORE_URL to inspect a store."
        request = SendMessageRequest(message=new_text_message(request_text, role=Role.ROLE_USER))
        async for event in client.send_message(request):
            print(event)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a message to an A2A agent.")
    parser.add_argument("message", nargs="?")
    parser.add_argument("--url", default="http://127.0.0.1:9999")
    parser.add_argument("--streaming", action="store_true")
    parser.add_argument("--show-card", action="store_true")
    store_actions = parser.add_mutually_exclusive_group()
    store_actions.add_argument(
        "--discover",
        metavar="STORE_URL",
        help="Discover this store's public A2A Agent Card and UCP profile.",
    )
    store_actions.add_argument(
        "--ucp-discover",
        metavar="STORE_URL",
        help="Inspect this store's public UCP profile.",
    )
    store_actions.add_argument(
        "--search",
        metavar="STORE_URL",
        help="Search this store's advertised UCP catalog (requires --query).",
    )
    store_actions.add_argument(
        "--checkout",
        metavar="STORE_URL",
        help="Prepare a UCP checkout session (requires --item-id).",
    )
    store_actions.add_argument(
        "--test-purchase",
        metavar="STORE_URL",
        help=(
            "Run an explicit local Stripe sandbox purchase using a Stripe test "
            "payment instrument (requires --item-id)."
        ),
    )
    parser.add_argument("--query", help="Product search query for --search.")
    parser.add_argument("--item-id", help="UCP product or variant ID for --checkout.")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument(
        "--goal",
        default="Identify advertised product, price, delivery, and scheduling capabilities.",
    )
    args = parser.parse_args()

    selected_url = (
        args.discover
        or args.ucp_discover
        or args.search
        or args.checkout
        or args.test_purchase
    )
    action = (
        "ucp_discover"
        if args.ucp_discover
        else "search"
        if args.search
        else "create_checkout"
        if args.checkout
        else "test_purchase"
        if args.test_purchase
        else "discover"
    )
    if args.search and not args.query:
        parser.error("--search requires --query.")
    if (args.checkout or args.test_purchase) and not args.item_id:
        parser.error("--checkout and --test-purchase require --item-id.")
    if args.query and not args.search:
        parser.error("--query can only be used with --search.")
    if args.item_id and not (args.checkout or args.test_purchase):
        parser.error("--item-id can only be used with --checkout or --test-purchase.")
    if args.quantity < 1:
        parser.error("--quantity must be a positive integer.")
    if args.quantity != 1 and not (args.checkout or args.test_purchase):
        parser.error("--quantity can only be used with --checkout or --test-purchase.")

    asyncio.run(
        run_client(
            url=args.url,
            message=args.message,
            streaming=args.streaming,
            show_card=args.show_card,
            store_url=selected_url,
            goal=args.goal,
            action=action,
            query=args.query,
            item_id=args.item_id,
            quantity=args.quantity,
        )
    )


if __name__ == "__main__":
    main()
