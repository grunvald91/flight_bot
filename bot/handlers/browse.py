"""Fast indicative Aviasales browsing; verification is requested per destination."""
import html
import logging
import secrets
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.tutu import _run_tutu_search
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.keyboards.common_kb import persistent_menu_keyboard
from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.discovery_archive import can_open_result, discovery_archive
from bot.services.fast_browse import PAGE_SIZE, load_indicative_destinations, page_options
from bot.states.search import FastBrowseStates
from bot.utils.parsers import _parse_date

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("browse"))
async def cmd_browse(message: Message, state: FSMContext):
    await _start_browse(message, state)


@router.callback_query(F.data == "menu:browse")
async def browse_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _start_browse(callback.message, state)


async def _start_browse(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FastBrowseStates.choosing_origin)
    await message.answer(
        "<b>⚡ Быстрый обзор направлений</b>\n\n"
        "Получаю ориентировочные цены Aviasales на зарубежные направления. "
        "Туту проверю только после выбора направления.\n\n"
        "Укажите город вылета, например: <code>Сочи</code> или <code>Москва</code>.",
        reply_markup=persistent_menu_keyboard(),
    )


@router.message(FastBrowseStates.choosing_origin)
async def browse_origin(message: Message, state: FSMContext):
    origin = city_resolver.resolve((message.text or "").strip())
    if not origin:
        await message.answer("Город не найден. Введите название или IATA-код ещё раз:")
        return
    await state.update_data(browse_origin=origin)
    await state.set_state(FastBrowseStates.choosing_date)
    await message.answer(
        f"Вылет из: <b>{html.escape(city_resolver.get_city_name(origin))}</b>. "
        "Выберите дату или введите её как <code>ДД.ММ</code>:",
        reply_markup=calendar_keyboard("browse"),
    )


@router.callback_query(F.data.startswith("caldate:browse:"))
async def browse_calendar_date(callback: CallbackQuery, state: FSMContext):
    departure_date = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    origin = data.get("browse_origin")
    if not origin or not _valid_date(departure_date):
        await callback.answer("Поиск или дата недействительны. Начните заново.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(f"Выбрана дата: <b>{_date_label(departure_date)}</b>")
    await callback.answer()
    await _run_browse(callback.message, origin, departure_date, owner_id=callback.from_user.id)


@router.message(FastBrowseStates.choosing_date)
async def browse_text_date(message: Message, state: FSMContext):
    departure_date = _parse_date((message.text or "").strip())
    if not departure_date or not _valid_date(departure_date):
        await message.answer("Выберите будущую дату в календаре или введите <code>ДД.ММ</code>.")
        return
    data = await state.get_data()
    origin = data.get("browse_origin")
    if not origin:
        await state.clear()
        await message.answer("Поиск устарел. Начните заново через /browse.")
        return
    await state.clear()
    await _run_browse(message, origin, departure_date, owner_id=message.from_user.id)


def _valid_date(value: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() >= date.today()
    except (TypeError, ValueError):
        return False


def _date_label(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")


def _money(value: int | float) -> str:
    return f"{value:,.0f}".replace(",", " ") if float(value).is_integer() else f"{value:,.2f}".replace(",", " ")


def _stops_label(count: int | None) -> str:
    if count is None:
        return "пересадки не указаны"
    return "без пересадок" if count == 0 else f"пересадок: {count}"


async def _run_browse(message: Message, origin: str, departure_date: str, *, owner_id: int):
    progress = await message.answer(
        "⏳ Собираю все доступные страницы Aviasales. Список появится после завершения сбора."
    )
    try:
        result = await load_indicative_destinations(origin, departure_date)
    except Exception:
        logger.exception("Indicative browse source failed")
        await progress.edit_text("Не удалось собрать направления полностью. Попробуйте позже.")
        return
    if not result.options:
        await progress.edit_text("Нет зарубежных направлений с ориентировочной ценой на эту дату.")
        return
    key = secrets.token_urlsafe(8)
    payload = {
        "options": result.options,
        "meta": {"owner_id": owner_id, "chat_id": message.chat.id,
                 "origin": origin, "departure_date": departure_date,
                 "source_rows": result.source_rows, "unresolved": result.unresolved},
    }
    try:
        discovery_archive.put_browse(key, payload)
    except Exception:
        logger.exception("Could not save indicative browse results")
        await progress.edit_text("Не удалось сохранить результаты. Попробуйте ещё раз.")
        return
    await progress.edit_text(
        _format_page(payload, 0), disable_web_page_preview=True,
        reply_markup=_page_keyboard(key, payload, 0),
    )


def _format_page(payload: dict, page: int) -> str:
    options = payload["options"]
    meta = payload["meta"]
    total_pages = (len(options) + PAGE_SIZE - 1) // PAGE_SIZE
    lines = [
        f"<b>⚡ Зарубежные направления · {_date_label(meta['departure_date'])}</b>",
        f"Вариантов: <b>{len(options)}</b> · страница <b>{page + 1}/{total_pages}</b>",
        "Сортировка: цена ↑, при равной цене — меньше пересадок.",
        "Выберите номер направления кнопкой ниже.",
        "",
    ]
    for index, option in enumerate(page_options(options, page), start=page * PAGE_SIZE + 1):
        lines.append(
            f"<b>{index}.</b> {html.escape(str(option['country']))} — "
            f"{html.escape(str(option['city']))} · ≈{_money(option['price'])} ₽ · "
            f"{_stops_label(option.get('transfers'))}"
        )
    lines.append("\n⚠️ Цены Aviasales ориентировочные: наличие и итоговую цену нужно проверить у провайдера.")
    if meta.get("unresolved"):
        lines.append(f"Не определена страна для {meta['unresolved']} строк источника; они не включены.")
    return "\n".join(lines)


def _page_keyboard(key: str, payload: dict, page: int) -> InlineKeyboardMarkup:
    options = payload["options"]
    choices = [InlineKeyboardButton(text=str(index + 1), callback_data=f"brsel:{key}:{index}")
               for index, _ in enumerate(page_options(options, page), start=page * PAGE_SIZE)]
    rows = [choices[index:index + 5] for index in range(0, len(choices), 5)]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"brpage:{key}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{(len(options) + PAGE_SIZE - 1) // PAGE_SIZE}", callback_data="calnoop"))
    if (page + 1) * PAGE_SIZE < len(options):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"brpage:{key}:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _payload(callback: CallbackQuery, key: str) -> dict | None:
    try:
        payload = discovery_archive.get(key)
    except Exception:
        logger.exception("Could not load browse results")
        payload = None
    if not payload or (payload.get("meta") or {}).get("kind") != "browse":
        await callback.answer("Сохранённый список недоступен. Начните новый поиск.", show_alert=True)
        return None
    if not can_open_result(payload, callback):
        await callback.answer("Этот поиск доступен только его автору.", show_alert=True)
        return None
    return payload


@router.callback_query(F.data.startswith("brpage:"))
async def browse_page(callback: CallbackQuery):
    try:
        _, key, raw_page = (callback.data or "").split(":", 2)
        page = int(raw_page)
    except (TypeError, ValueError):
        await callback.answer("Некорректная страница", show_alert=True)
        return
    payload = await _payload(callback, key)
    if payload is None:
        return
    if page < 0 or not page_options(payload["options"], page):
        await callback.answer("Такой страницы нет.", show_alert=True)
        return
    await callback.message.edit_text(
        _format_page(payload, page), disable_web_page_preview=True,
        reply_markup=_page_keyboard(key, payload, page),
    )
    await callback.answer()


def _selected_keyboard(key: str, index: int, option: dict, departure_date: str) -> InlineKeyboardMarkup:
    url = aviasales_api.build_search_link(option["origin"], option["destination"], departure_date)
    rows = [
        [InlineKeyboardButton(text="🟣 Туту", callback_data=f"brtutu:{key}:{index}"),
         InlineKeyboardButton(text="✈️ Aviasales ↗", url=url)],
    ]
    country_code = str(option.get("country_code") or "").upper()
    if len(country_code) == 2:
        rows.append([
            InlineKeyboardButton(
                text="ℹ️ Въезд для граждан РФ",
                callback_data=f"entry:{country_code}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ К списку", callback_data=f"brpage:{key}:{index // PAGE_SIZE}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("brsel:"))
async def browse_select(callback: CallbackQuery):
    selected = await _selected(callback)
    if selected is None:
        return
    key, index, payload, option = selected
    departure_at = option.get("departure_at") or (payload["meta"]["departure_date"])
    lines = [
        f"<b>⚡ Направление {index + 1} из {len(payload['options'])}</b>",
        f"{html.escape(str(option['country']))} — {html.escape(str(option['city']))} "
        f"({html.escape(str(option['destination']))})",
        f"Ориентировочная цена Aviasales: <b>≈{_money(option['price'])} ₽</b>",
        f"Вылет: {html.escape(str(departure_at))}",
        f"Пересадки: {_stops_label(option.get('transfers'))}",
    ]
    if option.get("airline"):
        lines.append(f"Авиакомпания: {html.escape(str(option['airline']))}")
    if option.get("flight_number"):
        lines.append(f"Рейс: {html.escape(str(option['flight_number']))}")
    if option.get("duration"):
        lines.append(f"В пути: {option['duration']} мин")
    lines.append("\n⚠️ Цена и рейс из Aviasales не гарантируют наличия у Туту. "
                 "Проверьте выбранное направление отдельно у нужного провайдера.")
    await callback.message.edit_text(
        "\n".join(lines), disable_web_page_preview=True,
        reply_markup=_selected_keyboard(key, index, option, payload["meta"]["departure_date"]),
    )
    await callback.answer()


async def _selected(callback: CallbackQuery):
    try:
        _, key, raw_index = (callback.data or "").split(":", 2)
        index = int(raw_index)
    except (TypeError, ValueError):
        await callback.answer("Некорректное направление", show_alert=True)
        return None
    payload = await _payload(callback, key)
    if payload is None:
        return None
    options = payload["options"]
    if index < 0 or index >= len(options):
        await callback.answer("Такого направления нет", show_alert=True)
        return None
    return key, index, payload, options[index]


@router.callback_query(F.data.startswith("brtutu:"))
async def browse_tutu(callback: CallbackQuery):
    selected = await _selected(callback)
    if selected is None:
        return
    _, _, payload, option = selected
    await callback.answer("Проверяю направление через Туту…")
    origin_name = city_resolver.get_city_name(option["origin"])
    await _run_tutu_search(
        callback.message, origin_name, option["city"], payload["meta"]["departure_date"],
        owner_id=callback.from_user.id,
    )
