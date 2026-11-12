"""Private Telegram 10.4 widget for the cheapest flights in a 30-day window.

Aviasales fares are indicative. Every action is scoped to the search owner,
and booking is never performed inside the widget.
"""
import logging
import re
import zlib
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.discovery_archive import discovery_archive
from bot.services.city_resolver import city_resolver
from bot.widgets.browse import (
    KEY, _api, _authorized, _as_price, _as_stops, _encode,
    _safe_title, _widget_confirmed, enabled,
)

logger = logging.getLogger(__name__)
PAGE_SIZE = 2
_TEMPLATE = Path(__file__).with_name("month.html")
_IATA = re.compile(r"[A-Z]{3}")


def _when(row: dict):
    raw = str(row.get("departure_at") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _window(payload: dict):
    meta = payload.get("meta") or {}
    try:
        start = date.fromisoformat(str(meta.get("start") or ""))
        days = int(meta.get("days") or 30)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid search window") from exc
    if not 1 <= days <= 31:
        raise ValueError("Invalid search window")
    return start, days


def _options(payload: dict, direct: bool, sort: str, day: int):
    if sort not in ("price", "date"):
        raise ValueError("Invalid sort")
    start, days = _window(payload)
    if type(day) is not int or not -1 <= day < days:
        raise ValueError("Invalid date offset")
    rows = []
    for index, ticket in enumerate(payload.get("options") or []):
        if not isinstance(ticket, dict):
            continue
        price, when, transfers = _as_price(ticket.get("price")), _when(ticket), _as_stops(ticket.get("transfers"))
        # Archived cards must not present departures in the past as current deals.
        if price is None or when is None or not max(start, date.today()) <= when < start + timedelta(days=days):
            continue
        if direct and transfers != 0:
            continue
        if day >= 0 and when != start + timedelta(days=day):
            continue
        rows.append((index, ticket, price, when, transfers))
    if sort == "date":
        rows.sort(key=lambda row: (row[3], row[2], row[0]))
    else:
        rows.sort(key=lambda row: (row[2], row[3], row[0]))
    return rows


def _row(item):
    index, ticket, price, when, transfers = item
    return {
        "i": index, "p": price, "dt": when.isoformat(), "t": transfers,
        "c": _safe_title(ticket.get("airline"), 12),
        "to": _safe_title(
            city_resolver.get_city_name(ticket.get("destination"))
            if ticket.get("destination") else "", 18),
        "tm": str(ticket.get("departure_at") or "")[11:16] if "T" in str(ticket.get("departure_at") or "") else "",
    }


def page_data(payload: dict, page: int = 0, direct: bool = False,
              sort: str = "price", day: int = -1) -> dict:
    if (type(page) is not int or not 0 <= page <= 999 or
            type(direct) is not bool):
        raise ValueError("Invalid page")
    if (payload.get("meta") or {}).get("kind") != "month":
        raise ValueError("Wrong search type")
    rows = _options(payload, direct, sort, day)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page >= pages:
        raise ValueError("Page out of range")
    # The highlighted minimum must refer to the currently selected day/filter.
    best = min(rows, key=lambda r: (r[2], r[3])) if rows else None
    meta = payload["meta"]
    result = {
        "ok": 1, "m": "month", "pg": page, "pages": pages, "total": len(rows),
        "d": int(direct), "s": sort[0], "day": day,
        "start": str(meta.get("start") or ""),
        "route": _safe_title(meta.get("origin_name"), 21) + " → " + _safe_title(meta.get("target_name"), 25),
        "best": {"p": best[2], "dt": best[3].isoformat()} if best else None,
        "rows": [_row(r) for r in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]],
    }
    # Keep every ticket on its page: optional city/airline labels can shrink,
    # but silently dropping items would make pagination incomplete.
    try:
        _encode(result)
    except ValueError:
        for row in result["rows"]:
            row["to"] = row["to"][:10]
            row["c"] = row["c"][:6]
            row["tm"] = row["tm"][:5]
        result["route"] = result["route"][:30]
        _encode(result)
    return result


def calendar_data(payload: dict, direct: bool = False) -> dict:
    start, days = _window(payload)
    cheapest = {}
    for _, _, price, when, _ in _options(payload, direct, "price", -1):
        offset = (when - start).days
        cheapest[offset] = min(cheapest.get(offset, price), price)
    result = {
        "ok": 1, "cal": 1, "start": start.isoformat(), "n": days,
        "d": int(direct),
        "rows": [{"i": i, "p": cheapest[i]} for i in sorted(cheapest)],
    }
    _encode(result)
    return result


def detail_data(payload: dict, index: int) -> dict:
    options = payload.get("options") or []
    if type(index) is not int or not 0 <= index < len(options):
        raise ValueError("Unknown ticket")
    ticket = options[index]
    when = _when(ticket)
    price = _as_price(ticket.get("price"))
    if when is None or price is None:
        raise ValueError("Incomplete ticket")
    start, days = _window(payload)
    if not max(start, date.today()) <= when < start + timedelta(days=days):
        raise ValueError("Ticket date is no longer inside the active search window")
    meta = payload.get("meta") or {}
    result = {
        "ok": 1, "detail": 1, "p": price, "dt": str(ticket.get("departure_at") or "")[:22],
        "route": _safe_title(meta.get("origin_name"), 22) + " → " + _safe_title(meta.get("target_name"), 25),
        "c": _safe_title(ticket.get("airline"), 25),
        "t": _as_stops(ticket.get("transfers")),
        "duration": ticket.get("duration") if type(ticket.get("duration")) is int else None,
        "can_open": bool(
            _IATA.fullmatch(str(meta.get("origin") or "")) and
            _IATA.fullmatch(str(ticket.get("destination") or meta.get("destination") or "")) and
            when >= date.today()
        ),
    }
    _encode(result)
    return result


def _template():
    html = _TEMPLATE.read_text(encoding="utf-8")
    raw = html.encode("utf-8")
    if not raw or b"\x00" in raw or len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError("Month widget exceeds Telegram HTML limits")
    return html


async def send_month_widget(bot, chat_id: int, key: str, payload: dict, direct: bool = False) -> bool:
    if not enabled() or not KEY.fullmatch(key) or not _authorized(payload, chat_id, chat_id):
        return False
    try:
        initial = _encode(page_data(payload, direct=direct))
        html = _template()
    except (OSError, ValueError, TypeError):
        logger.exception("Month widget could not be prepared")
        return False
    result = await _api(bot, "sendWidget", {
        "chat_id": chat_id, "html": html,
        "allow_queries": True, "is_shared": False,
        "public_data": initial, "chat_data": _encode({"k": key}),
        "caption": "📅 Самые дешёвые даты по направлению • цены ориентировочные",
    })
    if _widget_confirmed(result):
        logger.info("Month HTML widget accepted: message_id=%s", result["result"].get("message_id"))
        return True
    return False


async def query_result(query: dict):
    data = query.get("data")
    if not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
        return {"ok": 0, "error": "Некорректный запрос"}, None
    parts = data.split(":")
    if len(parts) < 3 or parts[0] not in ("ml", "mc", "md", "ma", "mt"):
        return {"ok": 0, "error": "Неизвестное действие"}, None
    action, key = parts[:2]
    if not KEY.fullmatch(key):
        return {"ok": 0, "error": "Некорректный поиск"}, None
    try:
        payload = discovery_archive.get(key)
    except Exception:
        logger.exception("Month archive unavailable")
        payload = None
    user = query.get("from") or {}
    chat = query.get("chat") or {}
    if not payload or not _authorized(payload, user.get("id"), chat.get("id")):
        return {"ok": 0, "error": "Этот поиск недоступен. Начните новый."}, None
    if (payload.get("meta") or {}).get("kind") != "month":
        return {"ok": 0, "error": "Неверный вид поиска"}, None
    if action == "ml" and len(parts) == 6:
        pg, direct, sort, day = parts[2:]
        if (not pg.isascii() or not pg.isdecimal() or len(pg) > 3
                or direct not in ("0", "1") or sort not in ("p", "d")
                or not re.fullmatch(r"-1|[0-9]{1,2}", day)):
            return {"ok": 0, "error": "Неправильный фильтр"}, None
        try:
            return page_data(payload, int(pg), direct == "1",
                             "price" if sort == "p" else "date", int(day)), None
        except ValueError:
            return {"ok": 0, "error": "Страница недоступна"}, None
    if action == "mt" and len(parts) == 3 and parts[2] in ("0", "1"):
        direct = parts[2] == "1"
        try:
            result = page_data(payload, 0, direct)
        except ValueError:
            return {"ok": 0, "error": "Фильтр недоступен"}, None
        try:
            from bot.services.search_prefs import save_direct_only
            saved = await save_direct_only(SimpleNamespace(
                id=user["id"], first_name=user.get("first_name"),
                username=user.get("username"),
            ), direct)
            if not saved:
                result["preference_saved"] = False
        except Exception:
            logger.warning("Could not persist monthly direct-only preference", exc_info=True)
            result["preference_saved"] = False
        return result, None
    if action == "mc" and len(parts) == 3 and parts[2] in ("0", "1"):
        try:
            return calendar_data(payload, parts[2] == "1"), None
        except ValueError:
            return {"ok": 0, "error": "Календарь недоступен"}, None
    if action in ("md", "ma") and len(parts) == 3:
        if not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 5:
            return {"ok": 0, "error": "Некорректный билет"}, None
        index = int(parts[2])
        try:
            detail = detail_data(payload, index)
        except ValueError:
            return {"ok": 0, "error": "Билет недоступен"}, None
        if action == "md":
            return detail, None
        if not detail["can_open"]:
            return {"ok": 0, "error": "Дата прошла или маршрут не определён"}, None
        return {"ok": 1, "message": "Отправляю кнопку проверки цены в чат."}, (chat["id"], key, index)
    return {"ok": 0, "error": "Неизвестная команда"}, None


async def send_month_link(bot, chat_id: int, key: str, index: int):
    """Only return a trusted Aviasales search URL, never promise fare availability."""
    payload = discovery_archive.get(key)
    if not payload or not _authorized(payload, chat_id, chat_id):
        return
    if (payload.get("meta") or {}).get("kind") != "month":
        return
    try:
        info = detail_data(payload, index)
    except ValueError:
        return
    if not info["can_open"]:
        return
    from bot.services.aviasales_api import aviasales_api
    meta = payload["meta"]
    ticket = payload["options"][index]
    origin = str(meta["origin"])
    destination = str(ticket.get("destination") or meta["destination"])
    when = _when(ticket)
    if not _IATA.fullmatch(origin) or not _IATA.fullmatch(destination) or when is None:
        return
    url = aviasales_api.build_search_link(origin, destination, when.isoformat())
    if not url.startswith("https://www.aviasales.ru/search/"):
        logger.warning("Rejected unexpected monthly search URL")
        return
    await bot.send_message(
        chat_id,
        "🔎 Проверьте актуальную цену и наличие на Aviasales. Указанная ранее стоимость была ориентировочной.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✈️ Проверить билеты на эту дату", url=url)],
        ]),
    )
