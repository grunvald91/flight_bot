"""Indicative cheapest international fares for each of 30 departure dates."""
import asyncio
import logging
import math
from datetime import date, timedelta

from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver

logger = logging.getLogger(__name__)


def cheapest_foreign(rows, origin_country, departure, return_day, country_for, direct=False):
    valid = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("destination")
        if not isinstance(code, str) or len(code) != 3 or not code.isascii() or not code.isalpha():
            continue
        country = country_for(code)
        if not country or country == origin_country:
            continue
        if str(row.get("departure_at") or "")[:10] != departure:
            continue
        if return_day and str(row.get("return_at") or "")[:10] != return_day:
            continue
        if not return_day and row.get("return_at"):
            continue
        if direct and (row.get("transfers") != 0 or (return_day and row.get("return_transfers") != 0)):
            continue
        try:
            raw = row.get("price", row.get("value"))
            if isinstance(raw, bool):
                continue
            price = float(raw)
            if not math.isfinite(price) or price <= 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        valid.append({**row, "price": math.ceil(price), "country_code": country})
    return min(valid, key=lambda row: (row["price"], row["destination"])) if valid else None


# Shared limiter across simultaneous users.
_limit = asyncio.Semaphore(3)


async def daily_foreign(origin: str, start: date, nights: int = 0, direct: bool = False):
    if nights not in (0, 3, 5, 7, 10, 14):
        raise ValueError("Unsupported trip duration")
    country = city_resolver.get_country_code(origin)
    if not country:
        raise ValueError("Unknown origin country")

    async def day(offset):
        departure = (start + timedelta(days=offset)).isoformat()
        return_day = (start + timedelta(days=offset+nights)).isoformat() if nights else None
        try:
            async with _limit:
                rows = await aviasales_api.search_destinations_on_date(
                    origin, departure, direct=direct, return_date=return_day,
                )
            best = cheapest_foreign(rows, country, departure, return_day,
                                    city_resolver.get_country_code, direct)
            return departure, return_day, best, False
        except Exception:
            logger.exception("Flexible fare discovery failed for %s %s", origin, departure)
            return departure, return_day, None, True

    return await asyncio.gather(*(day(i) for i in range(30)))
