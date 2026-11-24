"""Отдельный поиск: самые дешёвые билеты по направлению на ближайший месяц.

Выдача ранжирована от дешёвых к дорогим, листается страницами, а переключатель
«Без пересадок» живёт прямо под выдачей. Состояние выдачи (направление, переключатель, страница)
хранится в callback_data, поэтому кнопки работают и после перезапуска бота. Последний выбор
переключателя запоминается за пользователем (bot/services/search_prefs.py) и общий с «Дешёвые за границу».
"""
import html
import logging
import re
import secrets
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from zoneinfo import ZoneInfo
from bot.config import TIMEZONE
from bot.services.flexible_trips import daily_foreign
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards.common_kb import persistent_menu_keyboard
from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.countries import country_label, resolve_country
from bot.services.country_search import search_cheapest_to_country
from bot.services.discovery_archive import discovery_archive
from bot.widgets.month import send_month_widget
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
# Направление: IATA-код города (3 буквы) или код страны (2 буквы, поиск по всем её городам)
_TARGET_RE = re.compile(r"[A-Z]{2,3}")
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
        "начиная с самых дешёвых. Направление можно задать городом или целой страной.\n\n"
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
        "Теперь город или страна прибытия, например: <code>Стамбул</code> или <code>Турция</code>:"
    )


@router.message(MonthSearchStates.choosing_destination)
async def month_destination(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    # Название страны проверяем первым: «Грузия» или «Турция» — это поиск по всем городам страны
    destination = resolve_country(text) or city_resolver.resolve(text)
    if destination and not _TARGET_RE.fullmatch(destination):
        destination = None
    if destination and len(destination) == 2 and resolve_country(text) != destination:
        destination = None  # двухбуквенный код города нельзя путать с кодом страны
    if not destination:
        await message.answer("Не нашёл такой город или страну. Введите название (или IATA-код города) ещё раз:")
        return
    data = await state.get_data()
    origin = data.get("month_origin")
    if not origin:
        await state.clear()
        await message.answer("Поиск устарел. Нажмите кнопку «📅 Дешёвые на месяц» для нового поиска.")
        return
    await state.clear()

    progress = await message.answer("⏳ Ищу самые дешёвые билеты на ближайший месяц...")
    # Переключатель «Без пересадок» стартует с последнего выбора пользователя
    direct_only = await load_direct_only(message.from_user.id)
    # Native HTML bubbles are sent only to the owner in their private chat.
    # Always fetch the full window so that the widget can toggle direct flights
    # locally, without re-querying the supplier on every tap.
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if message.from_user and type(chat_id) is int and message.from_user.id == chat_id:
        try:
            tickets = await _load_tickets(origin, destination, False)
            if tickets:
                key = secrets.token_urlsafe(10)
                payload = {
                    "options": tickets,
                    "meta": {
                        "kind": "month", "owner_id": message.from_user.id,
                        "chat_id": message.chat.id, "origin": origin,
                        "destination": destination,
                        "origin_name": city_resolver.get_city_name(origin),
                        "target_name": _target_label(destination),
                        "start": datetime.now().date().isoformat(), "days": WINDOW_DAYS,
                    },
                }
                discovery_archive.put_month(key, payload)
                saved = discovery_archive.get(key)
                if await send_month_widget(message.bot, message.chat.id, key, saved, direct_only):
                    try:
                        await progress.delete()
                    except TelegramBadRequest:
                        pass
                    return
        except Exception:
            logger.exception("Month widget unavailable; retaining classic results")
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
    if not _IATA_RE.fullmatch(origin) or not _TARGET_RE.fullmatch(destination):
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
    if not _IATA_RE.fullmatch(origin) or not _TARGET_RE.fullmatch(destination) or direct not in ("0", "1"):
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
        await callback.answer("Кнопка устарела. Нажмите «📅 Дешёвые на месяц»", show_alert=True)
        return
    origin, destination, direct_only, page = parsed
    await _redraw(callback, origin, destination, direct_only, page)


@router.callback_query(F.data.startswith("mt:"))
async def cb_month_toggle(callback: CallbackQuery):
    parsed = _parse_toggle(callback.data)
    if not parsed:
        await callback.answer("Кнопка устарела. Нажмите «📅 Дешёвые на месяц»", show_alert=True)
        return
    origin, destination, direct_only = parsed

    saved = await save_direct_only(callback.from_user, direct_only)
    # Если запомнить не вышло, переключатель всё равно работает в этой выдаче
    await _redraw(
        callback, origin, destination, direct_only, 0,
        notice=None if saved else "Фильтр применён, но запомнить выбор не удалось",
    )


def _is_country(destination: str) -> bool:
    return len(destination) == 2


def _target_label(destination: str) -> str:
    if _is_country(destination):
        return f"{country_label(destination)} (все города)"
    return city_resolver.get_city_name(destination)


async def _load_tickets(origin: str, destination: str, direct_only: bool) -> list[dict]:
    if _is_country(destination):
        return await search_cheapest_to_country(
            origin, destination, days=WINDOW_DAYS, direct_only=direct_only,
        )
    tickets = await aviasales_api.search_cheapest_in_window(origin, destination, days=WINDOW_DAYS)
    if direct_only:
        tickets = [t for t in tickets if t.get("transfers") == 0]
    return tickets


def _line(rank: int, ticket: dict, origin: str, destination: str) -> str:
    # В поиске по стране у каждого билета свой город прибытия
    if _is_country(destination):
        destination = ticket.get("destination") or destination
        city = f"{html.escape(city_resolver.get_city_name(destination))} · "
    else:
        city = ""
    departure = ticket.get("departure_at", "")
    when = format_date_short(departure)
    time = format_time(departure)
    if time:
        when = f"{when}, {time}"

    airline_code = ticket.get("airline", "")
    airline = html.escape(city_resolver.get_airline_name(airline_code)) if airline_code else "—"
    transfers = ticket.get("transfers")
    transfers_text = ("пересадки не указаны" if type(transfers) is not int or transfers < 0
                      else "прямой" if transfers == 0 else format_transfers(transfers))

    link = html.escape(aviasales_api.build_ticket_link(ticket, origin, destination, None), quote=True)
    return (
        f"{rank}. <b>{format_price(ticket['price'])}</b> · {city}{when} · {airline} · {transfers_text}"
        f" — <a href=\"{link}\">проверить на Aviasales</a>"
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
    period = f"{format_date_short(today.isoformat())} — {format_date_short((today + timedelta(days=WINDOW_DAYS - 1)).isoformat())}"
    header = (
        f"<b>📅 Дешёвые на месяц: {html.escape(city_resolver.get_city_name(origin))}"
        f" → {html.escape(_target_label(destination))}</b>\n"
        f"{period} · от дешёвых к дорогим"
    )
    if direct_only:
        header += "\n✈️ Только без пересадок"

    if not tickets:
        body = (
            "Прямых билетов на ближайший месяц не нашёл. Выключите «Без пересадок», чтобы увидеть все варианты."
            if direct_only
            else "На ближайший месяц билетов по этому направлению не нашёл. Попробуйте другое направление."
            if not _is_country(destination)
            else "На ближайший месяц билетов в эту страну не нашёл. Попробуйте другую страну или город."
        )
        return f"{header}\n\n{body}", _keyboard(origin, destination, direct_only, 0, 1)

    start = page * PAGE_SIZE
    lines = [
        _line(start + i + 1, ticket, origin, destination)
        for i, ticket in enumerate(tickets[start:start + PAGE_SIZE])
    ]
    text = f"{header}\nВсего вариантов: {len(tickets)}\n\n" + "\n".join(lines)
    return text, _keyboard(origin, destination, direct_only, page, pages)


class FlexibleStates(StatesGroup):
    origin = State()
    duration = State()


@router.callback_query(F.data == "flex:start")
async def flex_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_flexible(callback.message, state)


async def start_flexible(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FlexibleStates.origin)
    await message.answer("🌍 Из какого города вылетаете? Введите город или IATA-код.")


@router.message(FlexibleStates.origin, F.text)
async def flex_origin(message: Message, state: FSMContext):
    origin = city_resolver.resolve(message.text.strip())
    if not origin or not _IATA_RE.fullmatch(origin) or not city_resolver.get_country_code(origin):
        await message.answer("Не удалось определить город вылета. Попробуйте ещё раз.")
        return
    await state.update_data(flex_origin=origin)
    await state.set_state(FlexibleStates.duration)
    rows = [[InlineKeyboardButton(text="В одну сторону · каждый день", callback_data="flex:0")]]
    for a, b in ((3, 5), (7, 10)):
        rows.append([InlineKeyboardButton(text=f"{n} ночей", callback_data=f"flex:{n}") for n in (a,b)])
    rows.append([InlineKeyboardButton(text="14 ночей", callback_data="flex:14")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:back")])
    await message.answer("Выберите длительность. Для поездки туда-обратно покажу общую цену авиабилетов за каждую дату вылета.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(FlexibleStates.duration, F.data.regexp(r"^flex:(0|3|5|7|10|14)$"))
async def flex_duration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    origin = (await state.get_data()).get("flex_origin")
    await state.clear()
    if not origin:
        return
    nights = int(callback.data.split(":")[1])
    direct = await load_direct_only(callback.from_user.id)
    progress = await callback.message.answer("⏳ Проверяю 30 дат вылета по данным Aviasales…")
    rows = await daily_foreign(origin, datetime.now(ZoneInfo(TIMEZONE)).date(), nights, direct)
    valid = [row for row in rows if row[2] is not None]
    best = min(valid, key=lambda row: row[2]["price"]) if valid else None
    header = f"🌍 <b>За границу из {html.escape(city_resolver.get_city_name(origin))}</b>\n"
    header += f"{nights} ночей · туда-обратно" if nights else "В одну сторону · на каждый день"
    if direct:
        header += " · без пересадок"
    if best:
        header += f"\n🏆 Лучшее из найденного: {format_date_short(best[0])} · {html.escape(city_resolver.get_city_name(best[2]['destination']))} · {format_price(best[2]['price'])}"
    header += "\nЦены из кэша Aviasales, наличие и стоимость уточняются при покупке.\n"
    await progress.edit_text(header, disable_web_page_preview=True)
    for offset in range(0, len(rows), 10):
        lines = []
        for departure, return_day, ticket, failed in rows[offset:offset+10]:
            label = format_date_short(departure)
            if return_day:
                label += "–" + format_date_short(return_day)
            if not ticket:
                lines.append(f"{label} · {'ошибка источника' if failed else 'нет данных'}")
                continue
            dest = ticket["destination"]
            link = aviasales_api.build_ticket_link(ticket, origin, dest, departure, return_day)
            city = html.escape(city_resolver.get_city_name(dest))
            lines.append(f'{label} · <a href="{html.escape(link, quote=True)}">{city} · {format_price(ticket["price"])}</a>')
        await callback.message.answer("\n".join(lines), disable_web_page_preview=True)
    buttons = []
    if best:
        dest = best[2]["destination"]
        buttons.append([InlineKeyboardButton(text="🔔 Следить за лучшим маршрутом", callback_data=f"sub:{origin}:{dest}:{best[0]}:{best[1] or ''}")])
    buttons.extend([[InlineKeyboardButton(text="🌍 Новый поиск", callback_data="flex:start")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:back")]])
    await callback.message.answer("Действия:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
