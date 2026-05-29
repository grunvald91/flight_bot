"""Админ-панель с агрегированной статистикой и переключением периодов."""
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.city_resolver import city_resolver
from database.crud import (
    get_favorite_count_stats,
    get_request_stats,
    get_subscription_count_stats,
    get_top_favorite_routes,
    get_top_subscription_routes,
    get_user_count_stats,
    get_user_detail,
    get_users_paginated,
)

logger = logging.getLogger(__name__)

router = Router()


def _parse_admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "437862772")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


ADMIN_IDS = _parse_admin_ids()

PERIODS = {
    "1": ("За сутки", 1),
    "7": ("За неделю", 7),
    "30": ("За месяц", 30),
    "all": ("За всё время", None),
}

USERS_PAGE_SIZE = 8


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _format_period(days: int | None) -> str:
    if days is None:
        return "всё время"
    if days == 1:
        return "сутки"
    return f"{days} дн."


def _period_keyboard(section: str, current: str) -> list[list[InlineKeyboardButton]]:
    row: list[InlineKeyboardButton] = []
    for key, (label, _) in PERIODS.items():
        mark = "• " if key == current else ""
        row.append(InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"adm:{section}:{key}",
        ))
    rows = [row[i:i + 2] for i in range(0, len(row), 2)]
    return rows


def _admin_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Общая сводка", callback_data="adm:overview:7")],
        [InlineKeyboardButton(text="👥 Пользователи (статистика)", callback_data="adm:users:7")],
        [InlineKeyboardButton(text="👤 Все пользователи (список)", callback_data="adm:userlist:0")],
        [InlineKeyboardButton(text="🔔 Подписки", callback_data="adm:subs:all")],
        [InlineKeyboardButton(text="⭐ Избранное", callback_data="adm:favs:all")],
        [InlineKeyboardButton(text="🔥 Топ маршрутов (запросы)", callback_data="adm:routes:7")],
        [InlineKeyboardButton(text="🏆 Топ пользователей", callback_data="adm:topusers:7")],
        [InlineKeyboardButton(text="🆕 Последние запросы", callback_data="adm:recent:1")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:root")],
    ])


def _section_keyboard(section: str, current_period: str) -> InlineKeyboardMarkup:
    rows = _period_keyboard(section, current_period)
    rows.append([InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _route_label(origin: str, destination: str) -> str:
    o_name = city_resolver.get_city_name(origin) or origin
    d_name = city_resolver.get_city_name(destination) or destination
    return f"{o_name} → {d_name}"


async def _render_root() -> tuple[str, InlineKeyboardMarkup]:
    user_stats = await get_user_count_stats(days=7)
    sub_stats = await get_subscription_count_stats()
    fav_stats = await get_favorite_count_stats()
    req = await get_request_stats(days=1, limit=1)

    text = (
        "<b>🛠 Админка</b>\n\n"
        f"<b>Пользователи:</b> {user_stats['total']} (новых за 7 дн: {user_stats['new_in_period']}, активных: {user_stats['active_in_period']})\n"
        f"<b>Подписки:</b> {sub_stats['total_active']} акт. · {sub_stats['paused']} пауза · {sub_stats['muted']} без уведом.\n"
        f"<b>Избранное:</b> {fav_stats['total']} (уникальных маршрутов: {fav_stats['unique_routes']})\n"
        f"<b>Запросов за сутки:</b> {req['total']} от {req['unique_users']} польз.\n\n"
        "Выберите раздел:"
    )
    return text, _admin_root_keyboard()


async def _render_overview(period_key: str) -> tuple[str, InlineKeyboardMarkup]:
    _, days = PERIODS[period_key]
    user_stats = await get_user_count_stats(days=days)
    sub_stats = await get_subscription_count_stats()
    fav_stats = await get_favorite_count_stats()
    req = await get_request_stats(days=days or 9999, limit=1)

    lines = [
        f"<b>📊 Общая сводка</b> · {_format_period(days)}",
        "",
        "<b>Пользователи</b>",
        f"  Всего: <b>{user_stats['total']}</b>",
        f"  Новых: <b>{user_stats['new_in_period']}</b>",
        f"  Активных: <b>{user_stats['active_in_period']}</b>",
        "",
        "<b>Подписки</b>",
        f"  Активных: <b>{sub_stats['total_active']}</b>",
        f"  На паузе: <b>{sub_stats['paused']}</b>",
        f"  Без уведомлений: <b>{sub_stats['muted']}</b>",
        f"  С лимитом цены: <b>{sub_stats['with_max_price']}</b>",
        f"  На конкретную дату: <b>{sub_stats['with_date']}</b>",
        "",
        "<b>Избранное</b>",
        f"  Всего записей: <b>{fav_stats['total']}</b>",
        f"  Уникальных маршрутов: <b>{fav_stats['unique_routes']}</b>",
        "",
        "<b>Запросы</b>",
        f"  Всего: <b>{req['total']}</b>",
        f"  Уникальных польз.: <b>{req['unique_users']}</b>",
    ]
    return "\n".join(lines), _section_keyboard("overview", period_key)


async def _render_users(period_key: str) -> tuple[str, InlineKeyboardMarkup]:
    _, days = PERIODS[period_key]
    user_stats = await get_user_count_stats(days=days)
    req = await get_request_stats(days=days or 9999, limit=1)

    lines = [
        f"<b>👥 Пользователи</b> · {_format_period(days)}",
        "",
        f"Всего зарегистрировано: <b>{user_stats['total']}</b>",
        f"Новых за период: <b>{user_stats['new_in_period']}</b>",
        f"Активных за период: <b>{user_stats['active_in_period']}</b>",
        f"Уникальных в запросах: <b>{req['unique_users']}</b>",
        "",
        f"Запросов за период: <b>{req['total']}</b>",
    ]
    if req["by_type"]:
        lines.append("\n<b>По типам запросов:</b>")
        type_names = {"command": "Команды", "inline": "Инлайн", "callback": "Кнопки", "message": "Сообщения"}
        for t, cnt in req["by_type"].items():
            lines.append(f"  {type_names.get(t, t)}: <b>{cnt}</b>")
    return "\n".join(lines), _section_keyboard("users", period_key)


async def _render_subs() -> tuple[str, InlineKeyboardMarkup]:
    sub_stats = await get_subscription_count_stats()
    top = await get_top_subscription_routes(limit=10)

    lines = [
        "<b>🔔 Подписки</b>",
        "",
        f"Активных всего: <b>{sub_stats['total_active']}</b>",
        f"На паузе: <b>{sub_stats['paused']}</b>",
        f"Без уведомлений: <b>{sub_stats['muted']}</b>",
        f"С лимитом цены: <b>{sub_stats['with_max_price']}</b>",
        f"На конкретную дату: <b>{sub_stats['with_date']}</b>",
    ]
    if top:
        lines.append("\n<b>Топ маршрутов:</b>")
        for r in top:
            lines.append(f"  {_route_label(r['origin'], r['destination'])} — <b>{r['count']}</b>")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:subs:all")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:root")],
    ])
    return "\n".join(lines), keyboard


async def _render_favs() -> tuple[str, InlineKeyboardMarkup]:
    fav_stats = await get_favorite_count_stats()
    top = await get_top_favorite_routes(limit=10)

    lines = [
        "<b>⭐ Избранное</b>",
        "",
        f"Всего записей: <b>{fav_stats['total']}</b>",
        f"Пользователей с избранным: <b>{fav_stats['unique_users']}</b>",
        f"Уникальных маршрутов: <b>{fav_stats['unique_routes']}</b>",
    ]
    if top:
        lines.append("\n<b>Топ маршрутов:</b>")
        for r in top:
            lines.append(f"  {_route_label(r['origin'], r['destination'])} — <b>{r['count']}</b>")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:favs:all")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:root")],
    ])
    return "\n".join(lines), keyboard


async def _render_routes(period_key: str) -> tuple[str, InlineKeyboardMarkup]:
    _, days = PERIODS[period_key]
    req = await get_request_stats(days=days or 9999, limit=1)

    lines = [f"<b>🔥 Топ маршрутов в запросах</b> · {_format_period(days)}", ""]
    if not req["popular_routes"]:
        lines.append("<i>Запросов пока нет.</i>")
    else:
        for i, r in enumerate(req["popular_routes"][:15], 1):
            query = r["query"] or "—"
            if len(query) > 50:
                query = query[:50] + "…"
            lines.append(f"{i}. <code>{query}</code> — <b>{r['count']}</b>")
    return "\n".join(lines), _section_keyboard("routes", period_key)


async def _render_top_users(period_key: str) -> tuple[str, InlineKeyboardMarkup]:
    _, days = PERIODS[period_key]
    req = await get_request_stats(days=days or 9999, limit=1)

    lines = [f"<b>🏆 Топ пользователей</b> · {_format_period(days)}", ""]
    if not req["top_users"]:
        lines.append("<i>Активности пока нет.</i>")
    else:
        for i, u in enumerate(req["top_users"][:15], 1):
            name = u["first_name"] or u["username"] or str(u["telegram_id"])
            uname = f" @{u['username']}" if u["username"] else ""
            lines.append(f"{i}. {name}{uname} — <b>{u['count']}</b> запр. (id <code>{u['telegram_id']}</code>)")
    return "\n".join(lines), _section_keyboard("topusers", period_key)


async def _render_recent(period_key: str) -> tuple[str, InlineKeyboardMarkup]:
    _, days = PERIODS[period_key]
    req = await get_request_stats(days=days or 9999, limit=25)

    lines = [f"<b>🆕 Последние запросы</b> · {_format_period(days)}", ""]
    if not req["recent"]:
        lines.append("<i>Активности пока нет.</i>")
    else:
        for r in req["recent"][:25]:
            time_str = r.created_at.strftime("%d.%m %H:%M") if r.created_at else "?"
            name = r.first_name or r.username or str(r.telegram_id)
            data = (r.request_data or "").replace("\n", " ")
            if len(data) > 40:
                data = data[:40] + "…"
            lines.append(f"  <code>{time_str}</code> · {name} · {r.request_type} · <code>{data}</code>")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n\n<i>…обрезано</i>"
    return text, _section_keyboard("recent", period_key)


def _format_dt(value) -> str:
    if not value:
        return "—"
    return value.strftime("%d.%m.%Y %H:%M")


def _format_dt_short(value) -> str:
    if not value:
        return "—"
    return value.strftime("%d.%m %H:%M")


def _user_display(u: dict | object) -> str:
    if isinstance(u, dict):
        first = u.get("first_name")
        username = u.get("username")
        tid = u.get("telegram_id")
    else:
        first = getattr(u, "first_name", None)
        username = getattr(u, "username", None)
        tid = getattr(u, "telegram_id", None)
    parts = []
    if first:
        parts.append(first)
    if username:
        parts.append(f"@{username}")
    if not parts:
        parts.append(str(tid))
    return " ".join(parts)


async def _render_userlist(page_key: str) -> tuple[str, InlineKeyboardMarkup]:
    try:
        page = max(0, int(page_key))
    except ValueError:
        page = 0

    offset = page * USERS_PAGE_SIZE
    users, total = await get_users_paginated(offset=offset, limit=USERS_PAGE_SIZE)

    if total == 0:
        text = "<b>👤 Все пользователи</b>\n\n<i>Пользователей пока нет.</i>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:root")],
        ])
        return text, keyboard

    pages = (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE
    lines = [
        f"<b>👤 Все пользователи ({total})</b>",
        f"<i>Страница {page + 1} из {pages}</i>",
        "",
        "Сортировка: по последней активности.",
        "Нажмите на пользователя, чтобы открыть карточку:",
    ]

    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        name = _user_display(u)
        subs = u["sub_count"]
        favs = u["fav_count"]
        last = _format_dt_short(u["last_activity"])
        chip = f"{name} · 🔔{subs} ⭐{favs} · {last}"
        if len(chip) > 64:
            chip = chip[:61] + "…"
        rows.append([
            InlineKeyboardButton(text=chip, callback_data=f"adm:user:{u['telegram_id']}")
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:userlist:{page - 1}"))
    if (page + 1) < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:userlist:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:root")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_user_card(telegram_id_str: str) -> tuple[str, InlineKeyboardMarkup]:
    try:
        telegram_id = int(telegram_id_str)
    except ValueError:
        return "Ошибка ID пользователя", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:userlist:0")],
        ])

    detail = await get_user_detail(telegram_id)
    if not detail:
        return "Пользователь не найден.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:userlist:0")],
        ])

    user = detail["user"]
    subs = detail["subscriptions"]
    favs = detail["favorites"]
    recent = detail["recent_requests"]

    name_line = _user_display(user)
    lines = [
        f"<b>👤 {name_line}</b>",
        "",
        f"ID: <code>{user.telegram_id}</code>",
        f"Регистрация: {_format_dt(user.created_at)}",
        f"Последняя активность: {_format_dt(detail['last_activity'])}",
        f"Запросов всего: <b>{detail['total_requests']}</b>",
        "",
        f"<b>🔔 Подписки:</b> {len(subs)}",
    ]
    for s in subs[:10]:
        route = _route_label(s.origin, s.destination)
        flags = []
        if s.is_paused:
            flags.append("⏸")
        if getattr(s, "notify_enabled", True) is False:
            flags.append("🔕")
        if s.depart_date:
            flags.append(s.depart_date.strftime("%d.%m"))
        if s.last_price:
            from bot.utils.formatters import format_price as _fp
            flags.append(_fp(s.last_price))
        flag_str = " · ".join(flags)
        lines.append(f"  • {route}" + (f" — {flag_str}" if flag_str else ""))
    if len(subs) > 10:
        lines.append(f"  <i>…ещё {len(subs) - 10}</i>")

    lines.append("")
    lines.append(f"<b>⭐ Избранное:</b> {len(favs)}")
    for f in favs[:10]:
        lines.append(f"  • {_route_label(f.origin, f.destination)}")
    if len(favs) > 10:
        lines.append(f"  <i>…ещё {len(favs) - 10}</i>")

    if recent:
        lines.append("")
        lines.append("<b>🆕 Последние действия:</b>")
        for r in recent[:10]:
            data = (r.request_data or "").replace("\n", " ")
            if len(data) > 35:
                data = data[:35] + "…"
            lines.append(f"  <code>{_format_dt_short(r.created_at)}</code> · {r.request_type} · <code>{data}</code>")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n\n<i>…обрезано</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="adm:userlist:0")],
        [InlineKeyboardButton(text="🏠 В админку", callback_data="adm:root")],
    ])
    return text, keyboard


_RENDERERS = {
    "overview": _render_overview,
    "users": _render_users,
    "routes": _render_routes,
    "topusers": _render_top_users,
    "recent": _render_recent,
}


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message.from_user.id):
        return
    text, keyboard = await _render_root()
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "adm:root")
async def cb_admin_root(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    text, keyboard = await _render_root()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_section(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    section = parts[1]
    period_key = parts[2]

    if section == "subs":
        text, keyboard = await _render_subs()
    elif section == "favs":
        text, keyboard = await _render_favs()
    elif section == "userlist":
        text, keyboard = await _render_userlist(period_key)
    elif section == "user":
        text, keyboard = await _render_user_card(period_key)
    else:
        renderer = _RENDERERS.get(section)
        if not renderer:
            await callback.answer()
            return
        if period_key not in PERIODS:
            period_key = "7"
        text, keyboard = await renderer(period_key)

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
