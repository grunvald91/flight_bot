import asyncio
import json
import logging
import secrets
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

TUTU_MCP_URL = "https://mcp.tutu.ru/mcp"


def _decode_tool_result(result: Any) -> dict:
    if getattr(result, "isError", False):
        text = " ".join(
            str(getattr(item, "text", ""))
            for item in getattr(result, "content", [])
        ).strip()
        raise RuntimeError(text or "Tutu MCP returned an error")

    text = next(
        (
            getattr(item, "text", "")
            for item in getattr(result, "content", [])
            if getattr(item, "text", None)
        ),
        "",
    )
    if not text:
        raise RuntimeError("Tutu MCP returned an empty response")

    payload = json.loads(text)
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if isinstance(payload, dict) and "result" in payload:
        nested = payload["result"]
        return json.loads(nested) if isinstance(nested, str) else nested
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Tutu MCP response format")
    return payload


class TutuMCPClient:
    def __init__(self, url: str = TUTU_MCP_URL):
        self.url = url

    async def _call(self, tool_name: str, arguments: dict) -> dict:
        try:
            async with asyncio.timeout(90):
                async with streamable_http_client(self.url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments)
            return _decode_tool_result(result)
        except Exception:
            logger.exception("Tutu MCP call failed: %s", tool_name)
            raise

    async def search_avia(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        adults: int = 1,
        page_size: int = 5,
    ) -> dict:
        return await self._call(
            "search_avia",
            {
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "adults": adults,
                "children": 0,
                "infants": 0,
                "service_class": "Y",
                "page": 1,
                "page_size": page_size,
                "sort": "price_asc",
                "direct_only": False,
                "view": "compact",
            },
        )

    async def create_checkout_link(self, checkout_ref: dict) -> dict:
        return await self._call("create_checkout_link", dict(checkout_ref))


class CheckoutRefCache:
    def __init__(self, ttl_seconds: int = 1800):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, dict]] = {}

    def put(self, checkout_ref: dict) -> str:
        self.cleanup()
        key = secrets.token_urlsafe(7)
        self._items[key] = (time.monotonic() + self.ttl_seconds, checkout_ref)
        return key

    def get(self, key: str) -> dict | None:
        item = self._items.get(key)
        if not item:
            return None
        expires_at, checkout_ref = item
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return checkout_ref

    def cleanup(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


tutu_mcp = TutuMCPClient()
tutu_checkout_cache = CheckoutRefCache()
