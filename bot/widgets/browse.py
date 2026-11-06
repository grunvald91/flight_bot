"""Telegram Bot API 10.4 HTML bubble for the indicative international browse.

Opt-in: FLIGHT_BOT_HTML_BUBBLES=1. The ordinary aiogram interface is retained.
Spec: https://github.com/TelegramMessenger/widget-sandbox/blob/main/docs/tgw-format.md
"""
import asyncio
import json
import logging
import math
import os
import re
import zlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from aiogram import Dispatcher
from bot.services.discovery_archive import discovery_archive
from bot.services.fast_browse import direct_view, visible_options
from bot.services.countries import country_label

logger = logging.getLogger(__name__)

PAGE_SIZE = 4
MAX_STATE_BYTES = 1024
KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PAGE = 999
_TEMPLATE = Path(__file__).with_name("browse.html")


def enabled() -> bool:
    return os.getenv("FLIGHT_BOT_HTML_BUBBLES", "0") == "1"


def _encode(obj: dict) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("Widget data exceeds 1024 UTF-8 bytes")
    return text


def _as_price(value):
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(amount) or amount <= 0:
        return None
    return round(amount)


def _as_stops(value):
    if type(value) is int and 0 <= value < 100:
        return value
    return None


def _safe_title(text, limit=29):
    return str(text or "")[:limit]


def page_data(payload: dict, page: int, direct_only: bool) -> dict:
    """Small JSON for both initial public_data and request answers (<=1024 bytes)."""
    if type(page) is not int or not 0 <= page <= MAX_PAGE:
        raise ValueError("Invalid page")
    view = visible_options(payload.get("options") or [], direct_only)
    pages = max(1, (len(view) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page >= pages:
        raise ValueError("Page out of range")
    meta = payload.get("meta") or {}
    rows = []
    for index, item in view[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        price = _as_price(item.get("price"))
        if price is None:
            continue
        rows.append({
            "i": index, "c": _safe_title(item.get("city")),
            "n": _safe_title(item.get("country")),
            "p": price, "t": _as_stops(item.get("transfers")),
        })
    country = meta.get("country_code")
    title = _safe_title(country_label(country) if isinstance(country, str) and len(country) == 2
                        else "Зарубежные направления", 30)
    result = {"ok": 1, "p": page, "d": 1 if direct_only else 0,
              "total": len(view), "pages": pages, "title": title, "rows": rows,
              "date": str(meta.get("departure_date") or "")[:10],
              "incomplete": bool(direct_only and meta.get("direct_checked") is False)}
    _encode(result)
    return result


def _authorized(payload: dict, user_id: int, chat_id: int | None) -> bool:
    """No forwarded or inline queries: private chat owner AND original chat."""
    meta = payload.get("meta") or {}
    return (
        type(user_id) is int and type(chat_id) is int
        and type(meta.get("owner_id")) is int and type(meta.get("chat_id")) is int
        and user_id == chat_id == meta["owner_id"] == meta["chat_id"]
    )


def _selection(payload: dict, raw_index: str, direct: bool = False) -> dict | None:
    if not raw_index.isascii() or not raw_index.isdecimal() or len(raw_index) > 6:
        return None
    index = int(raw_index)
    options = payload.get("options") or []
    if index >= len(options):
        return None
    option = options[index]
    return direct_view(option) if direct else option


async def _api(bot, method: str, params: dict) -> dict | None:
    """Use the verified Bot API 10.4 method names; never log URLs with tokens."""
    import aiohttp
    api = os.getenv("FLIGHT_BOT_WIDGET_API", "production")
    if api not in ("production", "beta"):
        logger.warning("Unknown FLIGHT_BOT_WIDGET_API value; using plain Telegram output")
        return None
    base = ("https://api.telegram.org/beta/bot" if api == "beta"
            else "https://api.telegram.org/bot")
    suffix = ("/test/" if api == "beta" else "/") + method
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6 if method == "answerWidgetQuery" else 12)) as session:
            async with session.post(base + bot.token + suffix, json=params) as response:
                result = await response.json(content_type=None)
        if not result.get("ok"):
            logger.warning("Widget API %s failed: %s", method, result.get("description", "unknown"))
            return None
        return result
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        # aiohttp exceptions may contain the entire token-bearing request URL.
        logger.warning("Widget API %s unavailable (%s)", method, type(exc).__name__)
        return None


def _template() -> str:
    html = _TEMPLATE.read_text(encoding="utf-8")
    if "\x00" in html or len(html.encode("utf-8")) > 32768 or len(zlib.compress(html.encode("utf-8"), 9)) > 4096:
        raise ValueError("Widget HTML exceeds Telegram 10.4 size limits")
    return html


async def send_browse_widget(bot, chat_id: int, key: str, payload: dict, direct_only: bool) -> bool:
    if not enabled() or not KEY.fullmatch(key) or not _authorized(payload, chat_id, chat_id):
        return False
    try:
        snapshot = _encode(page_data(payload, 0, direct_only))
        scope = _encode({"k": key})
        html = _template()
    except (ValueError, OSError):
        logger.exception("Widget payload invalid; using normal message")
        return False
    result = await _api(bot, "sendWidget", {
        "chat_id": chat_id, "html": html, "width": 340, "height": 460,
        "allow_queries": True, "is_shared": False,
        "public_data": snapshot, "chat_data": scope,
        "caption": "⚡ Ориентировочные цены направлений. Если карточка недоступна, используйте обычную выдачу.",
    })
    return result is not None


async def _visa(payload: dict, item: dict) -> dict:
    from bot.services.entry_rules import find_entry_rule
    country_code = str(item.get("country_code") or "").upper()
    country = str(item.get("country") or "")
    if not re.fullmatch(r"[A-Z]{2}", country_code):
        return {"ok": 0, "error": "Страна не определена"}
    try:
        rule = await asyncio.wait_for(find_entry_rule(country, country_code), timeout=7)
    except Exception:
        logger.warning("Entry rules unavailable for %s", country_code, exc_info=True)
        return {"ok": 0, "error": "Не удалось получить правила. Проверьте сайт МИД РФ."}
    if rule is None:
        return {"ok": 0, "error": "Правила не найдены. Проверьте сайт МИД РФ."}
    url = str(rule.source_url or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or len(url) > 256:
        url = ""
    summary = "Для граждан РФ:\n" + str(rule.ordinary_passport or "")
    if rule.stay:
        summary += "\nСрок: " + str(rule.stay)
    if rule.details:
        summary += "\n" + str(rule.details)
    summary = summary[:520]
    if rule.updated_at:
        summary += "\nСведения: " + str(rule.updated_at)[:25]
    summary += "\nПеред поездкой проверьте действующие правила."
    result = {"ok": 1, "name": _safe_title(rule.country, 50), "summary": summary, "url": url}
    # The Bot API caps the *encoded* UTF-8 answer at 1024 bytes, not characters.
    # Some country descriptions include long multibyte scripts or escaped text.
    while len(_encode_unlimited(result).encode("utf-8")) > MAX_STATE_BYTES and summary:
        summary = summary[: max(0, len(summary) - max(8, len(summary) // 8))]
        result["summary"] = summary
    _encode(result)
    return result


def _encode_unlimited(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _check_ticket(bot, chat_id: int, owner_id: int, origin: str, destination: str, date: str):
    from bot.handlers.tutu import _run_tutu_search
    from bot.services.city_resolver import city_resolver
    try:
        message = await bot.send_message(chat_id, "🔎 Проверяю наличие и актуальную цену у Туту…")
        message = message.as_(bot)
        await _run_tutu_search(message, city_resolver.get_city_name(origin),
                               city_resolver.get_city_name(destination), date, owner_id=owner_id)
    except Exception:
        logger.exception("Tutu verification from widget failed")
        await bot.send_message(chat_id, "Не удалось проверить билет. Попробуйте /tutu.")


async def _query_result(query: dict, bot) -> tuple[dict, tuple | None]:
    data = query.get("data")
    if not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
        return {"ok": 0, "error": "Некорректный запрос"}, None
    parts = data.split(":")
    if len(parts) not in (3, 4) or parts[0] not in ("b", "t", "v", "c"):
        return {"ok": 0, "error": "Неизвестная команда"}, None
    action, key = parts[:2]
    if not KEY.fullmatch(key):
        return {"ok": 0, "error": "Некорректный поиск"}, None
    chat = query.get("chat") or {}
    user = query.get("from") or {}
    payload = discovery_archive.get(key)
    if not payload or not _authorized(payload, user.get("id"), chat.get("id")):
        return {"ok": 0, "error": "Поиск недоступен. Запустите новый."}, None
    if action in ("b", "t") and len(parts) == 4:
        page_text, direct_text = parts[2:]
        if not page_text.isascii() or not page_text.isdecimal() or len(page_text) > 3 or direct_text not in ("0", "1"):
            return {"ok": 0, "error": "Некорректная страница"}, None
        if action == "t" and page_text != "0":
            return {"ok": 0, "error": "Некорректный фильтр"}, None
        try:
            result = page_data(payload, int(page_text), direct_text == "1")
        except ValueError:
            return {"ok": 0, "error": "Страница не найдена"}, None
        if action == "t":
            # Keep the preference shared with the legacy browse and month handlers.
            # A DB failure must not prevent a valid widget response.
            from bot.services.search_prefs import save_direct_only
            sender = SimpleNamespace(
                id=user["id"], username=user.get("username"), first_name=user.get("first_name"),
            )
            saved = await save_direct_only(sender, direct_text == "1")
            if not saved:
                result["preference_saved"] = False
        return result, None
    if action in ("v", "c") and ((action == "v" and len(parts) == 3) or (action == "c" and len(parts) == 4)):
        direct = action == "c" and parts[3] == "1"
        if action == "c" and parts[3] not in ("0", "1"):
            return {"ok": 0, "error": "Некорректный фильтр"}, None
        option = _selection(payload, parts[2], direct)
        if not option:
            return {"ok": 0, "error": "Направление не найдено"}, None
        if action == "v":
            return await _visa(payload, option), None
        origin = str(option.get("origin") or (payload.get("meta") or {}).get("origin") or "")
        dest = str(option.get("destination") or "")
        date = str((payload.get("meta") or {}).get("departure_date") or "")
        if not re.fullmatch(r"[A-Z]{3}", origin) or not re.fullmatch(r"[A-Z]{3}", dest) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return {"ok": 0, "error": "Недостаточно данных для проверки"}, None
        try:
            travel_date = date.fromisoformat(date)
        except ValueError:
            return {"ok": 0, "error": "Некорректная дата"}, None
        if travel_date < date.today():
            return {"ok": 0, "error": "Дата вылета прошла. Запустите новый поиск."}, None
        return {"ok": 1, "message": "Проверка через Туту запущена — результат появится в чате."}, (chat["id"], user["id"], origin, dest, date)
    return {"ok": 0, "error": "Некорректная команда"}, None


async def handle_widget_query(bot, query: dict) -> None:
    if not isinstance(query, dict) or not isinstance(query.get("id"), str):
        return
    try:
        response, job = await _query_result(query, bot)
        answer = _encode(response)
    except Exception:
        logger.exception("Widget query handler failed")
        answer, job = _encode({"ok": 0, "error": "Ошибка обработки запроса"}), None
    result = await _api(bot, "answerWidgetQuery", {"widget_query_id": query["id"], "data": answer})
    if result is not None and job:
        task = asyncio.create_task(_check_ticket(bot, *job))
        task.add_done_callback(lambda t: logger.error("Widget job failed: %s", t.exception()) if not t.cancelled() and t.exception() else None)


class WidgetDispatcher(Dispatcher):
    """Aiogram 3 keeps unknown 10.4 update fields in model_extra."""
    async def feed_update(self, bot, update, **kwargs):
        extra = getattr(update, "model_extra", None) or {}
        query = getattr(update, "widget_query", None) or extra.get("widget_query")
        if hasattr(query, "model_dump"):
            query = query.model_dump(exclude_none=True)
        if enabled() and isinstance(query, dict):
            await handle_widget_query(bot, query)
            return True
        return await super().feed_update(bot, update, **kwargs)
