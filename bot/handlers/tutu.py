import html
import logging
import math
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.tutu_mcp import (
    tutu_checkout_cache,
    tutu_mcp,
    tutu_search_cache,
)
from bot.services.discovery_archive import can_open_result, discovery_archive
from bot.states.search import TutuSearchStates
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.utils.formatters import format_price
from bot.utils.parsers import _parse_date

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("tutu"))
async def cmd_tutu(message: Message, state: FSMContext):
    await _start_tutu_search(message, state)


@router.callback_query(F.data == "tutu:start")
async def cb_tutu_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _start_tutu_search(callback.message, state)


async def _start_tutu_search(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(TutuSearchStates.choosing_origin)
    await message.answer(
        "<b>Поиск через Туту</b>\n\n"
        "Введите город или конкретный аэропорт вылета.\n"
        "Например: <code>Москва</code>, <code>Шереметьево</code> или <code>SVO</code>."
    )


@router.message(TutuSearchStates.choosing_origin)
async def tutu_origin(message: Message, state: FSMContext):
    origin = (message.text or "").strip()
    if not origin:
        await message.answer("Введите город или аэропорт вылета.")
        return
    await state.update_data(tutu_origin=origin)
    await state.set_state(TutuSearchStates.choosing_destination)
    await message.answer("Введите город или аэропорт прибытия:")


@router.message(TutuSearchStates.choosing_destination)
async def tutu_destination(message: Message, state: FSMContext):
    destination = (message.text or "").strip()
    if not destination:
        await message.answer("Введите город или аэропорт прибытия.")
        return
    await state.update_data(tutu_destination=destination)
    await state.set_state(TutuSearchStates.choosing_date)
    await message.answer(
        "Выберите дату вылета в календаре. При желании дату можно ввести вручную:",
        reply_markup=calendar_keyboard("tutu"),
    )


@router.callback_query(F.data.startswith("caldate:tutu:"))
async def tutu_calendar_date(callback: CallbackQuery, state: FSMContext):
    departure_date = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    origin = data.get("tutu_origin")
    destination = data.get("tutu_destination")
    if not origin or not destination:
        await callback.answer("Поиск устарел. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"Выбрана дата: <b>{_format_date(departure_date)}</b>"
    )
    await callback.answer()
    await _run_tutu_search(callback.message, origin, destination, departure_date,
                           owner_id=callback.from_user.id)


@router.message(TutuSearchStates.choosing_date)
async def tutu_date(message: Message, state: FSMContext):
    departure_date = _parse_date((message.text or "").strip())
    if not departure_date:
        await message.answer("Не удалось распознать дату. Пример: <code>01.10</code>")
        return

    data = await state.get_data()
    await state.clear()
    await _run_tutu_search(
        message,
        data["tutu_origin"],
        data["tutu_destination"],
        departure_date,
    )


async def _run_tutu_search(message: Message, origin: str, destination: str, departure_date: str,
                           owner_id: int | None = None):
    await message.answer(
        f"Ищу на Туту: <b>{html.escape(origin)} → {html.escape(destination)}</b> "
        f"на {_format_date(departure_date)}…"
    )
    try:
        payload = await tutu_mcp.search_avia_all(origin, destination, departure_date)
    except Exception as exc:
        logger.warning("Tutu search failed: %s", exc)
        await message.answer(
            "Туту сейчас не ответил или не смог распознать маршрут. "
            "Проверьте названия городов/аэропортов и попробуйте ещё раз через /tutu."
        )
        return

    offers = _sort_offers(payload.get("offers") or [], "price")
    meta = payload.get("meta") or {}
    if not offers:
        note = meta.get("airport_note") or meta.get("upstream_note")
        suffix = f"\n\n{html.escape(str(note))}" if note else ""
        if meta.get("complete") is False:
            suffix += "\n⚠️ Выдача Туту неполная; отсутствие билетов не подтверждено."
        await message.answer(
            "На Туту не найдено билетов в продаже на эту дату." + suffix
        )
        return

    resolved_from = (meta.get("from") or {}).get("name", origin)
    resolved_to = (meta.get("to") or {}).get("name", destination)
    total = len(offers)
    direct_count = sum(_transfer_count(offer) == 0 for offer in offers)
    direct_line = (f"Прямых: {direct_count}." if direct_count else
                   "Найдено прямых: 0 среди полученных предложений.")
    coverage_note = ("\n⚠️ Выдача Туту неполная: другие прямые рейсы могут не попасть в список. "
                     "Смотрите все результаты на сайте Туту." if meta.get("complete") is False else "")
    await message.answer(
        f"<b>Туту: {html.escape(str(resolved_from))} → {html.escape(str(resolved_to))}</b>\n"
        f"Найдено вариантов: {total}. {direct_line}\n"
        "Сначала прямые; сортировку по цене можно выбрать под карточкой. "
        "Цена в карточке — итог за 1 взрослого."
        f"{coverage_note}"
    )

    for source_index, offer in enumerate(offers):
        offer["_source_index"] = source_index
    payload["offers"] = offers
    scope = {"owner_id": owner_id if owner_id is not None else
             getattr(getattr(message, "from_user", None), "id", None),
             "chat_id": getattr(getattr(message, "chat", None), "id", None)}
    payload["meta"] = {**meta, **scope}
    search_key = tutu_search_cache.put(payload)
    archive_saved = True
    try:
        discovery_archive.put(search_key, {
            "offers": offers,
            "booking_summaries": [_booking_summary(offer) for offer in offers],
            "meta": {"origin": origin, "departure_date": departure_date, **scope},
        })
    except Exception:
        archive_saved = False
        logger.exception("Could not archive Tutu results")
    ordered = _sort_offers(offers, "stops")
    await message.answer(
        _format_carousel_card(ordered[0], 0, len(ordered), "stops")
        + ("\n⚠️ Не удалось сохранить результат; кнопка может перестать работать после перезапуска."
           if not archive_saved else ""),
        disable_web_page_preview=True,
        reply_markup=_carousel_keyboard(search_key, ordered, 0, "stops"),
    )


@router.callback_query(F.data.startswith("tutupage:"))
async def cb_tutu_page(callback: CallbackQuery):
    try:
        _, search_key, index_raw, sort_mode = callback.data.split(":", 3)
        index = int(index_raw)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная страница", show_alert=True)
        return
    await _show_carousel_page(callback, search_key, index, sort_mode)


@router.callback_query(F.data.startswith("tutusort:"))
async def cb_tutu_sort(callback: CallbackQuery):
    try:
        _, search_key, offer_id, sort_mode = callback.data.split(":", 3)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная сортировка", show_alert=True)
        return
    payload = tutu_search_cache.get(search_key) or discovery_archive.get(search_key)
    if not payload:
        await callback.answer("Сохранённые результаты не найдены.", show_alert=True)
        return
    if not can_open_result(payload, callback):
        await callback.answer("Этот поиск доступен только его автору.", show_alert=True)
        return
    offers = _sort_offers(payload.get("offers") or [], sort_mode)
    if offer_id.isdecimal():
        index = next((i for i, offer in enumerate(offers)
                      if offer.get("_source_index") == int(offer_id)), None)
    else:  # Old Telegram buttons; never guess if the truncated prefix is ambiguous.
        matches = [i for i, offer in enumerate(offers)
                   if str(offer.get("offer_id") or "").startswith(offer_id)]
        index = matches[0] if offer_id and len(matches) == 1 else None
    if index is None:
        await callback.answer("Нельзя однозначно определить билет.", show_alert=True)
        return
    await _show_carousel_page(callback, search_key, index, sort_mode)


@router.callback_query(F.data == "tutunoop")
async def cb_tutu_noop(callback: CallbackQuery):
    await callback.answer()


async def _show_carousel_page(
    callback: CallbackQuery,
    search_key: str,
    index: int,
    sort_mode: str,
):
    cached = tutu_search_cache.get(search_key)
    payload = cached or discovery_archive.get(search_key)
    if not payload:
        await callback.answer("Сохранённые результаты не найдены.", show_alert=True)
        return
    if not can_open_result(payload, callback):
        await callback.answer("Этот поиск доступен только его автору.", show_alert=True)
        return
    offers = _sort_offers(payload.get("offers") or [], sort_mode)
    if not offers:
        await callback.answer("Варианты не найдены", show_alert=True)
        return
    index = max(0, min(index, len(offers) - 1))
    card = _format_carousel_card(offers[index], index, len(offers), sort_mode)
    if cached is None:
        card += "\nСохранённый результат; кнопка ссылки остаётся доступной."
    await callback.message.edit_text(
        card,
        disable_web_page_preview=True,
        reply_markup=_carousel_keyboard(search_key, offers, index, sort_mode),
    )
    await callback.answer()


def _transfer_count(offer: dict) -> int:
    legs = offer.get("legs") or []
    if not legs or any(not isinstance(leg, dict) or not (leg.get("segments") or []) for leg in legs):
        return 10**9  # Unknown itinerary must not masquerade as a nonstop flight.
    return sum(len(leg["segments"]) - 1 for leg in legs)


def _sort_offers(offers: list[dict], sort_mode: str) -> list[dict]:
    def price(offer: dict) -> float | None:
        try:
            info = offer.get("price") or {}
            if info.get("currency") not in (None, "RUB"):
                return None
            amount = float(info.get("amount"))
            return amount if math.isfinite(amount) and amount > 0 else None
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    offers = [offer for offer in offers if isinstance(offer, dict) and price(offer) is not None]

    if sort_mode == "stops":
        return sorted(
            offers,
            key=lambda offer: (
                _transfer_count(offer),
                price(offer),
                offer.get("departure_at") or "",
            ),
        )
    return sorted(
        offers,
        key=lambda offer: (
            price(offer),
            _transfer_count(offer),
            offer.get("departure_at") or "",
        ),
    )


def _format_carousel_card(offer: dict, index: int, total: int, sort_mode: str) -> str:
    sort_label = "по пересадкам" if sort_mode == "stops" else "по цене"
    return (
        f"<b>Вариант {index + 1} из {total}</b> · сортировка {sort_label}\n\n"
        f"{_format_offer(offer)}\n\n"
        "⚠️ Цена на момент поиска; Туту может изменить цену и доступность. "
        "Проверьте итог перед оплатой."
    )


def _carousel_keyboard(
    search_key: str,
    offers: list[dict],
    index: int,
    sort_mode: str,
) -> InlineKeyboardMarkup:
    offer = offers[index]
    rows: list[list[InlineKeyboardButton]] = []

    checkout_ref = offer.get("checkout_ref") or {}
    if checkout_ref:
        rows.append([
            InlineKeyboardButton(
                text="Купить этот билет на Туту",
                callback_data=f"tutulink:{search_key}:{index}:{sort_mode}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="👤 Оформить с моим пассажиром",
                callback_data=f"tutuprofile:{search_key}:{index}:{sort_mode}",
            )
        ])

    previous_cb = f"tutupage:{search_key}:{index - 1}:{sort_mode}" if index > 0 else "tutunoop"
    next_cb = f"tutupage:{search_key}:{index + 1}:{sort_mode}" if index + 1 < len(offers) else "tutunoop"
    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=previous_cb),
        InlineKeyboardButton(text=f"{index + 1}/{len(offers)}", callback_data="tutunoop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ])

    offer_token = str(offer.get("_source_index", index))
    price_label = "✅ По цене" if sort_mode == "price" else "По цене"
    stops_label = "✅ По пересадкам" if sort_mode == "stops" else "По пересадкам"
    rows.append([
        InlineKeyboardButton(
            text=price_label,
            callback_data=f"tutusort:{search_key}:{offer_token}:price",
        ),
        InlineKeyboardButton(
            text=stops_label,
            callback_data=f"tutusort:{search_key}:{offer_token}:stops",
        ),
    ])

    search_url = offer.get("search_results_url") or checkout_ref.get("search_results_url")
    if search_url:
        rows.append([InlineKeyboardButton(text="Все результаты на сайте Туту", url=search_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _selected_saved_tutu_offer(callback: CallbackQuery) -> dict | None:
    try:
        _, key, raw_index, sort_mode = (callback.data or "").split(":", 3)
        index = int(raw_index)
    except (ValueError, TypeError):
        await callback.answer("Некорректный билет", show_alert=True)
        return None
    if sort_mode not in ("price", "stops"):
        await callback.answer("Некорректная сортировка", show_alert=True)
        return None
    try:
        payload = tutu_search_cache.get(key) or discovery_archive.get(key)
    except Exception:
        logger.exception("Could not read saved Tutu offer")
        payload = None
    if payload and not can_open_result(payload, callback):
        await callback.answer("Этот поиск доступен только его автору.", show_alert=True)
        return None
    offers = _sort_offers((payload or {}).get("offers") or [], sort_mode)
    if index < 0 or index >= len(offers) or not offers[index].get("checkout_ref"):
        await callback.answer("Ссылка на билет не найдена.", show_alert=True)
        return None
    return offers[index]


@router.callback_query(F.data.startswith("tutulink:"))
async def cb_tutu_saved_link(callback: CallbackQuery):
    offer = await _selected_saved_tutu_offer(callback)
    if offer is not None:
        await _send_checkout_link(callback, offer["checkout_ref"])


@router.callback_query(F.data.startswith("tutuprofile:"))
async def cb_tutu_saved_profile(callback: CallbackQuery):
    offer = await _selected_saved_tutu_offer(callback)
    if offer is not None:
        from bot.handlers.profile import _book_with_profile
        await _book_with_profile(callback, {
            "checkout_ref": offer["checkout_ref"],
            "offer_summary": offer.get("booking_summary") or _booking_summary(offer),
        })


@router.callback_query(F.data.startswith("tutubuy:"))
async def cb_tutu_buy(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    checkout_ref = tutu_checkout_cache.get(key)
    if not checkout_ref:
        await callback.answer("Код из старого сообщения недоступен. Откройте новый результат поиска.", show_alert=True)
        return

    await _send_checkout_link(callback, checkout_ref)


async def _send_checkout_link(callback: CallbackQuery, checkout_ref: dict):
    """Build a provider link on explicit click; do not re-search or book here."""
    await callback.answer("Готовлю ссылку Туту…")
    try:
        result = await tutu_mcp.create_checkout_link(checkout_ref)
    except Exception as exc:
        logger.warning("Tutu checkout link failed: %s", exc)
        await callback.message.answer(
            "Не удалось сформировать точную ссылку. Повторите поиск через /tutu."
        )
        return

    kind = result.get("kind")
    if kind == "deeplink_parts" and result.get("checkout_parts"):
        rows = []
        for part in result["checkout_parts"]:
            url = part.get("checkout_url")
            if url:
                label = f"Билет {part.get('ticket', len(rows) + 1)} из {part.get('of', '?')}"
                rows.append([InlineKeyboardButton(text=label, url=url)])
        if rows:
            await callback.message.answer(
                "Маршрут оформляется отдельными билетами и отдельными платежами:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return

    checkout_url = result.get("checkout_url")
    fallback_url = result.get("search_results_url") or checkout_ref.get("search_results_url")
    rows = []
    if checkout_url:
        rows.append([InlineKeyboardButton(text="Перейти к оформлению на Туту", url=checkout_url)])
    if fallback_url and fallback_url != checkout_url:
        rows.append([InlineKeyboardButton(text="Открыть результаты поиска", url=fallback_url)])

    if not rows:
        await callback.message.answer("Туту не вернул ссылку. Повторите поиск через /tutu.")
        return

    note = (
        "Откройте ссылку и проверьте рейс, тариф, пассажиров и итоговую цену перед оплатой."
    )
    if kind == "search_redirect":
        note = "Откроется страница результатов Туту — выберите нужный рейс там."
    await callback.message.answer(note, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _format_date(value: str) -> str:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def _format_datetime(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, AttributeError):
        return value or "—"


def _format_duration(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours} ч {mins} мин" if mins else f"{hours} ч"


def _format_baggage(conditions: dict) -> str:
    baggage = conditions.get("baggage") or {}
    cabin = conditions.get("cabin_baggage") or {}
    pieces = baggage.get("pieces")
    kg = baggage.get("kg")
    if pieces == 0:
        checked = "без багажа"
    elif pieces is None and kg is None:
        checked = "багаж не указан"
    else:
        checked = f"багаж: {pieces or 1} мест."
        if kg is not None:
            checked += f" до {kg} кг"

    cabin_pieces = cabin.get("pieces")
    cabin_kg = cabin.get("kg")
    dimensions = cabin.get("dimensions")
    if cabin_pieces is None and cabin_kg is None and not dimensions:
        hand = "ручная кладь не указана"
    else:
        hand = f"ручная кладь: {cabin_pieces or 1} мест."
        if cabin_kg is not None:
            hand += f" до {cabin_kg} кг"
        if dimensions:
            hand += f" ({dimensions})"
    return f"{checked}; {hand}"


def _plain_location(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("city") or value.get("code") or "—")
    return str(value or "—")


def _booking_summary(offer: dict) -> dict:
    """Non-PII trip facts shown in the secure confirmation page."""
    legs = offer.get("legs") or []
    segments = []
    for leg in legs:
        for segment in (leg or {}).get("segments") or []:
            segments.append({
                "from": _plain_location(segment.get("from")),
                "to": _plain_location(segment.get("to")),
                "departure_at": segment.get("departure_at"),
                "arrival_at": segment.get("arrival_at"),
                "flight": segment.get("voyage_no") or segment.get("flight_number"),
                "carrier": segment.get("carrier") or segment.get("airline"),
            })
    first_leg = legs[0] if legs else {}
    last_leg = legs[-1] if legs else {}
    price = offer.get("price") or {}
    checkout_ref = offer.get("checkout_ref") or {}
    return {
        "from": _plain_location(first_leg.get("from")),
        "to": _plain_location(last_leg.get("to")),
        "departure_at": offer.get("departure_at"),
        "arrival_at": offer.get("arrival_at"),
        "price_amount": price.get("amount"),
        "price_currency": price.get("currency") or "RUB",
        "service_class": checkout_ref.get("service_class") or "Y",
        "segments": segments,
    }


def _format_offer(offer: dict) -> str:
    price = (offer.get("price") or {}).get("amount", 0)
    legs = offer.get("legs") or []
    outbound = legs[0] if legs else {}
    segments = outbound.get("segments") or []
    flight_numbers = ", ".join(
        str(segment.get("voyage_no")) for segment in segments if segment.get("voyage_no")
    ) or "—"
    carriers = ", ".join(str(x) for x in offer.get("carriers") or []) or "—"
    transfer_count = max(0, len(segments) - 1)
    transfers = "прямой" if transfer_count == 0 else f"пересадок: {transfer_count}"

    variants = offer.get("variants") or []
    cheapest = variants[0] if variants else {}
    conditions = cheapest.get("conditions") or {}
    fare_family = conditions.get("fare_family") or "тариф не указан"
    refundable = conditions.get("refundable")
    changeable = conditions.get("changeable")
    refund_text = "возвратный" if refundable is True else ("невозвратный" if refundable is False else "возврат не указан")
    change_text = "изменяемый" if changeable is True else ("без изменений" if changeable is False else "изменения не указаны")

    review = offer.get("review_summary") or {}
    review_line = f"\n⭐ {html.escape(str(review.get('label')))}" if review.get("label") else ""

    return (
        f"<b>{format_price(price)}</b> · {html.escape(carriers)}\n"
        f"✈️ Рейс: {html.escape(flight_numbers)} · {transfers}\n"
        f"🛫 {html.escape(str(outbound.get('from', '—')))}\n"
        f"🕒 {_format_datetime(offer.get('departure_at'))}\n"
        f"🛬 {html.escape(str(outbound.get('to', '—')))}\n"
        f"🕒 {_format_datetime(offer.get('arrival_at'))}\n"
        f"⏱ {_format_duration(offer.get('duration_min'))}\n"
        f"🎫 {html.escape(str(fare_family))}: {html.escape(_format_baggage(conditions))}\n"
        f"↩️ {refund_text}; {change_text}"
        f"{review_line}"
    )
