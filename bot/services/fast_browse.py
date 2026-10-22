"""Low-cost international destination browsing; fares here are indicative only."""
import math
from dataclasses import dataclass

from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.international_discovery import country_name_ru

PAGE_SIZE = 10


@dataclass
class IndicativeResults:
    options: list[dict]
    source_rows: int
    unresolved: int


def _price(row: dict) -> float | None:
    try:
        value = row.get("price")
        if isinstance(value, bool):
            return None
        amount = float(value)
        if math.isfinite(amount) and amount > 0:
            return amount
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _stops(row: dict) -> int | None:
    value = row.get("transfers")
    if value is None:
        value = row.get("number_of_changes")
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
        return count if count >= 0 and str(value).strip() == str(count) else None
    except (TypeError, ValueError, OverflowError):
        return None


def page_options(options: list, page: int) -> list:
    if page < 0:
        return []
    return options[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]


def visible_options(options: list[dict], direct_only: bool) -> list[tuple[int, dict]]:
    """Что показывать в списке: пары (номер в полном списке, вариант).

    Кнопки выбора направления ссылаются на номер в полном списке, поэтому он сохраняется
    и при фильтре «Без пересадок». Вариант с неизвестным числом пересадок прямым не считается.
    """
    return [
        (index, option) for index, option in enumerate(options)
        if not direct_only or option.get("transfers") == 0
    ]


async def load_indicative_destinations(origin: str, departure_date: str) -> IndicativeResults:
    origin_country = city_resolver.get_country_code(origin)
    if not origin_country:
        raise ValueError("Origin country is unknown")
    # The client exhausts Aviasales pages and raises on ambiguous/failed pages.
    rows = await aviasales_api.search_destinations_on_date(origin, departure_date, limit=1000)
    options = []
    unresolved = 0
    for row in rows:
        departure_at = str(row.get("departure_at") or "")
        if not departure_at.startswith(departure_date):
            continue
        amount = _price(row)
        if amount is None:
            continue
        code = str(row.get("destination") or "").upper()
        country = city_resolver.get_country_code(code)
        if not country:
            unresolved += 1
            continue
        if country == origin_country:
            continue
        options.append({
            "origin": origin,
            "destination": code,
            "city": city_resolver.get_city_name(code),
            "country": country_name_ru(country),
            "country_code": country,
            "price": int(amount) if amount.is_integer() else amount,
            "departure_at": departure_at,
            "transfers": _stops(row),
            "airline": str(row.get("airline") or ""),
            "flight_number": str(row.get("flight_number") or ""),
            "duration": row.get("duration") if isinstance(row.get("duration"), int)
                        and row["duration"] > 0 else None,
        })
    options.sort(key=lambda row: (
        row["price"], row["transfers"] if row["transfers"] is not None else 10**9,
        row["city"], row["destination"],
    ))
    return IndicativeResults(options, len(rows), unresolved)
