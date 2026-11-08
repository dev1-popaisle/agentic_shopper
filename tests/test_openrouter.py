import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from a2a_agent.openrouter import (
    OPENROUTER_API_URL,
    DEFAULT_MODEL,
    OpenRouterError,
    load_api_key,
    match_catalog_question,
)


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_loads_key_from_rtf_without_echoing_it(self) -> None:
        key = "sk-or-v1-" + "a" * 40
        path = Path("/tmp/openrouter-test-key.rtf")
        with (
            patch.object(Path, "read_text", return_value=r"{\rtf1 OpenRouter key: " + key + "}"),
            patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}),
        ):
            self.assertEqual(load_api_key(path), key)

    def test_rejects_bad_key_format(self) -> None:
        with (
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "not-a-key"}),
            self.assertRaises(OpenRouterError),
        ):
            load_api_key()

    async def test_matches_only_ids_from_available_catalog(self) -> None:
        key = "sk-or-v1-" + "b" * 40
        request = httpx.Request("POST", OPENROUTER_API_URL)
        response = httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"matching_item_ids": ["variant-1"]}
                            )
                        }
                    }
                ]
            },
        )
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = response
        with (
            patch("a2a_agent.openrouter.load_api_key", return_value=key),
            patch("a2a_agent.openrouter.httpx.AsyncClient", return_value=client),
        ):
            matched = await match_catalog_question(
                "Do you have peanut butter?",
                [
                    {
                        "item_id": "variant-1",
                        "title": "Peanut butter",
                        "description": "Smooth peanut butter.",
                        "category": "Pantry",
                        "brand": "Example",
                        "upc": "012345678905",
                        "price_amount": 699,
                        "currency": "USD",
                        "available": True,
                    },
                    {
                        "item_id": "variant-2",
                        "title": "Out of stock cereal",
                        "available": False,
                    },
                ],
            )

        self.assertEqual(matched, ["variant-1"])
        kwargs = client.post.await_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {key}")
        self.assertEqual(kwargs["json"]["model"], DEFAULT_MODEL)
        self.assertEqual(DEFAULT_MODEL, "openai/gpt-6.1-terra-pro")
        sent_messages = kwargs["json"]["messages"]
        self.assertIn("Do you have peanut butter?", sent_messages[1]["content"])
        self.assertIn("Peanut butter", sent_messages[1]["content"])
        self.assertNotIn("Out of stock cereal", sent_messages[1]["content"])
        self.assertNotIn("delivery_address", str(kwargs["json"]))
        self.assertNotIn("phone", str(kwargs["json"]))

    async def test_openrouter_model_can_be_overridden(self) -> None:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", OPENROUTER_API_URL),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"matching_item_ids": ["v1"]})
                        }
                    }
                ]
            },
        )
        with (
            patch("a2a_agent.openrouter.load_api_key", return_value="sk-or-v1-" + "d" * 40),
            patch("a2a_agent.openrouter.httpx.AsyncClient", return_value=client),
            patch.dict("os.environ", {"OPENROUTER_MODEL": "openai/gpt-6.1-terra"}),
        ):
            await match_catalog_question(
                "oat milk",
                [{"item_id": "v1", "available": True, "title": "Oat milk"}],
            )

        self.assertEqual(client.post.await_args.kwargs["json"]["model"], "openai/gpt-6.1-terra")

    async def test_rejects_malformed_model_output(self) -> None:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", OPENROUTER_API_URL),
            json={"choices": [{"message": {"content": "not json"}}]},
        )
        with (
            patch("a2a_agent.openrouter.load_api_key", return_value="sk-or-v1-" + "c" * 40),
            patch("a2a_agent.openrouter.httpx.AsyncClient", return_value=client),
            self.assertRaisesRegex(OpenRouterError, "valid matching_item_ids"),
        ):
            await match_catalog_question(
                "Find peanut butter",
                [{"item_id": "v1", "available": True, "title": "Peanut butter"}],
            )

    async def test_rejects_model_product_id_not_in_catalog(self) -> None:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", OPENROUTER_API_URL),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"matching_item_ids": ["not-in-catalog"]}
                            )
                        }
                    }
                ]
            },
        )
        with (
            patch("a2a_agent.openrouter.load_api_key", return_value="sk-or-v1-" + "e" * 40),
            patch("a2a_agent.openrouter.httpx.AsyncClient", return_value=client),
            self.assertRaisesRegex(OpenRouterError, "outside the marketplace catalog"),
        ):
            await match_catalog_question(
                "buy an iPhone",
                [{"item_id": "v1", "available": True, "title": "Cereal"}],
            )


if __name__ == "__main__":
    unittest.main()
