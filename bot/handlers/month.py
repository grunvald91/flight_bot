"""Отдельный поиск: самые дешёвые билеты по направлению на ближайший месяц.

Выдача ранжирована от дешёвых к дорогим, листается страницами, а переключатель
«Без пересадок» живёт прямо под выдачей. Состояние выдачи (направление, переключатель, страница)
хранится в callback_data, поэтому кнопки работают и после перезапуска бота. Последний выбор
переключателя запоминается за пользователем (bot/services/search_prefs.py) и общий с «Дешёвые за границу».
"""
import html
import logging
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards.common_kb import persistent_menu_keyboard
from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.search_prefs import load_direct_only, save_direct_only
from bot.states.search import MonthSearchStates
from bot.utils.formatters import format_date_short, format_price, format_time, format_transfers

logger = logging.getLogger(__name__)

router = Router()

WINDOW_DAYS = 30
PAGE_SIZE = 10  # размер страницы выдачи; все билеты доступны листанием

# Данные кнопок присылает клиент Telegram, поэтому разбираем их строго:
# ровно три латинские буквы и номер страницы из ASCII-цифр (без «²», переводов строк и т. п.)
_IATA_RE = re.compile(r"[A-Z]{3}")
_PAGE_RE = re.compile(r"[0-9]{1,4}")


# ── Вход и выбор направления ──

@router.message(Command("month"))
async def cmd_month(message: Message, state: FSMContext):
    await _start(message, state)


@router.callback_query(F.data == "menu:month")
async def cb_month_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _start(callback.message, state)


async def _start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(MonthSearchStates.choosing_origin)
    await message.answer(
        "<b>📅 Дешёвые билеты на месяц</b>\n\n"
        "Покажу все билеты по выбранному направлению на ближайший месяц, "
        "начиная с самых дешёвых.\n\n"
        "Введите город вылета (название или IATA-код), например: <code>Сочи</code>",
        reply_markup=persistent_menu_keyboard(),
    )


@router.message(MonthSearchStates.choosing_origin)
async def month_origin(message: Message, state: FSMContext):
    origin = city_resolver.resolve((message.text or "").strip())
    if not origin:
        await message.answer("Город не найден. Введите название или IATA-код ещё раз:")
        return
    await state.update_data(month_origin=origin)
    await state.set_state(MonthSearchStates.choosing_destination)
    await message.answer(
        f"Вылет из: <b>{html.escape(city_resolver.get_city_name(origin))}</b>\n\n"
        "Теперь город прибытия:"
    )


@router.message(MonthSearchStates.choosing_destination)
async def month_destination(message: Message, state: FSMContext):
    destination = city_resolver.resolve((message.text or "").strip())
    if not destination:
        await message.answer("Город не найден. Введите название или IATA-код ещё раз:")
        return
    data = await state.get_data()
    origin = data.get("month_origin")
    if not origin:
        await state.clear()
        await message.answer("Поиск устарел. Начните заново: /month")
        return
    await state.clear()

    progress = await message.answer("⏳ Ищу самые дешёвые билеты на ближайший месяц...")
    # Переключатель «Без пересадок» стартует с последнего выбора пользователя
    direct_only = await load_direct_only(message.from_user.id)
    text, keyboard = await _render(origin, destination, direct_only=direct_only, page=0)
    try:
        await progress.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


# ── Выдача: переключатель и страницы ──

def _callback_data(origin: str, destination: str, direct_only: bool, page: int) -> str:
    return f"mo:{origin}:{destination}:{int(direct_only)}:{page}"


def _parse_callback(data: str) -> tuple[str, str, bool, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 5 or parts[0] != "mo":
        return None
    _, origin, destination, direct, page = parts
    if not _IATA_RE.fullmatch(origin) or not _IATA_RE.fullmatch(destination):
        return None
    if direct not in ("0", "1") or not _PAGE_RE.fullmatch(page):
        return None
    return origin, destination, direct == "1", int(page)


def _parse_toggle(data: str) -> tuple[str, str, bool] | None:
    """mt:ORIGIN:DEST:{0|1} — переключатель «Без пересадок»; значение — новое состояние."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != "mt":
        return None
    _, origin, destination, direct = parts
    if not _IATA_RE.fullmatch(origin) or not _IATA_RE.fullmatch(destination) or direct not in ("0", "1"):
        return None
    return origin, destination, direct == "1"


@router.callback_query(F.data == "mo:noop")
async def cb_month_noop(callback: CallbackQuery):
    await callback.answer()


async def _redraw(
    callback: CallbackQuery, origin: str, destination: str, direct_only: bool, page: int,
    notice: str | None = None,
):
    text, keyboard = await _render(origin, destination, direct_only=direct_only, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
    await callback.answer(notice)


@router.callback_query(F.data.startswith("mo:"))
async def cb_month_view(callback: CallbackQuery):
    parsed = _parse_callback(callback.data)
    if not parsed:
        await callback.answer("Кнопка устарела. Начните заново: /month", show_alert=True)
        return
    origin, destination, direct_only, page = parsed
    await _redraw(callback, origin, destination, direct_only, page)


@router.callback_query(F.data.startswith("mt:"))
async def cb_month_toggle(callback: CallbackQuery):
    parsed = _parse_toggle(callback.data)
    if not parsed:
        await callback.answer("Кнопка устарела. Начните заново: /month", show_alert=True)
        return
    origin, destination, direct_only = parsed

    saved = await save_direct_only(callback.from_user, direct_only)
    # Если запомнить не вышло, переключатель всё равно работает в этой выдаче
    await _redraw(
        callback, origin, destination, direct_only, 0,
        notice=None if saved else "Фильтр применён, но запомнить выбор не удалось",
    )


async def _load_tickets(origin: str, destination: str, direct_only: bool) -> list[dict]:
    tickets = await aviasales_api.search_cheapest_in_window(origin, destination, days=WINDOW_DAYS)
    if direct_only:
        tickets = [t for t in tickets if t.get("transfers", 0) == 0]
    return tickets


def _line(rank: int, ticket: dict, origin: str, destination: str) -> str:
    departure = ticket.get("departure_at", "")
    when = format_date_short(departure)
    time = format_time(departure)
    if time:
        when = f"{when}, {time}"

    airline_code = ticket.get("airline", "")
    airline = html.escape(city_resolver.get_airline_name(airline_code)) if airline_code else "—"
    transfers = ticket.get("transfers", 0)
    transfers_text = "прямой" if transfers == 0 else format_transfers(transfers)

    link = html.escape(aviasales_api.build_ticket_link(ticket, origin, destination, None), quote=True)
    return (
        f"{rank}. <b>{format_price(ticket['price'])}</b> · {when} · {airline} · {transfers_text}"
        f" — <a href=\"{link}\">купить</a>"
    )


def _keyboard(origin: str, destination: str, direct_only: bool, page: int, pages: int) -> InlineKeyboardMarkup:
    toggle_text = "✈️ Без пересадок: ВКЛ ✅" if direct_only else "✈️ Без пересадок: ВЫКЛ"
    rows = [[
        InlineKeyboardButton(
            text=toggle_text,
            callback_data=f"mt:{origin}:{destination}:{int(not direct_only)}",
        )
    ]]

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀️", callback_data=_callback_data(origin, destination, direct_only, page - 1),
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="mo:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(
                text="▶️", callback_data=_callback_data(origin, destination, direct_only, page + 1),
            ))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🔎 Другое направление", callback_data="menu:month")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(origin: str, destination: str, direct_only: bool, page: int) -> tuple[str, InlineKeyboardMarkup]:
    tickets = await _load_tickets(origin, destination, direct_only)

    pages = max(1, -(-len(tickets) // PAGE_SIZE))
    page = min(page, pages - 1)

    today = datetime.now().date()
    period = f"{format_date_short(today.isoformat())} — {format_date_short((today + timedelta(days=WINDOW_DAYS)).isoformat())}"
    header = (
        f"<b>📅 Дешёвые на месяц: {html.escape(city_resolver.get_city_name(origin))}"
        f" → {html.escape(city_resolver.get_city_name(destination))}</b>\n"
        f"{period} · от дешёвых к дорогим"
    )
    if direct_only:
        header += "\n✈️ Только без пересадок"

    if not tickets:
        body = (
            "Прямых билетов на ближайший месяц не нашёл. Выключите «Без пересадок», чтобы увидеть все варианты."
            if direct_only
            else "На ближайший месяц билетов по этому направлению не нашёл. Попробуйте другое направление."
        )
        return f"{header}\n\n{body}", _keyboard(origin, destination, direct_only, 0, 1)

    start = page * PAGE_SIZE
    lines = [
        _line(start + i + 1, ticket, origin, destination)
        for i, ticket in enumerate(tickets[start:start + PAGE_SIZE])
    ]
    text = f"{header}\nВсего вариантов: {len(tickets)}\n\n" + "\n".join(lines)
    return text, _keyboard(origin, destination, direct_only, page, pages)
