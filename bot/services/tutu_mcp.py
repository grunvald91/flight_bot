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
            if tool_name == "register_checkout_passengers":
                logger.error("Sensitive Tutu MCP call failed; details suppressed")
            else:
                logger.exception("Tutu MCP call failed: %s", tool_name)
            raise

    async def search_avia(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        adults: int = 1,
        page_size: int = 30,
        page: int = 1,
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
                "page": page,
                "page_size": page_size,
                "sort": "price_asc",
                "direct_only": False,
                "view": "compact",
            },
        )

    async def search_avia_all(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        adults: int = 1,
    ) -> dict:
        """Load every page exposed by the provider, without a cheaper-first cutoff."""
        combined: list[dict] = []
        seen: set[str] = set()
        latest_meta: dict = {}
        exhausted = False

        for page in range(1, 11):
            payload = await self.search_avia(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                adults=adults,
                page_size=30,
                page=page,
            )
            latest_meta = payload.get("meta") or latest_meta
            previous_count = len(combined)
            for offer in payload.get("offers") or []:
                offer_id = str(offer.get("offer_id") or "")
                if offer_id and offer_id in seen:
                    continue
                if offer_id:
                    seen.add(offer_id)
                combined.append(offer)

            if latest_meta.get("has_more") is False:
                exhausted = True
                break
            if len(combined) == previous_count:  # Empty or repeated page: never loop blindly.
                break
        latest_meta = dict(latest_meta)
        latest_meta["loaded_offers"] = len(combined)
        latest_meta["complete"] = exhausted and latest_meta.get("total_matched_exact") is not False
        return {"offers": combined, "meta": latest_meta}

    async def search_multitransport(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        *,
        adults: int = 1,
        optimize_for: str = "price",
        page_size: int = 12,
        page: int = 1,
        modes: list[str] | None = None,
    ) -> dict:
        arguments = {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "adults": adults,
            "optimize_for": optimize_for,
            "page": page,
            "page_size": page_size,
            "view": "compact",
        }
        if modes:
            arguments["modes"] = modes
        return await self._call("search_multitransport", arguments)

    async def search_hotels(
        self,
        city_name: str,
        check_in: str,
        check_out: str,
        *,
        adults: int = 1,
        children_ages: list[int] | None = None,
        page_size: int = 10,
        page: int = 1,
    ) -> dict:
        return await self._call(
            "search_hotels",
            {
                "city_name": city_name,
                "check_in": check_in,
                "check_out": check_out,
                "adults": adults,
                "children_ages": children_ages or [],
                "page": page,
                "page_size": page_size,
                "view": "compact",
            },
        )

    async def search_rail(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        *,
        passengers: int = 1,
        page_size: int = 10,
    ) -> dict:
        return await self._call(
            "search_rail",
            {
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "passengers": passengers,
                "page": 1,
                "page_size": page_size,
                "sort": "price_asc",
                "view": "compact",
            },
        )

    async def search_bus(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        *,
        adults: int = 1,
        page_size: int = 10,
    ) -> dict:
        return await self._call(
            "search_bus",
            {
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "adults": adults,
                "children": 0,
                "page": 1,
                "page_size": page_size,
                "sort": "price_asc",
                "view": "compact",
            },
        )

    async def search_etrain(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        *,
        page_size: int = 10,
    ) -> dict:
        return await self._call(
            "search_etrain",
            {
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date,
                "page": 1,
                "page_size": page_size,
                "sort": "price_asc",
                "view": "compact",
            },
        )

    async def create_checkout_link(self, checkout_ref: dict) -> dict:
        return await self._call("create_checkout_link", dict(checkout_ref))

    async def register_checkout_passengers(
        self,
        *,
        passengers: list[dict],
        customer: dict,
        avia_offer: dict,
    ) -> dict:
        """Create one unpaid avia booking after a separate explicit confirmation."""
        return await self._call(
            "register_checkout_passengers",
            {
                "passengers": passengers,
                "customer": customer,
                "avia_offer": avia_offer,
            },
        )


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


class SearchResultCache:
    def __init__(self, ttl_seconds: int = 1800):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, dict]] = {}

    def put(self, payload: dict) -> str:
        self.cleanup()
        key = secrets.token_urlsafe(7)
        self._items[key] = (time.monotonic() + self.ttl_seconds, payload)
        return key

    def get(self, key: str) -> dict | None:
        item = self._items.get(key)
        if not item:
            return None
        expires_at, payload = item
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return payload

    def cleanup(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


tutu_mcp = TutuMCPClient()
tutu_checkout_cache = CheckoutRefCache()
tutu_search_cache = SearchResultCache()
passenger_booking_cache = SearchResultCache(ttl_seconds=1800)
