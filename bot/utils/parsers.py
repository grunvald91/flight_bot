import re
from dataclasses import dataclass
from datetime import datetime

DASH_TRANSLATION = str.maketrans({
    "‐": "-",  # U+2010
    "‑": "-",  # U+2011
    "‒": "-",  # U+2012
    "–": "-",  # U+2013
    "—": "-",  # U+2014
    "―": "-",  # U+2015
    "−": "-",  # U+2212
    "﹘": "-",  # U+FE58
    "﹣": "-",  # U+FE63
    "－": "-",  # U+FF0D
    "­": "",   # U+00AD (soft hyphen)
})


@dataclass
class InlineQueryParsed:
    query_type: str          # "route" | "cheapest" | "help"
    origin: str | None = None
    destination: str | None = None
    depart_date: str | None = None    # YYYY-MM-DD
    return_date: str | None = None    # YYYY-MM-DD
    connections_only: bool = False
    raw_origin: str | None = None
    raw_destination: str | None = None


def parse_inline_query(text: str) -> InlineQueryParsed:
    """
    Парсит inline-запрос пользователя.

    Форматы:
        MOW-IST                       -> маршрут (IATA-IATA)
        MOW-IST 15.03                 -> маршрут + дата
        MOW-IST 15.03-22.03           -> маршрут + туда-обратно
        Сочи-Санкт-Петербург 06.03    -> маршрут с составными названиями
        Москва                        -> дешёвые из города
        fav / избранные               -> избранные маршруты пользователя
        (пусто)                       -> помощь
    """
    text = _normalize_text(text)

    if not text:
        return InlineQueryParsed(query_type="help")

    # Проверяем на команды избранного
    lower_text = text.lower()
    if lower_text in ["fav", "favorites", "избранные", "избранное"]:
        return InlineQueryParsed(query_type="favorites")

    text, connections_only = _extract_connections_marker(text)

    # Извлекаем даты с конца строки
    dates_pattern = re.compile(
        r'\s+(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)'
        r'(?:\s*-\s*(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?))?'
        r'\s*$'
    )
    date1 = None
    date2 = None
    route_part = text

    date_match = dates_pattern.search(text)
    if date_match:
        date1 = date_match.group(1)
        date2 = date_match.group(2)
        route_part = _normalize_text(text[:date_match.start()])

    # Пробуем разбить маршрут на origin-destination
    origin, destination = _split_route(route_part)

    if origin and destination:
        depart = _parse_date(date1) if date1 else None
        ret = _parse_date(date2) if date2 else None

        return InlineQueryParsed(
            query_type="route",
            connections_only=connections_only,
            raw_origin=origin,
            raw_destination=destination,
            depart_date=depart,
            return_date=ret,
        )

    # Одно слово / город — дешёвые направления
    single = route_part.strip()
    if single:
        return InlineQueryParsed(
            query_type="cheapest",
            raw_origin=single,
        )

    return InlineQueryParsed(query_type="help")


def _extract_connections_marker(text: str) -> tuple[str, bool]:
    markers = (
        "с пересадками",
        "пересадки",
        "стыковки",
        "connections",
    )

    normalized = _normalize_text(text)
    lowered = normalized.lower()
    for marker in markers:
        marker_norm = _normalize_text(marker).lower()
        prefix = f"{marker_norm} "
        suffix = f" {marker_norm}"
        if lowered.startswith(prefix):
            stripped = normalized[len(prefix):].strip()
            return stripped, True
        if lowered.endswith(suffix):
            stripped = normalized[:-len(suffix)].strip()
            return stripped, True

    return normalized, False


def _split_route(route: str) -> tuple[str | None, str | None]:
    """
    Разбивает маршрут на origin и destination.
    Обрабатывает составные названия: Санкт-Петербург, Нью-Йорк и т.д.

    Стратегия:
    1. IATA-IATA (MOW-IST) — быстрый путь
    2. Разделитель с пробелами (Сочи - Москва)
    3. Перебор точек разбиения по дефису — проверяем через city_resolver
    """
    route = _normalize_text(route)
    if not route:
        return None, None

    # 1. Разделитель с пробелами вокруг тире
    spaced_split = re.split(r"\s+-\s+", route, maxsplit=1)
    if len(spaced_split) == 2 and all(spaced_split):
        return spaced_split[0].strip(), spaced_split[1].strip()

    # 2. IATA-IATA: ровно 3 латинских буквы с каждой стороны
    iata_match = re.match(r'^([A-Za-z]{2,3})-([A-Za-z]{2,3})$', route)
    if iata_match:
        return iata_match.group(1), iata_match.group(2)

    # 3. Умный разбор: перебираем все дефисы и проверяем какая пара городов резолвится
    if "-" in route:
        positions = [i for i, c in enumerate(route) if c == "-"]
        from bot.services.city_resolver import city_resolver

        resolved_candidates: list[tuple[int, str, str]] = []

        for pos in positions:
            left = route[:pos].strip()
            right = route[pos + 1:].strip()
            if not left or not right:
                continue

            left_resolved = city_resolver.resolve(left)
            right_resolved = city_resolver.resolve(right)

            if left_resolved and right_resolved:
                score = min(len(left), len(right))
                resolved_candidates.append((score, left, right))

        if resolved_candidates:
            resolved_candidates.sort(key=lambda item: item[0], reverse=True)
            return resolved_candidates[0][1], resolved_candidates[0][2]

        # Если обе части не удалось резолвить, берём последний дефис.
        # Это устойчивее для составных городов: "Санкт-Петербург-Сочи".
        last = positions[-1]
        return route[:last].strip(), route[last + 1:].strip()

    return None, None


def _parse_date(date_str: str) -> str | None:
    """Парсит дату в формат YYYY-MM-DD. Поддерживает DD.MM и DD.MM.YYYY."""
    if not date_str:
        return None

    date_str = date_str.replace("/", ".")
    parts = date_str.split(".")

    now = datetime.now()

    try:
        if len(parts) == 2:
            day, month = int(parts[0]), int(parts[1])
            year = now.year
            d = datetime(year, month, day)
            # Если дата в прошлом — берём следующий год
            if d.date() < now.date():
                year += 1
            return f"{year}-{month:02d}-{day:02d}"

        elif len(parts) == 3:
            day, month = int(parts[0]), int(parts[1])
            year = int(parts[2])
            if year < 100:
                year += 2000
            return f"{year}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return None

    return None


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    normalized = text.translate(DASH_TRANSLATION)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
