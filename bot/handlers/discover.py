import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.tutu import _booking_summary, _format_offer, _send_checkout_link
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.services.city_resolver import city_resolver
from bot.services.discovery_archive import can_open_result, discovery_archive
from bot.services.international_discovery import discover_international_offers
from bot.services.tutu_mcp import tutu_search_cache
from bot.states.search import InternationalDiscoveryStates
from bot.utils.parsers import _parse_date

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("discover"))
async def cmd_discover(message: Message, state: FSMContext):
    await _start_discovery(message, state)


@router.callback_query(F.data == "menu:discover")
async def cb_discover_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _start_discovery(callback.message, state)


async def _start_discovery(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(InternationalDiscoveryStates.choosing_origin)
    await message.answer(
        "<b>🌍 Куда дешевле за границу</b>\n\n"
        "Укажите город вылета. Пункт назначения выбирать не нужно.\n"
        "Например: <code>Москва</code>, <code>Сочи</code> или <code>MOW</code>."
    )


@router.message(InternationalDiscoveryStates.choosing_origin)
async def discovery_origin(message: Message, state: FSMContext):
    origin = city_resolver.resolve((message.text or "").strip())
    if not origin:
        await message.answer("Город не найден. Введите название или IATA-код ещё раз:")
        return

    origin_name = city_resolver.get_city_name(origin)
    await state.update_data(discovery_origin=origin, discovery_origin_name=origin_name)
    await state.set_state(InternationalDiscoveryStates.choosing_date)
    await message.answer(
        f"Вылет из: <b>{html.escape(origin_name)} ({origin})</b>\n\n"
        "Выберите дату вылета в календаре:",
        reply_markup=calendar_keyboard("discover"),
    )


@router.callback_query(F.data.startswith("caldate:discover:"))
async def discovery_calendar_date(callback: CallbackQuery, state: FSMContext):
    departure_date = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    origin = data.get("discovery_origin")
    if not origin:
        await callback.answer("Поиск устарел. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"Выбрана дата: <b>{_format_date(departure_date)}</b>"
    )
    await callback.answer()
    await _run_discovery(callback.message, origin, departure_date,
                         owner_id=callback.from_user.id)


@router.message(InternationalDiscoveryStates.choosing_date)
async def discovery_text_date(message: Message, state: FSMContext):
    departure_date = _parse_date((message.text or "").strip())
    if not departure_date:
        await message.answer(
            "Выберите дату в календаре или введите её в формате <code>ДД.ММ</code>."
        )
        return
    data = await state.get_data()
    await state.clear()
    await _run_discovery(message, data["discovery_origin"], departure_date)


async def _run_discovery(message: Message, origin: str, departure_date: str,
                         owner_id: int | None = None):
    origin_name = city_resolver.get_city_name(origin)
    progress = await message.answer(
        f"<b>Подробный поиск билетов за границу</b>\n\n"
        f"Вылет из: <b>{html.escape(origin_name)}</b>\n"
        f"Дата: <b>{_format_date(departure_date)}</b>\n\n"
        "Проверяю найденные зарубежные направления и ищу конкретные билеты через Туту. "
        "Такой поиск занимает больше времени, зато результат будет точнее."
    )

    last_progress_text = ""

    async def update_progress(checked: int, total: int, found: int) -> None:
        nonlocal last_progress_text
        text = (
            f"<b>Подробный поиск билетов за границу</b>\n\n"
            f"Вылет из: <b>{html.escape(origin_name)}</b>\n"
            f"Дата: <b>{_format_date(departure_date)}</b>\n\n"
            f"Проверено через Туту: <b>{checked} из {total}</b>\n"
            f"Подтверждённых направлений: <b>{found}</b>\n\n"
            "⏳ Проверка продолжается; результат появится после завершения всех маршрутов."
        )
        if text != last_progress_text:
            await progress.edit_text(text)
            last_progress_text = text

    try:
        offers = await discover_international_offers(
            origin,
            departure_date,
            progress_callback=update_progress,
        )
    except Exception as exc:
        logger.exception("International discovery failed: %s", exc)
        await progress.edit_text(
            "Не удалось получить международные направления. Попробуйте ещё раз немного позже."
        )
        return

    if not offers:
        if offers.unverified:
            await progress.edit_text(
                f"Проверено маршрутов: <b>{offers.checked}</b>; "
                f"непроверенных: <b>{offers.unverified}</b>. "
                "Подтверждённых билетов нет. Попробуйте повторить поиск позже."
            )
            return
        await progress.edit_text(
            f"На {_format_date(departure_date)} не удалось найти подтверждённые "
            "международные варианты среди направлений, полученных от источника. Попробуйте другую дату."
        )
        return

    if offers.unverified:
        coverage_note = (
            f"⚠️ Проверено маршрутов: <b>{offers.checked}</b>; "
            f"непроверенных: <b>{offers.unverified}</b>. "
            "Среди непроверенных может быть билет дешевле. Ниже все подтверждённые варианты "
            "по возрастанию цены; первый — самый дешёвый только среди проверенных."
        )
    else:
        coverage_note = (
            f"Проверено маршрутов: <b>{offers.checked}</b>.\n"
            "Ниже все подтверждённые варианты по возрастанию цены "
            "для 1 взрослого в экономклассе."
        )
    await progress.edit_text(
        f"<b>🌍 Из {html.escape(origin_name)} за границу — {_format_date(departure_date)}</b>\n"
        + coverage_note
    )
    scope = {"owner_id": owner_id if owner_id is not None else
             getattr(getattr(message, "from_user", None), "id", None),
             "chat_id": getattr(getattr(message, "chat", None), "id", None)}
    search_key = tutu_search_cache.put({
        "offers": offers,
        "meta": {"origin": origin, "departure_date": departure_date,
                 "unverified": offers.unverified, **scope},
    })
    try:
        discovery_archive.put(search_key, {
            "offers": offers,
            "booking_summaries": [_booking_summary(offer) for offer in offers],
            "meta": {"origin": origin, "departure_date": departure_date,
                     "unverified": offers.unverified, **scope},
        })
    except Exception:
        logger.exception("Could not save search results")
        await progress.edit_text(
            f"<b>🌍 Из {html.escape(origin_name)} за границу — {_format_date(departure_date)}</b>\n"
            + coverage_note + "\n⚠️ Не удалось сохранить результаты для просмотра после перезапуска."
        )
    await message.answer(
        _format_discovery_card(offers[0], 0, len(offers)),
        disable_web_page_preview=True,
        reply_markup=_discovery_keyboard(search_key, offers, 0),
    )


@router.callback_query(F.data.startswith("discpage:"))
async def discovery_page(callback: CallbackQuery):
    try:
        _, search_key, raw_index = (callback.data or "").split(":", 2)
        index = int(raw_index)
    except (TypeError, ValueError):
        await callback.answer("Некорректная страница", show_alert=True)
        return

    payload = tutu_search_cache.get(search_key)
    archived = payload is None
    if archived:
        try:
            payload = discovery_archive.get(search_key)
        except Exception:
            logger.exception("Could not load display-only search archive")
    if not payload:
        await callback.answer("Архив результатов недоступен. Повторите поиск.", show_alert=True)
        return
    offers = payload.get("offers") or []
    if not payload or not payload.get("offers"):
        await callback.answer("Результаты поиска не найдены.", show_alert=True)
        return
    if not can_open_result(payload, callback):
        await callback.answer("Этот поиск доступен только его автору.", show_alert=True)
        return
    index = max(0, min(index, len(offers) - 1))
    card = _format_discovery_card(offers[index], index, len(offers))
    if archived:
        card += ("\n\n⚠️ Это сохранённые результаты. Цена и доступность "
                 "определяются Туту при открытии ссылки; перед оплатой проверьте их.")
        unverified = (payload.get("meta") or {}).get("unverified") or 0
        if unverified:
            card += (f" При этом {unverified} непроверенных маршрутов: "
                     "среди них может быть более дешёвый билет.")
    await callback.message.edit_text(
        card,
        disable_web_page_preview=True,
        reply_markup=_discovery_keyboard(search_key, offers, index),
    )
    await callback.answer()


async def _selected_discovery_offer(callback: CallbackQuery) -> dict | None:
    try:
        _, key, raw_index = (callback.data or "").split(":", 2)
        index = int(raw_index)
    except (ValueError, TypeError):
        await callback.answer("Некорректный билет", show_alert=True)
        return None
    try:
        payload = tutu_search_cache.get(key) or discovery_archive.get(key)
    except Exception:
        logger.exception("Could not open saved discovery offer")
        payload = None
    offers = (payload or {}).get("offers") or []
    if payload and not can_open_result(payload, callback):
        await callback.answer("Этот поиск доступен только его автору.", show_alert=True)
        return None
    if index < 0 or index >= len(offers):
        await callback.answer("Билет недоступен в сохранённых результатах.", show_alert=True)
        return None
    offer = offers[index]
    if not offer.get("checkout_ref"):
        await callback.answer("Для этого билета нет ссылки оформления.", show_alert=True)
        return None
    return offer


@router.callback_query(F.data.startswith("discbuy:"))
async def discovery_buy_link(callback: CallbackQuery):
    offer = await _selected_discovery_offer(callback)
    if offer is not None:
        await _send_checkout_link(callback, offer["checkout_ref"])


@router.callback_query(F.data.startswith("discbook:"))
async def discovery_book_with_profile(callback: CallbackQuery):
    offer = await _selected_discovery_offer(callback)
    if offer is not None:
        from bot.handlers.profile import _book_with_profile
        await _book_with_profile(callback, {
            "checkout_ref": offer["checkout_ref"],
            "offer_summary": offer.get("booking_summary") or _booking_summary(offer),
        })


def _format_date(value: str) -> str:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def _format_discovery_card(offer: dict, index: int, total: int) -> str:
    discovery = offer.get("_discovery") or {}
    country = html.escape(str(discovery.get("country") or "Страна не определена"))
    city = html.escape(str(discovery.get("city") or "Город не определён"))
    return (
        f"<b>Вариант {index + 1} из {total}</b> · по цене среди проверенных\n\n"
        f"🌍 <b>{country} — {city}</b>\n"
        f"🎟 <b>Только туда</b>\n\n"
        f"{_format_offer(offer)}\n\n"
        "⚠️ Цена подтверждена на момент поиска, но может измениться до оформления. "
        "Перед оплатой проверьте итоговую стоимость и условия тарифа."
    )


def _discovery_keyboard(
    search_key: str,
    offers: list[dict],
    index: int,
) -> InlineKeyboardMarkup:
    offer = offers[index]
    rows: list[list[InlineKeyboardButton]] = []
    checkout_ref = offer.get("checkout_ref") or {}
    if checkout_ref:
        rows.append([
            InlineKeyboardButton(
                text="Купить этот билет на Туту",
                callback_data=f"discbuy:{search_key}:{index}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="👤 Оформить с моим пассажиром",
                callback_data=f"discbook:{search_key}:{index}",
            )
        ])

    previous_cb = f"discpage:{search_key}:{index - 1}" if index > 0 else "calnoop"
    next_cb = f"discpage:{search_key}:{index + 1}" if index + 1 < len(offers) else "calnoop"
    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=previous_cb),
        InlineKeyboardButton(text=f"{index + 1}/{len(offers)}", callback_data="calnoop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ])

    search_url = offer.get("search_results_url") or checkout_ref.get("search_results_url")
    if search_url:
        rows.append([InlineKeyboardButton(text="Все рейсы в этот город", url=search_url)])

    country_code = str((offer.get("_discovery") or {}).get("country_code") or "").upper()
    if len(country_code) == 2:
        rows.append([
            InlineKeyboardButton(
                text="ℹ️ Виза и документы",
                callback_data=f"entry:{country_code}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="Новый поиск", callback_data="menu:discover")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
