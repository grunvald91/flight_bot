import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.tutu_mcp import tutu_checkout_cache, tutu_mcp
from bot.states.search import TutuSearchStates
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
        "Введите дату вылета в формате <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.\n"
        "Например: <code>01.10</code>"
    )


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


async def _run_tutu_search(message: Message, origin: str, destination: str, departure_date: str):
    await message.answer(
        f"Ищу на Туту: <b>{html.escape(origin)} → {html.escape(destination)}</b> "
        f"на {_format_date(departure_date)}…"
    )
    try:
        payload = await tutu_mcp.search_avia(origin, destination, departure_date)
    except Exception as exc:
        logger.warning("Tutu search failed: %s", exc)
        await message.answer(
            "Туту сейчас не ответил или не смог распознать маршрут. "
            "Проверьте названия городов/аэропортов и попробуйте ещё раз через /tutu."
        )
        return

    offers = payload.get("offers") or []
    meta = payload.get("meta") or {}
    if not offers:
        note = meta.get("airport_note") or meta.get("upstream_note")
        suffix = f"\n\n{html.escape(str(note))}" if note else ""
        await message.answer(
            "На Туту не найдено билетов в продаже на эту дату." + suffix
        )
        return

    resolved_from = (meta.get("from") or {}).get("name", origin)
    resolved_to = (meta.get("to") or {}).get("name", destination)
    total = meta.get("total_matched", len(offers))
    await message.answer(
        f"<b>Туту: {html.escape(str(resolved_from))} → {html.escape(str(resolved_to))}</b>\n"
        f"Найдено вариантов: {total}. Показываю самые дешёвые.\n"
        "Цена в карточке — итог за 1 взрослого."
    )

    for offer in offers[:5]:
        checkout_ref = offer.get("checkout_ref") or {}
        cache_key = tutu_checkout_cache.put(checkout_ref) if checkout_ref else None
        search_url = offer.get("search_results_url")
        buttons = []
        if cache_key:
            buttons.append([
                InlineKeyboardButton(
                    text="Купить этот билет на Туту",
                    callback_data=f"tutubuy:{cache_key}",
                )
            ])
        if search_url:
            buttons.append([
                InlineKeyboardButton(text="Все варианты на Туту", url=search_url)
            ])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer(
            _format_offer(offer),
            disable_web_page_preview=True,
            reply_markup=markup,
        )

    if meta.get("has_more") and offers:
        url = offers[0].get("search_results_url")
        if url:
            await message.answer(
                "Есть и другие варианты:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Открыть весь список на Туту", url=url)
                ]]),
            )


@router.callback_query(F.data.startswith("tutubuy:"))
async def cb_tutu_buy(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    checkout_ref = tutu_checkout_cache.get(key)
    if not checkout_ref:
        await callback.answer("Ссылка устарела. Повторите поиск через /tutu.", show_alert=True)
        return

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
