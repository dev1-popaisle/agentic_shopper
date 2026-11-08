import json
import os
from pathlib import Path
import re
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPENROUTER_KEY_FILE = PROJECT_ROOT / "Config" / "OR_API_Key_1.rtf"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-6.1-terra-pro"
MAX_INTENT_LENGTH = 2_000


class OpenRouterError(Exception):
    pass


def load_api_key(key_file: Path = OPENROUTER_KEY_FILE) -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return _validate_api_key(key)

    try:
        contents = key_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise OpenRouterError(
            "OpenRouter API key is unavailable. Set OPENROUTER_API_KEY or configure "
            "the ignored Config/OR_API_Key_1.rtf file."
        ) from exc

    match = re.search(r"sk-or-v1-[A-Za-z0-9_-]{20,}", contents)
    if match is None:
        raise OpenRouterError(
            "No OpenRouter API key was found in Config/OR_API_Key_1.rtf."
        )
    return _validate_api_key(match.group(0))


def _validate_api_key(key: str) -> str:
    if not re.fullmatch(r"sk-or-v1-[A-Za-z0-9_-]{20,}", key):
        raise OpenRouterError("The configured OpenRouter API key has an invalid format.")
    return key


async def match_catalog_question(
    question: str,
    products: list[dict[str, Any]],
) -> list[str]:
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_INTENT_LENGTH:
        raise OpenRouterError("Shopping question must be between 1 and 2000 characters.")
    if not products or len(products) > 50:
        raise OpenRouterError("Marketplace catalog must contain between 1 and 50 products.")

    catalog: list[dict[str, Any]] = []
    valid_ids: set[str] = set()
    for product in products:
        item_id = product.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise OpenRouterError("Marketplace catalog contains an invalid product ID.")
        if product.get("available") is not True:
            continue
        valid_ids.add(item_id)
        catalog.append(
            {
                "item_id": item_id,
                "title": str(product.get("title", ""))[:300],
                "description": str(product.get("description", ""))[:500],
                "category": str(product.get("category", ""))[:120],
                "brand": str(product.get("brand", ""))[:120],
                "upc": str(product.get("upc", ""))[:32],
                "price_amount": product.get("price_amount"),
                "currency": str(product.get("currency", "USD"))[:8],
                "available": True,
            }
        )
    if not catalog:
        return []

    payload: dict[str, Any] = {
        "model": os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a product question matcher for one specific mock "
                    "marketplace. The supplied JSON is the complete, authoritative "
                    "catalog snapshot; it contains the only products you may discuss. "
                    "Match the user's shopping question only to products supported "
                    "by the supplied catalog fields. Do not answer unrelated or "
                    "general questions. Do not infer ingredients, features, stock, "
                    "delivery, or other facts not explicitly present. Do not suggest "
                    "products that are absent from this catalog. If no listed product "
                    "clearly matches, return an empty array. Return only JSON with "
                    'one field: "matching_item_ids", an array of exact item_id '
                    "strings copied from the catalog. No prose."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "shopping_question": question.strip(),
                        "available_catalog": catalog,
                    },
                    ensure_ascii=True,
                ),
            },
        ],
    }

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                OPENROUTER_API_URL,
                headers={
                    "Authorization": f"Bearer {load_api_key()}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://127.0.0.1",
                    "X-Title": "A2A Buyer Agent",
                },
                json=payload,
            )
    except OpenRouterError:
        raise
    except httpx.TimeoutException as exc:
        raise OpenRouterError("Timed out while contacting OpenRouter.") from exc
    except httpx.RequestError as exc:
        raise OpenRouterError("Could not contact OpenRouter.") from exc

    if not response.is_success:
        raise OpenRouterError(
            f"OpenRouter returned HTTP {response.status_code}; check the key, model, and account limits."
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned invalid JSON.") from exc

    try:
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        item_ids = parsed["matching_item_ids"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise OpenRouterError(
            "OpenRouter response did not contain valid matching_item_ids."
        ) from exc
    if (
        not isinstance(item_ids, list)
        or any(not isinstance(item_id, str) for item_id in item_ids)
        or len(item_ids) > len(valid_ids)
    ):
        raise OpenRouterError("OpenRouter returned invalid marketplace product matches.")
    if any(item_id not in valid_ids for item_id in item_ids):
        raise OpenRouterError(
            "OpenRouter returned a product ID outside the marketplace catalog."
        )
    return list(dict.fromkeys(item_ids))
