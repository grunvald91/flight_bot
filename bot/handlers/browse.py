"""Fast indicative Aviasales browsing; verification is requested per destination."""
import html
import logging
import re
import secrets
from datetime import date, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.handlers.tutu import _run_tutu_search
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.keyboards.common_kb import persistent_menu_keyboard
from bot.services.aviasales_api import aviasales_api
from bot.services.city_resolver import city_resolver
from bot.services.countries import country_label, resolve_country
from bot.services.discovery_archive import can_open_result, discovery_archive
from bot.services.fast_browse import (
    PAGE_SIZE, direct_view, load_indicative_destinations, page_options, visible_options,
)
from bot.services.search_prefs import load_direct_only, save_direct_only
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
        "<b>⚡ Дешёвые билеты за границу</b>\n\n"
        "Покажу, в какие страны и города сейчас дешевле всего улететь на выбранную дату. "
        "После выбора направления можно открыть билет или посмотреть, нужна ли виза и какие документы нужны.\n\n"
        "Укажите город вылета, например: <code>Сочи</code> или <code>Москва</code>. "
        "Дальше можно выбрать страну прилёта.",
        reply_markup=persistent_menu_keyboard(),
    )


@router.message(FastBrowseStates.choosing_origin)
async def browse_origin(message: Message, state: FSMContext):
    origin = city_resolver.resolve((message.text or "").strip())
    if not origin:
        await message.answer("Город не найден. Введите название или IATA-код ещё раз:")
        return
    await state.update_data(browse_origin=origin, browse_country=None)
    await state.set_state(FastBrowseStates.choosing_country)
    await message.answer(
        f"Вылет из: <b>{html.escape(city_resolver.get_city_name(origin))}</b>.\n\n"
        "Куда летим? Напишите страну прилёта, например: <code>Турция</code>, "
        "или нажмите «Все страны».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🌍 Все страны", callback_data="brc:any")
        ]]),
    )


async def _ask_date(message: Message, state: FSMContext, origin: str, country: str | None):
    await state.update_data(browse_country=country)
    await state.set_state(FastBrowseStates.choosing_date)
    where = f" → <b>{html.escape(country_label(country))}</b>" if country else ""
    await message.answer(
        f"Вылет из: <b>{html.escape(city_resolver.get_city_name(origin))}</b>{where}. "
        "Выберите дату или введите её как <code>ДД.ММ</code>:",
        reply_markup=calendar_keyboard("browse"),
    )


@router.callback_query(F.data == "brc:any", FastBrowseStates.choosing_country)
async def browse_any_country(callback: CallbackQuery, state: FSMContext):
    origin = (await state.get_data()).get("browse_origin")
    if not origin:
        await callback.answer("Поиск устарел. Начните заново через /browse.", show_alert=True)
        return
    await callback.answer()
    await _ask_date(callback.message, state, origin, None)


@router.message(FastBrowseStates.choosing_country)
async def browse_country(message: Message, state: FSMContext):
    country = resolve_country((message.text or "").strip())
    if not country:
        await message.answer(
            "Не нашёл такую страну. Напишите название страны (например: <code>Армения</code>) "
            "или нажмите «Все страны» выше."
        )
        return
    origin = (await state.get_data()).get("browse_origin")
    if not origin:
        await state.clear()
        await message.answer("Поиск устарел. Начните заново через /browse.")
        return
    await _ask_date(message, state, origin, country)


@router.callback_query(F.data.startswith("caldate:browse:"))
async def browse_calendar_date(callback: CallbackQuery, state: FSMContext):
    departure_date = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    origin = data.get("browse_origin")
    if not origin or not _valid_date(departure_date):
        await callback.answer("Поиск или дата недействительны. Начните заново.", show_alert=True)
        return
    country = data.get("browse_country")
    await state.clear()
    await callback.message.edit_text(f"Выбрана дата: <b>{_date_label(departure_date)}</b>")
    await callback.answer()
    await _run_browse(
        callback.message, origin, departure_date, owner_id=callback.from_user.id, country=country,
    )


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
    country = data.get("browse_country")
    await state.clear()
    await _run_browse(message, origin, departure_date, owner_id=message.from_user.id, country=country)


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


async def _run_browse(
    message: Message, origin: str, departure_date: str, *, owner_id: int, country: str | None = None,
):
    progress = await message.answer(
        "⏳ Собираю все доступные страницы Aviasales. Список появится после завершения сбора."
    )
    try:
        result = await load_indicative_destinations(origin, departure_date)
    except Exception:
        logger.exception("Indicative browse source failed")
        await progress.edit_text("Не удалось собрать направления полностью. Попробуйте позже.")
        return
    options = result.options
    if country:
        options = [option for option in options if option.get("country_code") == country]
    if not options:
        await progress.edit_text(
            f"В {country_label(country)} нет вариантов с ориентировочной ценой на эту дату. "
            "Попробуйте другую дату или страну."
            if country else "Нет зарубежных направлений с ориентировочной ценой на эту дату."
        )
        return
    key = secrets.token_urlsafe(8)
    payload = {
        "options": options,
        "meta": {"owner_id": owner_id, "chat_id": message.chat.id,
                 "origin": origin, "departure_date": departure_date, "country_code": country,
                 "source_rows": result.source_rows, "unresolved": result.unresolved,
                 "direct_checked": result.direct_checked},
    }
    try:
        discovery_archive.put_browse(key, payload)
    except Exception:
        logger.exception("Could not save indicative browse results")
        await progress.edit_text("Не удалось сохранить результаты. Попробуйте ещё раз.")
        return
    # Переключатель «Без пересадок» стартует с последнего выбора пользователя
    direct_only = await load_direct_only(owner_id)
    await progress.edit_text(
        _format_page(payload, 0, direct_only), disable_web_page_preview=True,
        reply_markup=_page_keyboard(key, payload, 0, direct_only),
    )


def _total_pages(count: int) -> int:
    return max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)


def _format_page(payload: dict, page: int, direct_only: bool = False) -> str:
    options = payload["options"]
    meta = payload["meta"]
    view = visible_options(options, direct_only)
    total_pages = _total_pages(len(view))
    country = meta.get("country_code")
    title = f"⚡ {country_label(country)}" if country else "⚡ Зарубежные направления"
    lines = [f"<b>{html.escape(title)} · {_date_label(meta['departure_date'])}</b>"]
    if direct_only:
        lines.append("✈️ Только без пересадок · цена самого дешёвого прямого рейса")
        if meta.get("direct_checked") is False:
            lines.append("⚠️ Прямые рейсы запросить не удалось, список может быть неполным.")
        lines.append(
            f"Вариантов: <b>{len(view)}</b> из {len(options)} · страница <b>{page + 1}/{total_pages}</b>"
        )
    else:
        lines.append(f"Вариантов: <b>{len(options)}</b> · страница <b>{page + 1}/{total_pages}</b>")
    lines += [
        "Сортировка: цена ↑, при равной цене — меньше пересадок.",
        "Выберите направление или сразу нажмите «ℹ️ Виза и документы».",
        "",
    ]
    if not view:
        lines.append("Вариантов без пересадок на эту дату нет. Выключите «Без пересадок», чтобы увидеть все.")
    for rank, (_, option) in enumerate(page_options(view, page), start=page * PAGE_SIZE + 1):
        lines.append(
            f"<b>{rank}.</b> {html.escape(str(option['country']))} — "
            f"{html.escape(str(option['city']))} · ≈{_money(option['price'])} ₽ · "
            f"{_stops_label(option.get('transfers'))}"
        )
    lines.append("\n⚠️ Цены Aviasales ориентировочные: наличие и итоговую цену нужно проверить у провайдера.")
    if meta.get("unresolved"):
        lines.append(f"Не определена страна для {meta['unresolved']} строк источника; они не включены.")
    return "\n".join(lines)


def _page_keyboard(key: str, payload: dict, page: int, direct_only: bool = False) -> InlineKeyboardMarkup:
    view = visible_options(payload["options"], direct_only)
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(
            text="✈️ Без пересадок: ВКЛ ✅" if direct_only else "✈️ Без пересадок: ВЫКЛ",
            callback_data=f"brdirect:{key}:{int(not direct_only)}",
        )
    ]]
    for rank, (index, option) in enumerate(page_options(view, page), start=page * PAGE_SIZE + 1):
        city = str(option.get("city") or option.get("destination") or "Направление")
        if len(city) > 22:
            city = city[:21] + "…"
        row = [
            InlineKeyboardButton(
                text=f"{rank}. {city}",
                callback_data=f"brsel:{key}:{index}:{int(direct_only)}",
            )
        ]
        country_code = str(option.get("country_code") or "").upper()
        if len(country_code) == 2:
            row.append(
                InlineKeyboardButton(
                    text="ℹ️ Виза и документы",
                    callback_data=f"entry:{country_code}",
                )
            )
        rows.append(row)

    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"brpage:{key}:{page - 1}:{int(direct_only)}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{_total_pages(len(view))}", callback_data="calnoop"))
    if (page + 1) * PAGE_SIZE < len(view):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"brpage:{key}:{page + 1}:{int(direct_only)}"))
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


_BUTTON_RE = re.compile(r"(brpage|brsel|brtutu):([A-Za-z0-9_-]{1,40}):([0-9]{1,5})(?::([01]))?")


def _parse_button(data: str | None, prefix: str) -> tuple[str, int, bool | None] | None:
    """Кнопка списка: (ключ, номер, фильтр «Без пересадок» или None у кнопок из старых сообщений)."""
    match = _BUTTON_RE.fullmatch(data or "")
    if not match or match.group(1) != prefix:
        return None
    flag = match.group(4)
    return match.group(2), int(match.group(3)), (None if flag is None else flag == "1")


async def _direct_only_for(callback: CallbackQuery, flag: bool | None) -> bool:
    """Состояние фильтра берётся из самой кнопки; у старых кнопок — последний выбор пользователя."""
    return flag if flag is not None else await load_direct_only(callback.from_user.id)


@router.callback_query(F.data.startswith("brpage:"))
async def browse_page(callback: CallbackQuery):
    parsed = _parse_button(callback.data, "brpage")
    if parsed is None:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    key, page, flag = parsed
    payload = await _payload(callback, key)
    if payload is None:
        return
    direct_only = await _direct_only_for(callback, flag)
    view = visible_options(payload["options"], direct_only)
    if page > 0 and not page_options(view, page):
        await callback.answer("Такой страницы нет.", show_alert=True)
        return
    await callback.message.edit_text(
        _format_page(payload, page, direct_only), disable_web_page_preview=True,
        reply_markup=_page_keyboard(key, payload, page, direct_only),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brdirect:"))
async def browse_direct_toggle(callback: CallbackQuery):
    match = re.fullmatch(r"brdirect:([A-Za-z0-9_-]{1,40}):([01])", callback.data or "")
    if not match:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    key, raw_value = match.groups()
    payload = await _payload(callback, key)
    if payload is None:
        return
    direct_only = raw_value == "1"
    saved = await save_direct_only(callback.from_user, direct_only)
    # Если запомнить не вышло, переключатель всё равно работает в этой выдаче
    try:
        await callback.message.edit_text(
            _format_page(payload, 0, direct_only), disable_web_page_preview=True,
            reply_markup=_page_keyboard(key, payload, 0, direct_only),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
    await callback.answer(None if saved else "Фильтр применён, но запомнить выбор не удалось")


def _selected_keyboard(
    key: str, index: int, option: dict, departure_date: str,
    back_page: int | None = None, direct_only: bool | None = None,
) -> InlineKeyboardMarkup:
    url = aviasales_api.build_search_link(option["origin"], option["destination"], departure_date)
    rows = [
        [InlineKeyboardButton(text="🟣 Туту", callback_data=f"brtutu:{key}:{index}"),
         InlineKeyboardButton(text="✈️ Aviasales ↗", url=url)],
    ]
    country_code = str(option.get("country_code") or "").upper()
    if len(country_code) == 2:
        rows.append([
            InlineKeyboardButton(
                text="ℹ️ Виза и документы",
                callback_data=f"entry:{country_code}",
            )
        ])
    if back_page is None:
        back_page = index // PAGE_SIZE
    back = f"brpage:{key}:{back_page}" + ("" if direct_only is None else f":{int(direct_only)}")
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_page(options: list[dict], index: int, direct_only: bool) -> int:
    """Страница списка, на которой стоит вариант `index`, с учётом фильтра «Без пересадок»."""
    positions = [i for i, _ in visible_options(options, direct_only)]
    return positions.index(index) // PAGE_SIZE if index in positions else 0


@router.callback_query(F.data.startswith("brsel:"))
async def browse_select(callback: CallbackQuery):
    selected = await _selected(callback)
    if selected is None:
        return
    key, index, payload, option, flag = selected
    direct_only = await _direct_only_for(callback, flag)
    positions = [i for i, _ in visible_options(payload["options"], direct_only)]
    if index in positions:  # номер и общее число — как в списке, из которого открыли
        number, total = positions.index(index) + 1, len(positions)
    else:
        number, total = index + 1, len(payload["options"])
    if direct_only:  # в этом режиме показываем самый дешёвый прямой билет
        option = direct_view(option) or option
    departure_at = option.get("departure_at") or (payload["meta"]["departure_date"])
    lines = [
        f"<b>⚡ Направление {number} из {total}</b>",
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
        reply_markup=_selected_keyboard(
            key, index, option, payload["meta"]["departure_date"],
            back_page=_back_page(payload["options"], index, direct_only),
            direct_only=direct_only,
        ),
    )
    await callback.answer()


async def _selected(callback: CallbackQuery):
    prefix = (callback.data or "").split(":", 1)[0]
    parsed = _parse_button(callback.data, prefix) if prefix in ("brsel", "brtutu") else None
    if parsed is None:
        await callback.answer("Некорректное направление", show_alert=True)
        return None
    key, index, flag = parsed
    payload = await _payload(callback, key)
    if payload is None:
        return None
    options = payload["options"]
    if index >= len(options):
        await callback.answer("Такого направления нет", show_alert=True)
        return None
    return key, index, payload, options[index], flag


@router.callback_query(F.data.startswith("brtutu:"))
async def browse_tutu(callback: CallbackQuery):
    selected = await _selected(callback)
    if selected is None:
        return
    _, _, payload, option, _ = selected
    await callback.answer("Проверяю направление через Туту…")
    origin_name = city_resolver.get_city_name(option["origin"])
    await _run_tutu_search(
        callback.message, origin_name, option["city"], payload["meta"]["departure_date"],
        owner_id=callback.from_user.id,
    )
