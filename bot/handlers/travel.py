import asyncio
import html
import logging
import math
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards.calendar_kb import calendar_keyboard
from bot.services.tutu_mcp import tutu_mcp, tutu_search_cache
from bot.states.search import HotelSearchStates, MultiTransportStates, TripPlannerStates
from bot.utils.parsers import _parse_date

logger = logging.getLogger(__name__)
router = Router()

TRANSPORT_LABELS = {
    "avia": "✈️ Самолёт",
    "railway": "🚆 Поезд",
    "rail": "🚆 Поезд",
    "bus": "🚌 Автобус",
    "etrain": "🚉 Электричка",
}


def travel_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚦 Сравнить транспорт", callback_data="travel:compare")],
        [InlineKeyboardButton(text="🏨 Найти отель", callback_data="travel:hotels")],
        [InlineKeyboardButton(text="🧳 Поездка целиком", callback_data="travel:trip")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:back")],
    ])


@router.message(Command("travel"))
async def cmd_travel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>🧭 Планировщик поездки</b>\n\n"
        "🚦 <b>Сравнить транспорт</b> — самолёт, поезд, автобус и электричка в одном поиске.\n"
        "🏨 <b>Найти отель</b> — варианты на нужные даты.\n"
        "🧳 <b>Поездка целиком</b> — транспорт туда + обратно + отель и общий бюджет.\n\n"
        "Первая версия считает для <b>1 взрослого</b>.",
        reply_markup=travel_menu_keyboard(),
    )


@router.callback_query(F.data == "travel:start")
async def cb_travel_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await cmd_travel(callback.message, state)


@router.callback_query(F.data == "travel:compare")
async def cb_compare_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.set_state(MultiTransportStates.choosing_origin)
    await callback.message.answer(
        "<b>🚦 Сравнение транспорта</b>\n\nВведите город отправления, например: <code>Москва</code>."
    )


@router.message(MultiTransportStates.choosing_origin)
async def compare_origin(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Введите город отправления.")
        return
    await state.update_data(travel_origin=value)
    await state.set_state(MultiTransportStates.choosing_destination)
    await message.answer("Введите город назначения:")


@router.message(MultiTransportStates.choosing_destination)
async def compare_destination(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Введите город назначения.")
        return
    await state.update_data(travel_destination=value)
    await state.set_state(MultiTransportStates.choosing_date)
    await message.answer("Выберите дату поездки:", reply_markup=calendar_keyboard("travelcmp"))


@router.callback_query(F.data.startswith("caldate:travelcmp:"))
async def compare_calendar_date(callback: CallbackQuery, state: FSMContext):
    departure_date = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    origin = data.get("travel_origin")
    destination = data.get("travel_destination")
    if not origin or not destination:
        await callback.answer("Поиск устарел. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(f"Дата: <b>{_fmt_date(departure_date)}</b>")
    await callback.answer()
    await _run_compare(callback.message, origin, destination, departure_date, callback.from_user.id)


@router.message(MultiTransportStates.choosing_date)
async def compare_text_date(message: Message, state: FSMContext):
    departure_date = _parse_date((message.text or "").strip())
    if not departure_date:
        await message.answer("Не удалось распознать дату. Пример: <code>15.10</code>")
        return
    data = await state.get_data()
    await state.clear()
    await _run_compare(
        message, data["travel_origin"], data["travel_destination"], departure_date, message.from_user.id
    )


@router.callback_query(F.data == "travel:hotels")
async def cb_hotels_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.set_state(HotelSearchStates.choosing_city)
    await callback.message.answer(
        "<b>🏨 Поиск отелей</b>\n\nВведите город проживания, например: <code>Казань</code>."
    )


@router.message(HotelSearchStates.choosing_city)
async def hotel_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not city:
        await message.answer("Введите город.")
        return
    await state.update_data(hotel_city=city)
    await state.set_state(HotelSearchStates.choosing_check_in)
    await message.answer("Выберите дату заезда:", reply_markup=calendar_keyboard("hotelcin"))


@router.callback_query(F.data.startswith("caldate:hotelcin:"))
async def hotel_checkin_calendar(callback: CallbackQuery, state: FSMContext):
    check_in = (callback.data or "").split(":", 2)[2]
    await state.update_data(hotel_check_in=check_in)
    await state.set_state(HotelSearchStates.choosing_check_out)
    await callback.message.edit_text(
        f"Заезд: <b>{_fmt_date(check_in)}</b>\nТеперь выберите дату выезда:",
        reply_markup=calendar_keyboard("hotelcout"),
    )
    await callback.answer()


@router.message(HotelSearchStates.choosing_check_in)
async def hotel_checkin_text(message: Message, state: FSMContext):
    check_in = _parse_date((message.text or "").strip())
    if not check_in:
        await message.answer("Не удалось распознать дату. Пример: <code>15.10</code>")
        return
    await state.update_data(hotel_check_in=check_in)
    await state.set_state(HotelSearchStates.choosing_check_out)
    await message.answer("Выберите дату выезда:", reply_markup=calendar_keyboard("hotelcout"))


@router.callback_query(F.data.startswith("caldate:hotelcout:"))
async def hotel_checkout_calendar(callback: CallbackQuery, state: FSMContext):
    check_out = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    if not _date_after(check_out, data.get("hotel_check_in")):
        await callback.answer("Дата выезда должна быть позже даты заезда.", show_alert=True)
        return
    city = data.get("hotel_city")
    check_in = data.get("hotel_check_in")
    if not city or not check_in:
        await callback.answer("Поиск устарел. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"{html.escape(city)} · {_fmt_date(check_in)} — {_fmt_date(check_out)}"
    )
    await callback.answer()
    await _run_hotels(callback.message, city, check_in, check_out, callback.from_user.id)


@router.message(HotelSearchStates.choosing_check_out)
async def hotel_checkout_text(message: Message, state: FSMContext):
    check_out = _parse_date((message.text or "").strip())
    data = await state.get_data()
    check_in = data.get("hotel_check_in")
    if not check_out or not _date_after(check_out, check_in):
        await message.answer("Дата выезда должна быть позже даты заезда.")
        return
    city = data["hotel_city"]
    await state.clear()
    await _run_hotels(message, city, check_in, check_out, message.from_user.id)


@router.callback_query(F.data == "travel:trip")
async def cb_trip_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.set_state(TripPlannerStates.choosing_origin)
    await callback.message.answer(
        "<b>🧳 Поездка целиком</b>\n\nВведите город отправления:"
    )


@router.message(TripPlannerStates.choosing_origin)
async def trip_origin(message: Message, state: FSMContext):
    origin = (message.text or "").strip()
    if not origin:
        await message.answer("Введите город отправления.")
        return
    await state.update_data(trip_origin=origin)
    await state.set_state(TripPlannerStates.choosing_destination)
    await message.answer("Введите город назначения:")


@router.message(TripPlannerStates.choosing_destination)
async def trip_destination(message: Message, state: FSMContext):
    destination = (message.text or "").strip()
    if not destination:
        await message.answer("Введите город назначения.")
        return
    await state.update_data(trip_destination=destination)
    await state.set_state(TripPlannerStates.choosing_departure)
    await message.answer("Выберите дату отправления:", reply_markup=calendar_keyboard("tripout"))


@router.callback_query(F.data.startswith("caldate:tripout:"))
async def trip_departure_calendar(callback: CallbackQuery, state: FSMContext):
    departure = (callback.data or "").split(":", 2)[2]
    await state.update_data(trip_departure=departure)
    await state.set_state(TripPlannerStates.choosing_return)
    await callback.message.edit_text(
        f"Туда: <b>{_fmt_date(departure)}</b>\nВыберите дату возвращения:",
        reply_markup=calendar_keyboard("tripback"),
    )
    await callback.answer()


@router.message(TripPlannerStates.choosing_departure)
async def trip_departure_text(message: Message, state: FSMContext):
    departure = _parse_date((message.text or "").strip())
    if not departure:
        await message.answer("Не удалось распознать дату.")
        return
    await state.update_data(trip_departure=departure)
    await state.set_state(TripPlannerStates.choosing_return)
    await message.answer("Выберите дату возвращения:", reply_markup=calendar_keyboard("tripback"))


@router.callback_query(F.data.startswith("caldate:tripback:"))
async def trip_return_calendar(callback: CallbackQuery, state: FSMContext):
    return_date = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    departure = data.get("trip_departure")
    if not _date_after(return_date, departure):
        await callback.answer("Возвращение должно быть позже отправления.", show_alert=True)
        return
    origin = data.get("trip_origin")
    destination = data.get("trip_destination")
    if not origin or not destination or not departure:
        await callback.answer("Поиск устарел. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"{html.escape(origin)} → {html.escape(destination)} · "
        f"{_fmt_date(departure)} — {_fmt_date(return_date)}"
    )
    await callback.answer()
    await _run_trip(callback.message, origin, destination, departure, return_date, callback.from_user.id)


@router.message(TripPlannerStates.choosing_return)
async def trip_return_text(message: Message, state: FSMContext):
    return_date = _parse_date((message.text or "").strip())
    data = await state.get_data()
    departure = data.get("trip_departure")
    if not return_date or not _date_after(return_date, departure):
        await message.answer("Дата возвращения должна быть позже даты отправления.")
        return
    await state.clear()
    await _run_trip(
        message,
        data["trip_origin"],
        data["trip_destination"],
        departure,
        return_date,
        message.from_user.id,
    )


async def _run_compare(
    message: Message,
    origin: str,
    destination: str,
    departure_date: str,
    owner_id: int,
) -> None:
    status = await message.answer(
        f"🚦 Сравниваю транспорт <b>{html.escape(origin)} → {html.escape(destination)}</b> "
        f"на {_fmt_date(departure_date)}…"
    )
    results = await asyncio.gather(
        tutu_mcp.search_multitransport(origin, destination, departure_date, optimize_for="price"),
        tutu_mcp.search_multitransport(origin, destination, departure_date, optimize_for="time"),
        return_exceptions=True,
    )
    payloads = [item for item in results if isinstance(item, dict)]
    if not payloads:
        logger.warning("Multitransport search failed: %r", results)
        await status.edit_text("Туту не смог сравнить транспорт по этому маршруту. Попробуйте позже.")
        return

    variants = _merge_variants(payloads)
    if not variants:
        await status.edit_text("На эту дату варианты транспорта не найдены.")
        return

    meta = payloads[0].get("meta") or {}
    key = tutu_search_cache.put({
        "offers": variants,
        "meta": {
            "owner_id": owner_id,
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "kind": "multitransport",
        },
    })
    await status.edit_text(
        _format_compare(origin, destination, departure_date, variants, meta),
        disable_web_page_preview=True,
        reply_markup=_transport_keyboard(key, variants[:6]),
    )


async def _run_hotels(
    message: Message,
    city: str,
    check_in: str,
    check_out: str,
    owner_id: int,
) -> None:
    status = await message.answer(
        f"🏨 Ищу отели в <b>{html.escape(city)}</b> на "
        f"{_fmt_date(check_in)} — {_fmt_date(check_out)}…"
    )
    try:
        payload = await tutu_mcp.search_hotels(
            city, check_in, check_out, adults=1, page_size=10
        )
    except Exception as exc:
        logger.warning("Hotel search failed: %s", exc)
        await status.edit_text("Туту не смог получить отели. Попробуйте позже.")
        return

    hotels = [item for item in payload.get("hotels") or [] if _hotel_price(item) is not None]
    hotels.sort(key=_hotel_price)
    if not hotels:
        await status.edit_text("На эти даты в полученной выдаче отелей не найдено.")
        return

    stay = payload.get("stay") or {}
    nights = stay.get("nights") or _nights(check_in, check_out)
    key = tutu_search_cache.put({
        "hotels": hotels,
        "meta": {
            "owner_id": owner_id,
            "kind": "hotels",
            "city": city,
            "check_in": check_in,
            "check_out": check_out,
        },
    })
    await status.edit_text(
        _format_hotels(city, check_in, check_out, nights, hotels[:5]),
        disable_web_page_preview=True,
        reply_markup=_hotel_keyboard(key, hotels[:5]),
    )


async def _run_trip(
    message: Message,
    origin: str,
    destination: str,
    departure: str,
    return_date: str,
    owner_id: int,
) -> None:
    status = await message.answer(
        "<b>🧳 Собираю поездку целиком</b>\n"
        "Сравниваю транспорт туда и обратно и ищу отель…"
    )
    outbound, inbound, hotels_payload = await asyncio.gather(
        tutu_mcp.search_multitransport(origin, destination, departure, optimize_for="price", page_size=10),
        tutu_mcp.search_multitransport(destination, origin, return_date, optimize_for="price", page_size=10),
        tutu_mcp.search_hotels(destination, departure, return_date, adults=1, page_size=10),
        return_exceptions=True,
    )

    outbound_variants = _priced_variants(outbound if isinstance(outbound, dict) else {})
    inbound_variants = _priced_variants(inbound if isinstance(inbound, dict) else {})
    hotels = [
        item for item in ((hotels_payload.get("hotels") or []) if isinstance(hotels_payload, dict) else [])
        if _hotel_price(item) is not None
    ]
    hotels.sort(key=_hotel_price)

    best_out = outbound_variants[0] if outbound_variants else None
    best_back = inbound_variants[0] if inbound_variants else None
    best_hotel = hotels[0] if hotels else None

    if not any((best_out, best_back, best_hotel)):
        logger.warning("Trip planner failed: outbound=%r inbound=%r hotels=%r", outbound, inbound, hotels_payload)
        await status.edit_text("Не удалось собрать варианты поездки. Попробуйте позже.")
        return

    components = [best_out, best_back, best_hotel]
    key = tutu_search_cache.put({
        "trip_components": components,
        "meta": {"owner_id": owner_id, "kind": "trip"},
    })
    await status.edit_text(
        _format_trip(origin, destination, departure, return_date, best_out, best_back, best_hotel),
        disable_web_page_preview=True,
        reply_markup=_trip_keyboard(key, best_out, best_back, best_hotel),
    )


@router.callback_query(F.data.startswith("travelbuy:"))
async def cb_travel_buy(callback: CallbackQuery):
    try:
        _, key, raw_index = (callback.data or "").split(":", 2)
        index = int(raw_index)
    except (ValueError, TypeError):
        await callback.answer("Некорректный вариант.", show_alert=True)
        return

    payload = tutu_search_cache.get(key) or {}
    meta = payload.get("meta") or {}
    if meta.get("owner_id") not in (None, callback.from_user.id):
        await callback.answer("Этот результат доступен только автору поиска.", show_alert=True)
        return

    items = payload.get("offers") or payload.get("hotels") or payload.get("trip_components") or []
    if index < 0 or index >= len(items) or not isinstance(items[index], dict):
        await callback.answer("Вариант уже недоступен.", show_alert=True)
        return

    item = items[index]
    url = _item_url(item)
    if url:
        await callback.answer("Откройте ссылку кнопкой в новом сообщении.")
        await callback.message.answer(
            "Проверьте итоговую цену и условия на Туту перед оплатой.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть на Туту", url=url)
            ]]),
        )
        return

    checkout_ref = item.get("checkout_ref") or {}
    if not checkout_ref:
        await callback.answer("Для этого варианта Туту не вернул ссылку.", show_alert=True)
        return

    await callback.answer("Готовлю ссылку Туту…")
    try:
        result = await tutu_mcp.create_checkout_link(checkout_ref)
    except Exception as exc:
        logger.warning("Travel checkout link failed: %s", exc)
        await callback.message.answer("Не удалось сформировать ссылку. Повторите поиск.")
        return
    url = result.get("checkout_url") or result.get("search_results_url")
    if not url:
        await callback.message.answer("Туту не вернул ссылку для этого варианта.")
        return
    await callback.message.answer(
        "Проверьте итоговую цену и условия на Туту перед оплатой.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть на Туту", url=url)
        ]]),
    )


def _priced_variants(payload: dict) -> list[dict]:
    variants = [v for v in payload.get("variants") or [] if _price(v) is not None]
    return sorted(variants, key=lambda v: (_price(v), _duration_value(v)))


def _merge_variants(payloads: list[dict]) -> list[dict]:
    merged = {}
    for payload in payloads:
        for item in payload.get("variants") or []:
            if not isinstance(item, dict) or _price(item) is None:
                continue
            key = str(item.get("offer_id") or (
                item.get("transport"), item.get("departure_at"), _price(item)
            ))
            merged[key] = item
    return sorted(merged.values(), key=lambda v: (_price(v), _duration_value(v)))


def _format_compare(origin: str, destination: str, date_value: str, variants: list[dict], meta: dict) -> str:
    lines = [
        f"<b>🚦 {html.escape(origin)} → {html.escape(destination)}</b>",
        f"📅 {_fmt_date(date_value)} · 1 взрослый",
        "",
        "<b>По видам транспорта:</b>",
    ]
    summary = meta.get("modes_summary") or {}
    for mode in ("avia", "railway", "bus", "etrain"):
        info = summary.get(mode) or {}
        count = info.get("count") or 0
        if count:
            price = info.get("min_price")
            duration = info.get("min_duration_min")
            bits = []
            if price is not None:
                bits.append(f"от {_money(price)}")
            if duration is not None:
                bits.append(f"от {_duration(duration)}")
            lines.append(f"{TRANSPORT_LABELS[mode]} — {' · '.join(bits)}")
        else:
            lines.append(f"{TRANSPORT_LABELS[mode]} — нет в полученной выдаче")

    lines.extend(["", "<b>Самые дешёвые варианты:</b>"])
    for i, item in enumerate(variants[:6], 1):
        lines.append(
            f"{i}. {_transport_name(item)} — <b>{_money(_price(item))}</b> · "
            f"{_duration(item.get('duration_min'))} · {_fmt_time(item.get('departure_at'))}"
        )
    unavailable = meta.get("unavailable") or []
    if unavailable:
        lines.append("\n⚠️ Часть источников Туту временно недоступна.")
    lines.append("\nЦена и наличие актуальны на момент поиска.")
    return "\n".join(lines)


def _format_hotels(city: str, check_in: str, check_out: str, nights: int, hotels: list[dict]) -> str:
    lines = [
        f"<b>🏨 Отели · {html.escape(city)}</b>",
        f"📅 {_fmt_date(check_in)} — {_fmt_date(check_out)} · {nights} ноч.",
        "👤 1 взрослый",
        "",
        "<b>Недорогие варианты из полученной выдачи:</b>",
    ]
    for i, hotel in enumerate(hotels, 1):
        offer = hotel.get("best_offer") or {}
        stars = int(hotel.get("stars") or 0)
        rating = hotel.get("rating")
        extra = []
        if stars:
            extra.append("★" * stars)
        if rating:
            extra.append(f"рейтинг {rating}/10")
        if offer.get("breakfast_included") is True:
            extra.append("завтрак")
        if offer.get("free_cancellation") is True:
            extra.append("бесплатная отмена")
        suffix = f" · {' · '.join(extra)}" if extra else ""
        lines.append(
            f"{i}. <b>{html.escape(str(hotel.get('name') or 'Отель'))}</b> — "
            f"<b>{_money(_hotel_price(hotel))}</b> за всё проживание{suffix}"
        )
    lines.append("\n⚠️ Итоговую цену и условия проверьте на Туту.")
    return "\n".join(lines)


def _format_trip(
    origin: str,
    destination: str,
    departure: str,
    return_date: str,
    outbound: dict | None,
    inbound: dict | None,
    hotel: dict | None,
) -> str:
    lines = [
        f"<b>🧳 {html.escape(origin)} ⇄ {html.escape(destination)}</b>",
        f"📅 {_fmt_date(departure)} — {_fmt_date(return_date)} · 1 взрослый",
        "",
    ]
    total = 0.0
    complete = True

    if outbound:
        value = _price(outbound)
        total += value or 0
        lines.append(
            f"➡️ Туда: {_transport_name(outbound)} · <b>{_money(value)}</b> · "
            f"{_duration(outbound.get('duration_min'))}"
        )
    else:
        complete = False
        lines.append("➡️ Туда: вариант не получен")

    if inbound:
        value = _price(inbound)
        total += value or 0
        lines.append(
            f"⬅️ Обратно: {_transport_name(inbound)} · <b>{_money(value)}</b> · "
            f"{_duration(inbound.get('duration_min'))}"
        )
    else:
        complete = False
        lines.append("⬅️ Обратно: вариант не получен")

    if hotel:
        value = _hotel_price(hotel)
        total += value or 0
        lines.append(
            f"🏨 {html.escape(str(hotel.get('name') or 'Отель'))}: "
            f"<b>{_money(value)}</b> за всё проживание"
        )
    else:
        complete = False
        lines.append("🏨 Отель: вариант не получен")

    lines.append("")
    if complete:
        lines.append(f"💰 <b>Ориентировочный минимум: {_money(total)}</b>")
        lines.append("Это сумма трёх отдельных бронирований: туда + обратно + отель.")
    else:
        lines.append("Часть компонентов не получена, поэтому общий бюджет не считаю.")

    lines.append("⚠️ Цены и наличие могут измениться до оформления.")
    return "\n".join(lines)


def _transport_keyboard(key: str, variants: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, item in enumerate(variants):
        label = f"{i + 1}. {_transport_name(item)} · {_money(_price(item))}"
        url = _item_url(item)
        if url:
            rows.append([InlineKeyboardButton(text=label, url=url)])
        elif item.get("checkout_ref"):
            rows.append([InlineKeyboardButton(text=label, callback_data=f"travelbuy:{key}:{i}")])
    rows.append([InlineKeyboardButton(text="🧭 Новый поиск", callback_data="travel:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hotel_keyboard(key: str, hotels: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, item in enumerate(hotels):
        label = f"{i + 1}. {str(item.get('name') or 'Отель')[:28]} · {_money(_hotel_price(item))}"
        url = _item_url(item)
        if url:
            rows.append([InlineKeyboardButton(text=label, url=url)])
        elif item.get("checkout_ref"):
            rows.append([InlineKeyboardButton(text=label, callback_data=f"travelbuy:{key}:{i}")])
    rows.append([InlineKeyboardButton(text="🧭 Новый поиск", callback_data="travel:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _trip_keyboard(key: str, outbound: dict | None, inbound: dict | None, hotel: dict | None) -> InlineKeyboardMarkup:
    rows = []
    items = [outbound, inbound, hotel]
    labels = ["➡️ Оформить туда", "⬅️ Оформить обратно", "🏨 Открыть отель"]
    for index, (label, item) in enumerate(zip(labels, items)):
        if not item:
            continue
        url = _item_url(item)
        if url:
            rows.append([InlineKeyboardButton(text=label, url=url)])
        elif item.get("checkout_ref"):
            rows.append([InlineKeyboardButton(text=label, callback_data=f"travelbuy:{key}:{index}")])
    rows.append([InlineKeyboardButton(text="🧭 Новый расчёт", callback_data="travel:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _transport_name(item: dict) -> str:
    return TRANSPORT_LABELS.get(str(item.get("transport") or "").lower(), "🧭 Транспорт")


def _price(item: dict) -> float | None:
    try:
        info = item.get("price") or {}
        if info.get("currency") not in (None, "RUB"):
            return None
        value = float(info.get("amount"))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _hotel_price(item: dict) -> float | None:
    try:
        info = (item.get("best_offer") or {}).get("price") or {}
        if info.get("currency") not in (None, "RUB"):
            return None
        value = float(info.get("amount"))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _item_url(item: dict) -> str | None:
    best = item.get("best_offer") or {}
    value = (
        item.get("checkout_url")
        or best.get("checkout_url")
        or item.get("search_results_url")
        or (item.get("checkout_ref") or {}).get("fallback_url")
    )
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return None


def _money(value: float | int | None) -> str:
    if value is None:
        return "цена не указана"
    return f"{float(value):,.0f} ₽".replace(",", " ")


def _duration(value: int | float | None) -> str:
    if value is None:
        return "время не указано"
    minutes = int(value)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def _duration_value(item: dict) -> int:
    try:
        return int(item.get("duration_min") or 10**9)
    except (TypeError, ValueError):
        return 10**9


def _fmt_time(value: str | None) -> str:
    if not value:
        return "время не указано"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return str(value)


def _fmt_date(value: str) -> str:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return str(value)


def _date_after(later: str | None, earlier: str | None) -> bool:
    try:
        return datetime.strptime(later, "%Y-%m-%d").date() > datetime.strptime(earlier, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False


def _nights(check_in: str, check_out: str) -> int:
    try:
        start = datetime.strptime(check_in, "%Y-%m-%d").date()
        end = datetime.strptime(check_out, "%Y-%m-%d").date()
        return max(1, (end - start).days)
    except ValueError:
        return 1
