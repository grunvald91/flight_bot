"""Compact, private Telegram 10.4 HTML cards for Tutu and deep international searches.

No purchase or passenger data is ever handled inside a widget. The widget only
opens existing, user-confirmed Telegram checkout/profile callbacks.
"""
import asyncio
import logging
import math
import re
import zlib
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.discovery_archive import discovery_archive
from bot.widgets.browse import (
    KEY, _api, _authorized, _encode, _safe_title, _visa, _widget_confirmed, enabled,
)

logger = logging.getLogger(__name__)
PAGE_SIZE = 3
_TEMPLATE = Path(__file__).with_name("offers.html")
_MODES = {"tutu", "discover"}


def _amount(offer: dict) -> int | None:
    price = offer.get("price") or {}
    if not isinstance(price, dict) or price.get("currency") not in ("RUB", None):
        return None
    try:
        value = float(price.get("amount"))
        if not math.isfinite(value) or value <= 0:
            return None
        return round(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _stops(offer: dict) -> int | None:
    legs = offer.get("legs") or []
    if not isinstance(legs, list) or not legs:
        return None
    if any(not isinstance(leg, dict) or not isinstance(leg.get("segments"), list) or
           not leg.get("segments") for leg in legs):
        return None
    return sum(len(leg["segments"]) - 1 for leg in legs)


def _place(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("city") or value.get("code") or "—")
    return str(value or "—")


def _places(offer: dict) -> tuple[str, str]:
    discovery = offer.get("_discovery") or {}
    if isinstance(discovery, dict) and discovery.get("city"):
        return str(discovery.get("country") or "За границу"), str(discovery["city"])
    legs = offer.get("legs") or []
    if not legs:
        return "Авиабилет", "Маршрут"
    return _place(legs[0].get("from")), _place(legs[-1].get("to"))


def _view(payload: dict, direct: bool, sort: str) -> list[tuple[int, dict]]:
    if sort not in ("price", "stops"):
        raise ValueError("Invalid sort order")
    offers = payload.get("offers") or []
    view = [(i, offer) for i, offer in enumerate(offers)
            if isinstance(offer, dict) and _amount(offer) is not None
            and (not direct or _stops(offer) == 0)]
    if sort == "stops":
        view.sort(key=lambda pair: (
            _stops(pair[1]) if _stops(pair[1]) is not None else 1000000,
            _amount(pair[1]), pair[0],
        ))
    else:
        view.sort(key=lambda pair: (
            _amount(pair[1]),
            _stops(pair[1]) if _stops(pair[1]) is not None else 1000000,
            pair[0],
        ))
    return view


def _row(i: int, offer: dict) -> dict:
    a, b = _places(offer)
    return {
        "i": i, "a": _safe_title(a, 25), "b": _safe_title(b, 25),
        "p": _amount(offer), "s": _stops(offer),
        "t": str(offer.get("departure_at") or "").replace("T", " ")[:16],
        "c": _safe_title(", ".join(str(v) for v in (offer.get("carriers") or [])[:2]), 23),
    }


def page_data(payload: dict, page: int = 0, direct: bool = False, sort: str = "price") -> dict:
    if type(page) is not int or page < 0 or page > 999 or type(direct) is not bool:
        raise ValueError("Invalid page/filter")
    kind = (payload.get("meta") or {}).get("kind")
    if kind not in _MODES:
        raise ValueError("Invalid search type")
    view = _view(payload, direct, sort)
    pages = max(1, (len(view) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page >= pages:
        raise ValueError("Page out of range")
    meta = payload.get("meta") or {}
    result = {
        "ok": 1, "m": kind, "pg": page, "pages": pages, "total": len(view),
        "d": int(direct), "s": sort[0], "dt": str(meta.get("departure_date") or "")[:10],
        "unverified": int(meta.get("unverified") or 0) if kind == "discover" else 0,
        "rows": [_row(i, offer) for i, offer in view[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]],
    }
    # Never remove offers from a page: hidden rows become unreachable because
    # pagination still advances in groups of PAGE_SIZE.
    try:
        _encode(result)
    except ValueError:
        for row in result["rows"]:
            row["a"] = row["a"][:14]
            row["b"] = row["b"][:14]
            row["c"] = row["c"][:12]
        _encode(result)
    return result


def detail_data(offer: dict, kind: str) -> dict:
    from_place, to_place = _places(offer)
    conditions = ((offer.get("variants") or [{}])[0].get("conditions") or {})
    baggage = conditions.get("baggage") or {}
    cabin = conditions.get("cabin_baggage") or {}
    result = {
        "ok": 1, "detail": 1, "m": kind,
        "a": _safe_title(from_place, 42), "b": _safe_title(to_place, 42),
        "p": _amount(offer), "s": _stops(offer),
        "depart": str(offer.get("departure_at") or "").replace("T", " ")[:24],
        "arrive": str(offer.get("arrival_at") or "").replace("T", " ")[:24],
        "airline": _safe_title(", ".join(str(v) for v in (offer.get("carriers") or [])[:3]), 52),
        "duration": offer.get("duration_min") if type(offer.get("duration_min")) is int else None,
        "fare": _safe_title(conditions.get("fare_family"), 36),
        "bag": baggage.get("pieces") if type(baggage.get("pieces")) is int else None,
        "bagkg": baggage.get("kg") if type(baggage.get("kg")) in (int, float) else None,
        "hand": cabin.get("pieces") if type(cabin.get("pieces")) is int else None,
        "refund": conditions.get("refundable") if type(conditions.get("refundable")) is bool else None,
        "change": conditions.get("changeable") if type(conditions.get("changeable")) is bool else None,
        "visa": bool(kind == "discover" and re.fullmatch(
            r"[A-Z]{2}", str((offer.get("_discovery") or {}).get("country_code") or ""))),
        "checkout": bool(offer.get("checkout_ref")),
    }
    _encode(result)
    return result


def _template() -> str:
    html = _TEMPLATE.read_text(encoding="utf-8")
    raw = html.encode("utf-8")
    if not raw or len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError("Offer widget HTML exceeds Telegram limits")
    return html


async def send_offers_widget(bot, chat_id: int, key: str, payload: dict, kind: str) -> bool:
    if not enabled() or kind not in _MODES or not KEY.fullmatch(key):
        return False
    if (payload.get("meta") or {}).get("kind") != kind or not _authorized(payload, chat_id, chat_id):
        return False
    try:
        initial = _encode(page_data(payload))
        scope = _encode({"k": key})
        html = _template()
    except (ValueError, OSError):
        logger.exception("Could not prepare offer widget")
        return False
    result = await _api(bot, "sendWidget", {
        "chat_id": chat_id, "html": html, "height": 545,
        "allow_queries": True, "is_shared": False,
        "public_data": initial, "chat_data": scope,
        "caption": ("🌍 Подтверждённые билеты — подробный поиск"
                    if kind == "discover" else "🟣 Найденные рейсы Туту"),
    })
    if _widget_confirmed(result):
        logger.info("Offer widget sent: kind=%s, message_id=%s", kind, result["result"].get("message_id"))
        return True
    logger.warning("Falling back to classic ticket carousel: kind=%s", kind)
    return False


async def query_result(query: dict) -> tuple[dict, tuple | None]:
    data = query.get("data")
    if not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
        return {"ok": 0, "error": "Некорректный запрос"}, None
    parts = data.split(":")
    if len(parts) < 3 or parts[0] not in ("of", "od", "oa", "ov"):
        return {"ok": 0, "error": "Неизвестная команда"}, None
    action, key = parts[:2]
    if not KEY.fullmatch(key):
        return {"ok": 0, "error": "Некорректный поиск"}, None
    user = query.get("from") or {}
    chat = query.get("chat") or {}
    try:
        payload = discovery_archive.get(key)
    except Exception:
        logger.exception("Could not read widget offer archive")
        payload = None
    if not payload or not _authorized(payload, user.get("id"), chat.get("id")):
        return {"ok": 0, "error": "Поиск недоступен. Запустите новый."}, None
    kind = (payload.get("meta") or {}).get("kind")
    if kind not in _MODES:
        return {"ok": 0, "error": "Неподдерживаемые результаты"}, None
    if action == "of" and len(parts) == 5:
        pg, direct, sort = parts[2:]
        if (not pg.isascii() or not pg.isdecimal() or len(pg) > 3 or
                direct not in ("0", "1") or sort not in ("p", "s")):
            return {"ok": 0, "error": "Некорректный фильтр"}, None
        try:
            return page_data(payload, int(pg), direct == "1",
                             "price" if sort == "p" else "stops"), None
        except ValueError:
            return {"ok": 0, "error": "Страница недоступна"}, None
    if action in ("od", "oa", "ov") and len(parts) == 3:
        raw_index = parts[2]
        if not raw_index.isascii() or not raw_index.isdecimal() or len(raw_index) > 5:
            return {"ok": 0, "error": "Некорректный билет"}, None
        index = int(raw_index)
        offers = payload.get("offers") or []
        if index >= len(offers) or not isinstance(offers[index], dict):
            return {"ok": 0, "error": "Билет не найден"}, None
        offer = offers[index]
        if _amount(offer) is None:
            return {"ok": 0, "error": "Цена недоступна"}, None
        if action == "od":
            return detail_data(offer, kind), None
        if action == "ov":
            if kind != "discover":
                return {"ok": 0, "error": "Для этого поиска нет визовых сведений"}, None
            discovery = offer.get("_discovery") or {}
            return await _visa(payload, {"country_code": discovery.get("country_code"),
                                         "country": discovery.get("country")}), None
        if not offer.get("checkout_ref"):
            return {"ok": 0, "error": "Ссылки оформления для рейса нет"}, None
        return {"ok": 1, "message": "Кнопки оформления отправляю в чат. Проверьте тариф перед оплатой."}, (
            chat["id"], key, index, kind,
        )
    return {"ok": 0, "error": "Некорректная команда"}, None


async def send_offer_actions(bot, chat_id: int, key: str, index: int, kind: str) -> None:
    """Send existing checkout/profile callbacks; keep booking outside the widget."""
    payload = discovery_archive.get(key)
    if not payload or (payload.get("meta") or {}).get("chat_id") != chat_id:
        return
    if (payload.get("meta") or {}).get("kind") != kind:
        return
    offers = payload.get("offers") or []
    if not 0 <= index < len(offers) or not offers[index].get("checkout_ref"):
        return
    if kind == "discover":
        buy = f"discbuy:{key}:{index}"
        profile = f"discbook:{key}:{index}"
    else:
        # Existing Tutu callbacks address the *price-sorted* position.
        from bot.handlers.tutu import _sort_offers
        ordered = _sort_offers(offers, "price")
        selected = offers[index]
        source = selected.get("_source_index")
        rank = next((i for i, offer in enumerate(ordered)
                     if offer.get("_source_index") == source), None) if source is not None else None
        if rank is None:
            rank = next((i for i, offer in enumerate(ordered) if offer is selected), None)
        if rank is None:
            return
        buy = f"tutulink:{key}:{rank}:price"
        profile = f"tutuprofile:{key}:{rank}:price"
    await bot.send_message(
        chat_id,
        "🎟 Выбранный билет. Перед оплатой проверьте цену, условия и наличие на Туту.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть оформление на Туту", callback_data=buy)],
            [InlineKeyboardButton(text="👤 С моим пассажиром", callback_data=profile)],
        ]),
    )
