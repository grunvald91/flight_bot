import asyncio
import logging
import math
from collections.abc import Awaitable, Callable

from babel import Locale

from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.tutu_mcp import tutu_mcp

logger = logging.getLogger(__name__)
RU_LOCALE = Locale.parse("ru")
SPECIAL_COUNTRIES_RU = {
    "AB": "Абхазия",
    "XK": "Косово",
}


def country_name_ru(country_code: str | None) -> str:
    if not country_code:
        return "Страна не определена"
    code = country_code.upper()
    return str(SPECIAL_COUNTRIES_RU.get(code) or RU_LOCALE.territories.get(code) or code)


def _price_amount(offer: dict) -> float:
    try:
        amount = float((offer.get("price") or {}).get("amount") or 0)
        return amount if math.isfinite(amount) else 0.0
    except (TypeError, ValueError):
        return 0.0

class DiscoveryResults(list):
    """Verified offers with an explicit accounting of the candidate search."""

    def __init__(self, offers: list[dict], *, checked: int, unverified: int,
                 no_offers: int, source_rows: int, source_limit: int):
        super().__init__(offers)
        self.checked = checked
        self.unverified = unverified
        self.no_offers = no_offers
        self.source_rows = source_rows
        self.source_limit = source_limit


async def discover_international_offers(
    origin: str,
    departure_date: str,
    *,
    result_limit: int | None = None,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
) -> list[dict]:
    """Find international candidates, then verify full live prices through Tutu MCP."""
    origin_country = city_resolver.get_country_code(origin)
    if not origin_country:
        raise ValueError("Cannot determine origin country for international discovery")
    origin_name = city_resolver.get_city_name(origin)
    source_limit = 1000  # Page size, not a cap on the candidate stream.
    candidate_rows = await aviasales_api.search_destinations_on_date(
        origin,
        departure_date,
        limit=source_limit,
    )

    candidates: list[dict] = []
    seen: set[str] = set()
    unresolved_count = 0
    for row in candidate_rows:
        destination = str(row.get("destination") or "").upper()
        if not destination:
            raise ValueError("Destination source returned an incomplete row")
        if destination in seen:
            continue
        destination_country = city_resolver.get_country_code(destination)
        if not destination_country:
            unresolved_count += 1
            continue
        if (
            destination_country == origin_country
        ):
            continue
        seen.add(destination)
        candidates.append({
            "code": destination,
            "city": city_resolver.get_city_name(destination),
            "country_code": destination_country,
            "indicative_price": row.get("price") or row.get("value"),
        })


    total_candidates = len(candidates)
    if progress_callback:
        try:
            await progress_callback(0, total_candidates, 0)
        except Exception as exc:
            logger.info("Discovery progress update failed: %s", exc)

    semaphore = asyncio.Semaphore(6)
    progress_lock = asyncio.Lock()
    checked_count = 0
    found_count = 0
    unverified_count = unresolved_count
    no_offers_count = 0

    async def report_progress(status: str) -> None:
        nonlocal checked_count, found_count, unverified_count, no_offers_count
        async with progress_lock:
            checked_count += 1
            if status == "found":
                found_count += 1
            elif status == "unverified":
                unverified_count += 1
            else:
                no_offers_count += 1
            should_report = checked_count % 10 == 0 or checked_count == total_candidates
            snapshot = (checked_count, total_candidates, found_count)
        if should_report and progress_callback:
            try:
                await progress_callback(*snapshot)
            except Exception as exc:
                logger.info("Discovery progress update failed: %s", exc)

    async def verify(candidate: dict) -> dict | None:
        selected: dict | None = None
        status = "no_offers"
        async with semaphore:
            try:
                # Tutu sorts the route pool by price before pagination. The first
                # offer is the cheapest when its upstream pool was not truncated.
                payload = await tutu_mcp.search_avia(
                    origin_name, candidate["city"], departure_date,
                    page_size=1, page=1,
                )
                if not isinstance(payload, dict) or not isinstance(payload.get("offers"), list):
                    raise ValueError("Invalid Tutu search response")
                meta = payload.get("meta") or {}
                if meta.get("total_matched_exact") is not True:
                    raise ValueError("Tutu route pool completeness is not confirmed")
                priced = [offer for offer in payload["offers"]
                          if isinstance(offer, dict) and _price_amount(offer) > 0]
                if priced and any((offer.get("price") or {}).get("currency") != "RUB" for offer in priced):
                    raise ValueError("Tutu returned a price without confirmed RUB currency")
                if not priced and (payload["offers"] or meta.get("has_more")):
                    raise ValueError("Tutu route page has no valid price")
                if priced:
                    selected = dict(min(priced, key=_price_amount))
                    resolved_to = ((payload.get("meta") or {}).get("to") or {}).get("name")
                    selected["_discovery"] = {
                        "destination_code": candidate["code"],
                        "city": resolved_to or candidate["city"],
                        "country_code": candidate["country_code"],
                        "country": country_name_ru(candidate["country_code"]),
                        "indicative_price": candidate["indicative_price"],
                        "trip_type": "one_way",
                    }
                    status = "found"
            except Exception as exc:
                status = "unverified"
                logger.info(
                    "Tutu discovery candidate failed %s -> %s: %s",
                    origin,
                    candidate["code"],
                    exc,
                )
        await report_progress(status)
        return selected

    checked = await asyncio.gather(
        *(verify(candidate) for candidate in candidates),
        return_exceptions=False,
    )
    offers = [offer for offer in checked if offer]
    offers.sort(key=lambda offer: (_price_amount(offer), offer["_discovery"]["city"]))
    return DiscoveryResults(
        offers if result_limit is None else offers[:result_limit],
        checked=checked_count, unverified=unverified_count,
        no_offers=no_offers_count, source_rows=len(candidate_rows), source_limit=source_limit,
    )
