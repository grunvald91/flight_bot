import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.tutu import _format_offer
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.services.city_resolver import city_resolver
from bot.services.international_discovery import discover_international_offers
from bot.services.tutu_mcp import tutu_checkout_cache, tutu_search_cache
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
    await _run_discovery(callback.message, origin, departure_date)


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


async def _run_discovery(message: Message, origin: str, departure_date: str):
    origin_name = city_resolver.get_city_name(origin)
    progress = await message.answer(
        f"Ищу самые дешёвые международные направления из "
        f"<b>{html.escape(origin_name)}</b> на {_format_date(departure_date)}…\n"
        "Сначала нахожу направления, затем проверяю полную цену и условия на Туту."
    )
    try:
        offers = await discover_international_offers(origin, departure_date)
    except Exception as exc:
        logger.exception("International discovery failed: %s", exc)
        await progress.edit_text(
            "Не удалось получить международные направления. Попробуйте ещё раз немного позже."
        )
        return

    if not offers:
        await progress.edit_text(
            f"На {_format_date(departure_date)} не удалось найти подтверждённые "
            "международные варианты из этого города. Попробуйте другую дату."
        )
        return

    await progress.edit_text(
        f"<b>🌍 Из {html.escape(origin_name)} за границу — {_format_date(departure_date)}</b>\n"
        f"Найдено подтверждённых направлений: <b>{len(offers)}</b>.\n"
        "Список отсортирован по полной стоимости перелёта за 1 взрослого."
    )
    search_key = tutu_search_cache.put({
        "offers": offers,
        "meta": {"origin": origin, "departure_date": departure_date},
    })
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
    if not payload:
        await callback.answer("Результаты устарели. Повторите поиск.", show_alert=True)
        return
    offers = payload.get("offers") or []
    if not offers:
        await callback.answer("Варианты не найдены", show_alert=True)
        return
    index = max(0, min(index, len(offers) - 1))
    await callback.message.edit_text(
        _format_discovery_card(offers[index], index, len(offers)),
        disable_web_page_preview=True,
        reply_markup=_discovery_keyboard(search_key, offers, index),
    )
    await callback.answer()


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
        f"<b>Направление {index + 1} из {total}</b> · от дешёвых к дорогим\n\n"
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
        checkout_key = tutu_checkout_cache.put(checkout_ref)
        rows.append([
            InlineKeyboardButton(
                text="Купить этот билет на Туту",
                callback_data=f"tutubuy:{checkout_key}",
            )
        ])

    previous_cb = f"discpage:{search_key}:{index - 1}" if index > 0 else "calnoop"
    next_cb = f"discpage:{search_key}:{index + 1}" if index + 1 < len(offers) else "calnoop"
    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=previous_cb),
        InlineKeyboardButton(text=f"{index + 1}/{len(offers)}", callback_data="calnoop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ])

    search_url = offer.get("search_results_url")
    if search_url:
        rows.append([InlineKeyboardButton(text="Все рейсы в этот город", url=search_url)])
    rows.append([
        InlineKeyboardButton(text="Новый поиск", callback_data="menu:discover")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
