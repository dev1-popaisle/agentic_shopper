import asyncio
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
from httpcore import AsyncNetworkBackend, AsyncNetworkStream
from httpcore._backends.auto import AutoBackend


CARD_PATH = "/.well-known/agent-card.json"
MAX_CARD_BYTES = 1_000_000
MAX_SKILLS = 100
MAX_INTERFACES = 20
REQUEST_TIMEOUT_SECONDS = 8.0

CAPABILITY_KEYWORDS = {
    "product_search": ("product search", "catalog", "product discovery", "search products"),
    "price_quotes": ("quote", "pricing", "price", "cost estimate"),
    "price_negotiation": ("negotiat", "counteroffer", "counter-offer", "bargain"),
    "shipping_and_delivery": ("shipping", "delivery", "shipment", "fulfillment"),
    "delivery_scheduling": ("schedule", "scheduling", "time slot", "delivery window", "appointment"),
}


class DiscoveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_store_url(store_url: str) -> str:
    if len(store_url) > 2_048:
        raise DiscoveryError("invalid_url", "Store URL must be at most 2048 characters.")
    try:
        parsed = urlsplit(store_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryError("invalid_url", "Store URL is malformed.") from exc

    if not hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise DiscoveryError(
            "invalid_url",
            "Store URL must not contain credentials or a fragment.",
        )

    host = hostname.rstrip(".").lower()
    if os.environ.get("A2A_ALLOW_LOCAL_TEST_STORE") == "1":
        try:
            local_ip = ipaddress.ip_address(host)
        except ValueError:
            local_ip = None
        if (
            parsed.scheme.lower() == "http"
            and local_ip is not None
            and local_ip.is_loopback
            and port is not None
        ):
            authority = f"[{host}]" if local_ip.version == 6 else host
            return f"http://{authority}:{port}"

    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise DiscoveryError(
            "invalid_url",
            "Store URL must be HTTPS on the standard port, unless loopback testing is explicitly enabled.",
        )

    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise DiscoveryError("unsafe_url", "Local and internal hostnames are not allowed.")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise DiscoveryError("invalid_url", "Store hostname is invalid.") from exc
        authority = host
    else:
        if not literal_ip.is_global:
            raise DiscoveryError("unsafe_url", "Private or non-public IP addresses are not allowed.")
        authority = f"[{host}]" if literal_ip.version == 6 else host
    return f"https://{authority}"


class PublicOnlyNetworkBackend(AsyncNetworkBackend):
    """Resolve and connect only to globally routable addresses."""

    def __init__(self, *, allow_loopback: bool = False) -> None:
        self._backend = AutoBackend()
        self._resolved_addresses: dict[str, list[str]] = {}
        self._allow_loopback = allow_loopback

    async def resolve_public(self, host: str, port: int) -> list[str]:
        try:
            ip = ipaddress.ip_address(host)
            if self._allow_loopback and ip.is_loopback:
                self._resolved_addresses[host] = [str(ip)]
                return [str(ip)]
            addresses = [str(ip)]
        except ValueError:
            try:
                results = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(
                        host,
                        port,
                        type=socket.SOCK_STREAM,
                    ),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise DiscoveryError(
                    "discovery_timeout",
                    "Timed out while resolving the store hostname.",
                ) from exc
            except OSError as exc:
                raise DiscoveryError("dns_resolution_failed", "Could not resolve the store hostname.") from exc
            addresses = list(dict.fromkeys(item[4][0] for item in results))

        if not addresses:
            raise DiscoveryError("dns_resolution_failed", "No addresses were found for the store hostname.")
        if self._allow_loopback and any(
            not ipaddress.ip_address(address).is_loopback for address in addresses
        ):
            raise DiscoveryError(
                "unsafe_url",
                "Local test-store hostnames must resolve only to loopback addresses.",
            )
        if not self._allow_loopback and any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise DiscoveryError(
                "unsafe_url",
                "Store hostname resolves to a private or non-public network address.",
            )
        self._resolved_addresses[host] = addresses
        return addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> AsyncNetworkStream:
        addresses = self._resolved_addresses.get(host)
        if addresses is None:
            addresses = await self.resolve_public(host, port)

        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    host=address,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, OSError) as exc:
                last_error = exc
        raise OSError(f"Could not connect to validated store host {host}.") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> AsyncNetworkStream:
        raise DiscoveryError("unsafe_url", "Unix socket connections are not allowed.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class PublicHttpsTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, allow_loopback: bool = False) -> None:
        self.network_backend = PublicOnlyNetworkBackend(allow_loopback=allow_loopback)
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=self.network_backend,
            max_connections=4,
            max_keepalive_connections=0,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=str(request.url),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


async def discover_store(store_url: str, goal: str) -> dict[str, Any]:
    origin = validate_store_url(store_url)
    agent_card_url = f"{origin}{CARD_PATH}"
    parsed = urlsplit(origin)
    is_local = urlsplit(origin).scheme == "http"
    transport = PublicHttpsTransport(allow_loopback=is_local)
    await transport.network_backend.resolve_public(
        parsed.hostname or "",
        parsed.port or (80 if is_local else 443),
    )

    try:
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                agent_card_url,
                headers={"Accept": "application/json"},
            ) as response:
                if response.status_code == 404:
                    return _no_agent_card_result(store_url, goal)
                if response.status_code != 200:
                    raise DiscoveryError(
                        "discovery_failed",
                        f"Agent Card endpoint returned HTTP {response.status_code}.",
                    )

                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise DiscoveryError(
                        "invalid_agent_card",
                        "Agent Card endpoint did not return a JSON content type.",
                    )

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CARD_BYTES:
                        raise DiscoveryError(
                            "invalid_agent_card",
                            "Agent Card response exceeded the 1 MB size limit.",
                        )
    except DiscoveryError:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise DiscoveryError("discovery_timeout", "Timed out while fetching the Agent Card.") from exc
    except httpx.RequestError as exc:
        raise DiscoveryError("discovery_failed", "Could not fetch the Agent Card from this store.") from exc
    except OSError as exc:
        raise DiscoveryError("discovery_failed", "Could not resolve or connect to this store.") from exc

    try:
        card = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DiscoveryError("invalid_agent_card", "Agent Card response was not valid JSON.") from exc
    if not isinstance(card, dict) or not isinstance(card.get("name"), str):
        raise DiscoveryError("invalid_agent_card", "Response is not a valid A2A Agent Card.")

    return _summarize_agent_card(store_url, goal, card)


def _no_agent_card_result(store_url: str, goal: str) -> dict[str, Any]:
    return {
        "status": "no_a2a_agent_card_found",
        "store_url": store_url,
        "goal": goal,
        "a2a": {
            "status": "not_found",
            "checked_url": f"{urlsplit(store_url).scheme}://{urlsplit(store_url).netloc}{CARD_PATH}",
        },
        "mcp": {
            "status": "not_discovered",
            "reason": "A store website URL alone does not provide a standard MCP endpoint discovery mechanism.",
        },
        "next_step": "Check the store's developer documentation or provide a known A2A or MCP endpoint.",
    }


def _summarize_agent_card(
    store_url: str,
    goal: str,
    card: dict[str, Any],
) -> dict[str, Any]:
    skills = card.get("skills", [])
    interfaces = card.get("supportedInterfaces", card.get("supported_interfaces", []))
    if not isinstance(skills, list) or not isinstance(interfaces, list):
        raise DiscoveryError("invalid_agent_card", "Agent Card skills or interfaces were malformed.")

    skill_summaries: list[dict[str, Any]] = []
    evidence_text: list[tuple[str, str]] = []
    for skill in skills[:MAX_SKILLS]:
        if not isinstance(skill, dict):
            continue
        skill_id = _string_field(skill, "id") or "unspecified"
        name = _string_field(skill, "name")
        description = _string_field(skill, "description")
        tags = _string_list(skill.get("tags", []))
        examples = _string_list(skill.get("examples", []))
        input_modes = _string_list(skill.get("inputModes", skill.get("input_modes", [])))
        output_modes = _string_list(skill.get("outputModes", skill.get("output_modes", [])))
        searchable = " ".join([name, description, *tags, *examples]).casefold()
        evidence_text.append((skill_id, searchable))
        skill_summaries.append(
            {
                "id": skill_id,
                "name": name,
                "description": description,
                "tags": tags,
                "examples": examples,
                "input_modes": input_modes,
                "output_modes": output_modes,
            }
        )

    advertised: list[dict[str, Any]] = []
    for capability, keywords in CAPABILITY_KEYWORDS.items():
        evidence = [
            {"skill_id": skill_id, "matched_terms": [word for word in keywords if word in text]}
            for skill_id, text in evidence_text
            if any(word in text for word in keywords)
        ]
        if evidence:
            advertised.append({"capability": capability, "evidence": evidence})

    interface_summaries = []
    for interface in interfaces[:MAX_INTERFACES]:
        if isinstance(interface, dict):
            interface_summaries.append(
                {
                    "advertised_url": _string_field(interface, "url"),
                    "protocol_binding": _string_field(
                        interface, "protocolBinding", "protocol_binding"
                    ),
                    "protocol_version": _string_field(
                        interface, "protocolVersion", "protocol_version"
                    ),
                }
            )
    capabilities = card.get("capabilities", {})
    if not isinstance(capabilities, dict):
        capabilities = {}
    supported_capabilities = {
        key: value
        for key, value in capabilities.items()
        if key in {
            "streaming",
            "pushNotifications",
            "stateTransitionHistory",
            "extendedAgentCard",
        }
        and isinstance(value, bool)
    }

    return {
        "status": "a2a_agent_card_found",
        "store_url": store_url,
        "goal": goal,
        "a2a": {
            "status": "agent_card_found",
            "name": _string_field(card, "name"),
            "description": _string_field(card, "description"),
            "version": _string_field(card, "version"),
            "interfaces": interface_summaries,
            "protocol_capabilities": supported_capabilities,
            "skills": skill_summaries,
            "advertised_commerce_capabilities": advertised,
            "advertised_interface_urls_are_untrusted": True,
            "capability_assessment": (
                "Capabilities are inferred from Agent Card text only; the remote operations were not invoked or verified."
            ),
        },
        "mcp": {
            "status": "not_discovered",
            "reason": "A2A Agent Card discovery does not reveal MCP endpoints. MCP connection details must be published separately or supplied by the store.",
        },
        "safety": {
            "remote_agent_contacted": False,
            "orders_placed": False,
            "payments_made": False,
        },
        "next_step": "Review the advertised skills and obtain a store-supported endpoint and protocol details before attempting any commerce operation.",
    }


def _string_field(value: dict[str, Any], *names: str) -> str:
    for name in names:
        field = value.get(name)
        if isinstance(field, str):
            return field[:2_000]
    return ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:500] for item in value if isinstance(item, str)][:20]
