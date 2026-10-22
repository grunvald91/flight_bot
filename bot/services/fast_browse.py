"""Low-cost international destination browsing; fares here are indicative only."""
import logging
import math
from dataclasses import dataclass

from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.international_discovery import country_name_ru

logger = logging.getLogger(__name__)

PAGE_SIZE = 10


@dataclass
class IndicativeResults:
    options: list[dict]
    source_rows: int
    unresolved: int
    # False, если прямые рейсы запросить не удалось: тогда «Без пересадок» неполный
    direct_checked: bool = True


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


def direct_view(option: dict) -> dict | None:
    """Прямой вариант направления или None.

    Источник отдаёт по направлению один самый дешёвый билет. Если он с пересадкой, но
    есть более дорогой прямой, тот лежит в option["direct"]; его и показываем в режиме
    «Без пересадок». Неизвестное число пересадок прямым не считается.
    """
    if option.get("transfers") == 0:
        return option
    direct = option.get("direct")
    if isinstance(direct, dict) and direct.get("price") is not None:
        merged = {key: value for key, value in option.items() if key != "direct"}
        merged.update(direct)
        merged["transfers"] = 0
        return merged
    return None


def visible_options(options: list[dict], direct_only: bool) -> list[tuple[int, dict]]:
    """Что показывать в списке: пары (номер в полном списке, вариант).

    Кнопки выбора направления ссылаются на номер в полном списке, поэтому он сохраняется
    и при фильтре «Без пересадок». В этом режиме цена и рейс берутся от самого дешёвого
    прямого билета, а список сортируется по этой цене.
    """
    if not direct_only:
        return list(enumerate(options))
    view = []
    for index, option in enumerate(options):
        direct = direct_view(option)
        if direct is not None:
            view.append((index, direct))
    view.sort(key=lambda item: (item[1]["price"], str(item[1].get("city") or ""),
                                str(item[1].get("destination") or "")))
    return view


def _option(row: dict, origin: str, code: str, country: str, amount: float) -> dict:
    return {
        "origin": origin,
        "destination": code,
        "city": city_resolver.get_city_name(code),
        "country": country_name_ru(country),
        "country_code": country,
        "price": int(amount) if amount.is_integer() else amount,
        "departure_at": str(row.get("departure_at") or ""),
        "transfers": _stops(row),
        "airline": str(row.get("airline") or ""),
        "flight_number": str(row.get("flight_number") or ""),
        "duration": row.get("duration") if isinstance(row.get("duration"), int)
                    and row["duration"] > 0 else None,
    }


def _parse_rows(rows: list[dict], origin: str, departure_date: str, origin_country: str):
    """Строки источника -> {код направления: вариант}, число строк без страны."""
    result: dict[str, dict] = {}
    unresolved = 0
    for row in rows:
        if not str(row.get("departure_at") or "").startswith(departure_date):
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
        option = _option(row, origin, code, country, amount)
        current = result.get(code)
        if current is None or option["price"] < current["price"]:
            result[code] = option
    return result, unresolved


async def load_indicative_destinations(origin: str, departure_date: str) -> IndicativeResults:
    origin_country = city_resolver.get_country_code(origin)
    if not origin_country:
        raise ValueError("Origin country is unknown")
    # The client exhausts Aviasales pages and raises on ambiguous/failed pages.
    rows = await aviasales_api.search_destinations_on_date(origin, departure_date, limit=1000)
    found, unresolved = _parse_rows(rows, origin, departure_date, origin_country)

    # Самый дешёвый билет по направлению часто с пересадкой, а прямой рейс дороже и в этот
    # список не попадает. Поэтому отдельно запрашиваем самые дешёвые прямые.
    direct_checked = True
    try:
        direct_rows = await aviasales_api.search_destinations_on_date(
            origin, departure_date, limit=1000, direct=True,
        )
    except Exception:
        logger.exception("Direct destinations source failed")
        direct_rows = []
        direct_checked = False
    direct_found, _ = _parse_rows(direct_rows, origin, departure_date, origin_country)
    for code, direct in direct_found.items():
        direct["transfers"] = 0  # источник отдал его по запросу «только прямые»
        current = found.get(code)
        if current is None:
            found[code] = direct
        elif current.get("transfers") != 0:
            current["direct"] = {
                key: direct[key]
                for key in ("price", "departure_at", "airline", "flight_number", "duration")
            }

    options = list(found.values())
    options.sort(key=lambda row: (
        row["price"], row["transfers"] if row["transfers"] is not None else 10**9,
        row["city"], row["destination"],
    ))
    return IndicativeResults(options, len(rows), unresolved, direct_checked)
