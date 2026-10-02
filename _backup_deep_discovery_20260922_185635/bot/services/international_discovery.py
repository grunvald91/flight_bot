import asyncio
import logging

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
        return float((offer.get("price") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


async def discover_international_offers(
    origin: str,
    departure_date: str,
    *,
    candidate_limit: int = 24,
    result_limit: int = 12,
) -> list[dict]:
    """Find international candidates, then verify full live prices through Tutu MCP."""
    origin_country = city_resolver.get_country_code(origin)
    origin_name = city_resolver.get_city_name(origin)
    candidate_rows = await aviasales_api.search_destinations_on_date(
        origin,
        departure_date,
        limit=100,
    )

    candidates: list[dict] = []
    seen: set[str] = set()
    for row in candidate_rows:
        destination = str(row.get("destination") or "").upper()
        destination_country = city_resolver.get_country_code(destination)
        if (
            not destination
            or destination in seen
            or not destination_country
            or destination_country == origin_country
        ):
            continue
        seen.add(destination)
        candidates.append({
            "code": destination,
            "city": city_resolver.get_city_name(destination),
            "country_code": destination_country,
            "indicative_price": row.get("price") or row.get("value"),
        })
        if len(candidates) >= candidate_limit:
            break

    semaphore = asyncio.Semaphore(4)

    async def verify(candidate: dict) -> dict | None:
        async with semaphore:
            try:
                payload = await tutu_mcp.search_avia(
                    origin_name,
                    candidate["city"],
                    departure_date,
                    page_size=3,
                )
            except Exception as exc:
                logger.info(
                    "Tutu discovery candidate failed %s -> %s: %s",
                    origin,
                    candidate["code"],
                    exc,
                )
                return None

        offers = payload.get("offers") or []
        priced = [offer for offer in offers if _price_amount(offer) > 0]
        if not priced:
            return None
        selected = min(priced, key=_price_amount)
        selected = dict(selected)
        resolved_to = ((payload.get("meta") or {}).get("to") or {}).get("name")
        selected["_discovery"] = {
            "destination_code": candidate["code"],
            "city": resolved_to or candidate["city"],
            "country_code": candidate["country_code"],
            "country": country_name_ru(candidate["country_code"]),
            "indicative_price": candidate["indicative_price"],
            "trip_type": "one_way",
        }
        return selected

    checked = await asyncio.gather(
        *(verify(candidate) for candidate in candidates),
        return_exceptions=False,
    )
    offers = [offer for offer in checked if offer]
    offers.sort(key=lambda offer: (_price_amount(offer), offer["_discovery"]["city"]))
    return offers[:result_limit]
