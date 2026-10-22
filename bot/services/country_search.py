"""Билеты на ближайший месяц в страну целиком: все её города, ранжирование по цене."""
import asyncio
import logging
from datetime import datetime, timedelta

from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver

logger = logging.getLogger(__name__)

# Одновременных запросов к Aviasales: у страны может быть много городов
CONCURRENCY = 8


def _months(start, days: int) -> list[str]:
    end = start + timedelta(days=days)
    months, cursor = [], start.replace(day=1)
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


async def country_destinations(origin: str, country_code: str, months: list[str], direct_only: bool) -> list[str]:
    """Города страны, в которые из `origin` в эти месяцы вообще есть билеты."""
    results = await asyncio.gather(
        *[aviasales_api.search_destinations_in_month(origin, month, direct=direct_only) for month in months],
        return_exceptions=True,
    )
    found: list[str] = []
    for month, rows in zip(months, results):
        if isinstance(rows, Exception):
            logger.error("country destinations %s %s failed: %s", origin, month, rows)
            continue
        for row in rows:
            code = str(row.get("destination") or "").upper()
            if (
                code and code != origin and code not in found
                and city_resolver.get_country_code(code) == country_code
            ):
                found.append(code)
    return found


async def search_cheapest_to_country(
    origin: str, country_code: str, *, days: int = 30, direct_only: bool = False, start_date=None,
) -> list[dict]:
    """Все билеты из `origin` в города страны на ближайшие `days` дней, от дешёвых к дорогим.

    У каждого билета заполнено поле destination, чтобы показать город и собрать ссылку.
    """
    start = start_date or datetime.now().date()
    destinations = await country_destinations(origin, country_code, _months(start, days), direct_only)
    if not destinations:
        return []

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(destination: str) -> list[dict]:
        async with semaphore:
            try:
                tickets = await aviasales_api.search_cheapest_in_window(
                    origin, destination, start_date=start, days=days,
                )
            except Exception:
                logger.exception("country search %s-%s failed", origin, destination)
                return []
        for ticket in tickets:
            ticket["destination"] = destination
        return tickets

    groups = await asyncio.gather(*[one(code) for code in destinations])
    tickets = [t for group in groups for t in group]
    if direct_only:
        tickets = [t for t in tickets if t.get("transfers", 0) == 0]
    tickets.sort(key=lambda t: (t["price"], t.get("departure_at", ""), t["destination"]))
    return tickets
