import logging
import html
import re
from zoneinfo import ZoneInfo
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import MIN_PRICE_DROP, TIMEZONE
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.keyboards.common_kb import (
    subscription_delete_confirm_keyboard,
    subscription_keyboard,
    subscriptions_list_keyboard,
)
from bot.services.city_resolver import city_resolver
from bot.utils.formatters import format_price
from database.crud import (
    add_subscription,
    get_or_create_user,
    get_subscription,
    get_subscriptions,
    get_user_settings,
    pause_all_subscriptions,
    pause_subscription,
    remove_subscription,
    set_subscription_notifications,
    update_subscription_min_drop,
    update_subscription_max_price,
    update_user_settings,
)

logger = logging.getLogger(__name__)

router = Router()

MIN_DROP_PRESETS = (300, 500, 1000, 2000)
SUBS_PAGE_SIZE = 8


class ManageSubscriptionsStates(StatesGroup):
    waiting_min_price_drop = State()
    waiting_sub_min_drop = State()
    waiting_max_price = State()


class NewAlertStates(StatesGroup):
    origin = State()
    destination = State()
    departure = State()
    return_day = State()
    target = State()


def _parse_date_arg(text: str) -> date | None:
    """Парсит DD.MM или DD.MM.YYYY."""
    text = text.strip()
    now = datetime.now()

    try:
        if len(text) <= 5:
            parsed = datetime.strptime(text, "%d.%m")
            result = parsed.replace(year=now.year).date()
            if result < now.date():
                result = result.replace(year=now.year + 1)
            return result
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_min_price_drop_input(text: str) -> int | None:
    if not text:
        return None

    normalized = text.strip().replace(" ", "").replace("\u00a0", "")
    if not re.fullmatch(r"[0-9]{1,6}", normalized):
        return None

    value = int(normalized)
    if value < 100 or value > 200000:
        return None
    return value


async def _ensure_user(user) -> None:
    await get_or_create_user(
        user.id,
        username=user.username,
        first_name=user.first_name,
    )


def _notifications_enabled(settings) -> bool:
    return not settings or settings.notifications_enabled is not False


def _subscription_manage_keyboard(settings) -> InlineKeyboardMarkup:
    notifications_on = _notifications_enabled(settings)
    direct_only = bool(settings and settings.direct_flights_only)

    notifications_text = "🔔 Уведомления: Вкл" if notifications_on else "🔕 Уведомления: Выкл"
    notifications_data = "submgr:notifications_off" if notifications_on else "submgr:notifications_on"

    direct_text = "✈️ Прямые рейсы: Вкл" if direct_only else "✈️ Прямые рейсы: Выкл"
    direct_data = "submgr:direct_off" if direct_only else "submgr:direct_on"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=notifications_text, callback_data=notifications_data)],
            [InlineKeyboardButton(text=direct_text, callback_data=direct_data)],
            [InlineKeyboardButton(text="📉 Мин. снижение цены", callback_data="submgr:min_drop")],
            [InlineKeyboardButton(text="✉️ Проверить доставку", callback_data="alerttest:send")],
            [
                InlineKeyboardButton(text="⏸ Пауза всех", callback_data="submgr:pause_all"),
                InlineKeyboardButton(text="▶️ Возобновить все", callback_data="submgr:resume_all"),
            ],
            [InlineKeyboardButton(text="⬅️ К списку подписок", callback_data="submgr:back")],
        ]
    )


def _min_drop_keyboard(current_value: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for value in MIN_DROP_PRESETS:
        mark = "✅ " if value == current_value else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{format_price(value)}",
                    callback_data=f"submgr:min_drop_set:{value}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="submgr:min_drop_custom")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="submgr:open")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _subscriptions_header_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Новое оповещение", callback_data="alertnew:start")],
            [InlineKeyboardButton(text="⚙️ Общие настройки", callback_data="submgr:open")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:back")],
        ]
    )


def _sub_label(sub) -> str:
    origin_name = city_resolver.get_city_name(sub.origin)
    dest_name = city_resolver.get_city_name(sub.destination)
    label = f"{origin_name} → {dest_name}"
    if sub.depart_date:
        label += f" · {sub.depart_date.strftime('%d.%m')}"
    if sub.return_date:
        label += f"–{sub.return_date.strftime('%d.%m')}"
    if sub.last_price:
        label += f" · {format_price(sub.last_price)}"
    return label


def _format_sub_card(sub, default_min_drop: int, global_enabled: bool = True) -> str:
    origin_name = html.escape(city_resolver.get_city_name(sub.origin))
    dest_name = html.escape(city_resolver.get_city_name(sub.destination))

    status_lines = []
    if sub.is_paused:
        status_lines.append("⏸ <b>На паузе</b> — слежение остановлено")
    else:
        notif_state = "🔔 включены" if getattr(sub, "notify_enabled", True) is not False else "🔕 выключены"
        status_lines.append(f"✅ Слежение активно · Уведомления {notif_state}")

    if not global_enabled:
        status_lines[0] += "\n🔕 Общие оповещения отключены"

    date_line = (
        f"📅 Дата: <b>{sub.depart_date.strftime('%d.%m.%Y')}</b>"
        if sub.depart_date
        else "📅 Период: ближайшие 30 дней"
    )

    price_line = (
        f"💰 Текущая цена: <b>{format_price(sub.last_price)}</b>"
        if sub.last_price
        else "💰 Цена ещё не проверялась"
    )

    threshold = sub.min_price_drop or default_min_drop
    threshold_note = "" if sub.min_price_drop else " (из общих настроек)"

    text = (
        f"<b>{origin_name} ({sub.origin}) → {dest_name} ({sub.destination})</b>\n\n"
        f"{status_lines[0]}\n"
        f"{date_line}\n"
        f"{price_line}\n"
    )
    if sub.return_date:
        text += f"↩️ Обратно: <b>{sub.return_date.strftime('%d.%m.%Y')}</b>\n"
    if sub.last_checked_at:
        text += f"🕒 Последняя успешная проверка: {sub.last_checked_at.strftime('%d.%m %H:%M')} UTC\n"
    text += "Проверка: каждый час. Цены ориентировочные.\n"
    if sub.max_price:
        text += f"🎯 Лимит цены: <b>{format_price(sub.max_price)}</b>\n"
    text += f"📉 Уведомлю при снижении от <b>{format_price(threshold)}</b>{threshold_note}"
    return text


def _sub_min_drop_keyboard(sub_id: int, current_value: int | None, default_value: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for value in MIN_DROP_PRESETS:
        mark = "✅ " if current_value == value else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{mark}{format_price(value)}",
                callback_data=f"subact:mindropset:{sub_id}:{value}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text=("✅ " if current_value is None else "") + f"По умолчанию ({format_price(default_value)})",
            callback_data=f"subact:mindropg:{sub_id}",
        )
    ])
    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data=f"subact:mindropcustom:{sub_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"subcard:{sub_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_subscription_card(
    target_message: Message,
    telegram_id: int,
    sub_id: int,
    edit: bool = True,
) -> bool:
    sub = await get_subscription(sub_id, telegram_id)
    if not sub:
        return False

    settings = await get_user_settings(telegram_id)
    default_min = settings.min_price_drop if settings and settings.min_price_drop else MIN_PRICE_DROP
    text = _format_sub_card(sub, default_min, _notifications_enabled(settings))

    keyboard = subscription_keyboard(
        sub.id,
        sub.origin,
        sub.destination,
        is_paused=sub.is_paused,
        notify_enabled=getattr(sub, "notify_enabled", True) is not False,
    )

    if edit:
        try:
            await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return True
        except Exception:
            pass

    await target_message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    return True


async def _show_subscriptions_list(
    target_message: Message,
    telegram_id: int,
    page: int = 0,
    edit: bool = True,
) -> None:
    subscriptions = await get_subscriptions(telegram_id)
    total = len(subscriptions)
    settings = await get_user_settings(telegram_id)
    global_status = "включены" if _notifications_enabled(settings) else "отключены"

    if total == 0:
        text = (
            f"<b>🔔 Оповещения</b>\nУведомления {global_status} · каждый час\n\n"
            "У вас пока нет активных подписок.\n\n"
            "Нажмите «Новое оповещение» или «Следить за ценой» под билетом.\n"
            "Проверка цен каждый час."
        )
        keyboard = _subscriptions_header_keyboard()
        if edit:
            try:
                await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                return
            except Exception:
                pass
        await target_message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return

    page = max(0, min(page, (total - 1) // SUBS_PAGE_SIZE))
    start = page * SUBS_PAGE_SIZE
    page_items = subscriptions[start:start + SUBS_PAGE_SIZE]

    active = sum(1 for s in subscriptions if not s.is_paused)
    paused = total - active
    muted = sum(
        1 for s in subscriptions
        if not s.is_paused and getattr(s, "notify_enabled", True) is False
    )

    text_lines = [
        f"<b>🔔 Оповещения ({total})</b>",
        f"Уведомления {global_status} · проверка каждый час",
        "",
        f"Активных: <b>{active}</b> · На паузе: <b>{paused}</b>" + (f" · Без уведомлений: <b>{muted}</b>" if muted else ""),
        "",
        "Нажмите на маршрут, чтобы открыть карточку:",
    ]
    if total > SUBS_PAGE_SIZE:
        pages = (total + SUBS_PAGE_SIZE - 1) // SUBS_PAGE_SIZE
        text_lines.append(f"\n<i>Страница {page + 1} из {pages}</i>")

    items = [
        (sub.id, _sub_label(sub), sub.is_paused, getattr(sub, "notify_enabled", True) is not False)
        for sub in page_items
    ]
    keyboard = subscriptions_list_keyboard(items, page, SUBS_PAGE_SIZE, total)
    text = "\n".join(text_lines)

    if edit:
        try:
            await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            pass
    await target_message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def _build_management_text(telegram_id: int) -> str:
    settings = await get_user_settings(telegram_id)
    subscriptions = await get_subscriptions(telegram_id)

    notifications_on = _notifications_enabled(settings)
    direct_only = bool(settings and settings.direct_flights_only)
    min_drop = settings.min_price_drop if settings and settings.min_price_drop else MIN_PRICE_DROP

    total = len(subscriptions)
    paused = len([sub for sub in subscriptions if sub.is_paused])

    return (
        "<b>⚙️ Управление подписками</b>\n\n"
        f"Подписок всего: <b>{total}</b>\n"
        f"На паузе: <b>{paused}</b>\n\n"
        f"Уведомления о ценах: <b>{'ВКЛ' if notifications_on else 'ВЫКЛ'}</b>\n"
        f"Только прямые рейсы: <b>{'ВКЛ' if direct_only else 'ВЫКЛ'}</b>\n"
        f"Мин. снижение цены: <b>{format_price(min_drop)}</b>\n\n"
        "Выберите действие:" 
    )


async def _show_management(target_message: Message, telegram_id: int, edit: bool = False):
    settings = await get_user_settings(telegram_id)
    text = await _build_management_text(telegram_id)
    keyboard = _subscription_manage_keyboard(settings)

    if edit:
        try:
            await target_message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            pass

    await target_message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    """Подписка: /subscribe MOW-IST [дата] [лимит] [мин_снижение]."""
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "<b>Подписка на мониторинг цен</b>\n\n"
            "Формат: <code>/subscribe MOW-IST</code>\n"
            "С датой: <code>/subscribe MOW-IST 15.03</code>\n"
            "С лимитом: <code>/subscribe MOW-IST 15.03 15000</code>\n\n"
            f"По умолчанию уведомление при снижении от {format_price(MIN_PRICE_DROP)}.\n"
            "Без даты мониторинг идёт на ближайшие 30 дней.\n"
            "Проверка цен выполняется каждый час.",
            parse_mode="HTML",
        )
        return

    parts = args[1].strip().split()
    route = parts[0]
    depart_date = None
    max_price = None
    min_price_drop = None

    extra_parts = parts[1:]
    price_args: list[str] = []

    for part in extra_parts:
        parsed = _parse_date_arg(part)
        if parsed and depart_date is None:
            depart_date = parsed
        else:
            price_args.append(part)

    if len(price_args) > 0:
        try:
            max_price = int(price_args[0])
        except ValueError:
            pass

    if len(price_args) > 1:
        try:
            min_price_drop = int(price_args[1])
        except ValueError:
            pass

    if "-" not in route:
        await message.answer("Формат маршрута: <code>MOW-IST</code>", parse_mode="HTML")
        return

    origin_raw, destination_raw = route.split("-", 1)
    origin = city_resolver.resolve(origin_raw)
    destination = city_resolver.resolve(destination_raw)

    if not origin or not destination:
        not_found = origin_raw if not origin else destination_raw
        await message.answer(f"Город не найден: {not_found}")
        return

    added = await add_subscription(
        message.from_user.id,
        origin,
        destination,
        max_price=max_price,
        min_price_drop=min_price_drop,
        depart_date=depart_date,
    )

    origin_name = city_resolver.get_city_name(origin)
    destination_name = city_resolver.get_city_name(destination)

    if not added:
        duplicate = f"Вы уже подписаны: {origin_name} -> {destination_name}"
        if depart_date:
            duplicate += f" ({depart_date.strftime('%d.%m.%Y')})"
        await message.answer(duplicate)
        return

    text = f"✅ Подписка создана: {origin_name} -> {destination_name}"
    if depart_date:
        text += f"\nДата: {depart_date.strftime('%d.%m.%Y')}"
    else:
        text += "\nПериод: ближайшие 30 дней"

    if max_price:
        text += f"\nЛимит цены: {format_price(max_price)}"

    threshold = min_price_drop or MIN_PRICE_DROP
    text += f"\nМин. снижение для уведомления: {format_price(threshold)}"
    await message.answer(text)


@router.callback_query(F.data.startswith("sub:pause:"))
async def cb_pause_subscription(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    updated = await pause_subscription(sub_id, callback.from_user.id, paused=True)
    if not updated:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    await callback.answer("Подписка на паузе")
    await _send_subscriptions(callback.bot, callback.from_user.id)


@router.callback_query(F.data.startswith("sub:resume:"))
async def cb_resume_subscription(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    updated = await pause_subscription(sub_id, callback.from_user.id, paused=False)
    if not updated:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    await callback.answer("Подписка возобновлена")
    await _send_subscriptions(callback.bot, callback.from_user.id)


@router.callback_query(F.data.startswith("sub:"))
async def cb_add_subscription(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка")
        return

    if parts[1] in {"pause", "resume"}:
        await callback.answer()
        return

    origin = parts[1]
    destination = parts[2]
    if not re.fullmatch(r"[A-Z]{3}", origin) or not re.fullmatch(r"[A-Z]{3}", destination) or origin == destination:
        await callback.answer("Некорректный маршрут", show_alert=True)
        return

    depart_date = None
    return_date = None

    if len(parts) > 3 and parts[3]:
        try:
            depart_date = datetime.strptime(parts[3], "%Y-%m-%d").date()
        except ValueError:
            pass

    if len(parts) > 4 and parts[4]:
        try:
            return_date = datetime.strptime(parts[4], "%Y-%m-%d").date()
        except ValueError:
            pass

    if (len(parts) > 3 and parts[3] and depart_date is None) or (len(parts) > 4 and parts[4] and return_date is None):
        await callback.answer("Некорректная дата", show_alert=True)
        return
    if return_date and (not depart_date or return_date < depart_date):
        await callback.answer("Некорректная дата возвращения", show_alert=True)
        return
    await _ensure_user(callback.from_user)
    added = await add_subscription(
        callback.from_user.id,
        origin,
        destination,
        depart_date=depart_date,
        return_date=return_date,
    )

    origin_name = city_resolver.get_city_name(origin)
    destination_name = city_resolver.get_city_name(destination)

    if added:
        message = f"Подписка добавлена: {origin_name} -> {destination_name}"
        if depart_date:
            message += f" ({depart_date.strftime('%d.%m')})"
        await callback.answer(message)
        return

    await callback.answer("Такая подписка уже есть")


@router.callback_query(F.data.startswith("unsub:"))
async def cb_remove_subscription(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    removed = await remove_subscription(sub_id, callback.from_user.id)
    if not removed:
        await callback.answer("Подписка не найдена")
        return

    await callback.answer("Подписка удалена")

    try:
        await callback.message.edit_text("Подписка удалена ✅")
    except Exception:
        pass

    await _send_subscriptions(callback.bot, callback.from_user.id)


@router.callback_query(F.data == "submgr:open")
async def cb_submgr_open(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _ensure_user(callback.from_user)
    await _show_management(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data == "submgr:back")
async def cb_submgr_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_subscriptions_list(callback.message, callback.from_user.id, page=0, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("submgr:"))
async def cb_submgr_actions(callback: CallbackQuery, state: FSMContext):
    action = (callback.data or "").split(":", 1)[1]

    if action == "open" or action == "back":
        return

    await _ensure_user(callback.from_user)

    if action == "notifications_on":
        await update_user_settings(callback.from_user.id, notifications_enabled=True)
        await _show_management(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Уведомления включены")
        return

    if action == "notifications_off":
        await update_user_settings(callback.from_user.id, notifications_enabled=False)
        await _show_management(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Уведомления выключены")
        return

    if action == "direct_on":
        await update_user_settings(callback.from_user.id, direct_flights_only=True)
        await _show_management(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Фильтр прямых рейсов включен")
        return

    if action == "direct_off":
        await update_user_settings(callback.from_user.id, direct_flights_only=False)
        await _show_management(callback.message, callback.from_user.id, edit=True)
        await callback.answer("Фильтр прямых рейсов выключен")
        return

    if action == "pause_all":
        changed = await pause_all_subscriptions(callback.from_user.id, paused=True)
        await _show_management(callback.message, callback.from_user.id, edit=True)
        await callback.answer(f"На паузу поставлено: {changed}")
        return

    if action == "resume_all":
        changed = await pause_all_subscriptions(callback.from_user.id, paused=False)
        await _show_management(callback.message, callback.from_user.id, edit=True)
        await callback.answer(f"Возобновлено: {changed}")
        return

    if action == "min_drop":
        settings = await get_user_settings(callback.from_user.id)
        current = settings.min_price_drop if settings and settings.min_price_drop else MIN_PRICE_DROP

        text = (
            "<b>📉 Мин. снижение цены</b>\n\n"
            "Уведомление придет, только если цена упадет минимум на эту сумму.\n"
            f"Текущее значение: <b>{format_price(current)}</b>"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_min_drop_keyboard(current))
        await callback.answer()
        return

    if action.startswith("min_drop_set:"):
        try:
            value = int(action.split(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректное значение", show_alert=True)
            return

        await update_user_settings(callback.from_user.id, min_price_drop=value)

        text = (
            "<b>📉 Мин. снижение цены</b>\n\n"
            "Уведомление придет, только если цена упадет минимум на эту сумму.\n"
            f"Текущее значение: <b>{format_price(value)}</b>"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_min_drop_keyboard(value))
        await callback.answer(f"Обновлено: {format_price(value)}")
        return

    if action == "min_drop_custom":
        await state.set_state(ManageSubscriptionsStates.waiting_min_price_drop)
        await callback.answer()
        await callback.message.answer(
            "Введите минимальное снижение цены (от 100 до 200000), например <code>700</code>",
            parse_mode="HTML",
        )
        return

    await callback.answer()


@router.message(ManageSubscriptionsStates.waiting_min_price_drop)
async def process_manage_min_price_drop(message: Message, state: FSMContext):
    value = _parse_min_price_drop_input(message.text or "")

    if value is None:
        await message.answer(
            "Некорректное значение. Введите число от 100 до 200000, например <code>700</code>",
            parse_mode="HTML",
        )
        return

    await _ensure_user(message.from_user)
    await update_user_settings(message.from_user.id, min_price_drop=value)
    await state.clear()

    await message.answer(
        f"✅ Мин. снижение цены обновлено: <b>{format_price(value)}</b>",
        parse_mode="HTML",
    )
    await _show_management(message, message.from_user.id)


@router.callback_query(F.data == "menu:subscriptions")
async def cb_subscriptions(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_subscriptions_list(callback.message, callback.from_user.id, page=0, edit=True)
    await callback.answer()


@router.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message, state: FSMContext):
    await state.clear()
    await _ensure_user(message.from_user)
    await _show_subscriptions_list(message, message.from_user.id, page=0, edit=False)


async def _send_subscriptions(bot, telegram_id: int):
    """Отправляет компактный список подписок (одно сообщение, кнопки-чипы)."""

    class _ChatLike:
        def __init__(self, target_bot, chat_id: int):
            self._bot = target_bot
            self._chat_id = chat_id

        async def answer(self, text: str, **kwargs):
            return await self._bot.send_message(self._chat_id, text, **kwargs)

        async def edit_text(self, *args, **kwargs):
            raise RuntimeError("no message to edit")

    await _show_subscriptions_list(_ChatLike(bot, telegram_id), telegram_id, page=0, edit=False)


@router.callback_query(F.data == "sublist:open")
async def cb_sublist_open(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_subscriptions_list(callback.message, callback.from_user.id, page=0, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("sublist:p:"))
async def cb_sublist_page(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        page = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await _show_subscriptions_list(callback.message, callback.from_user.id, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("subcard:"))
async def cb_subcard(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        sub_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return
    shown = await _show_subscription_card(callback.message, callback.from_user.id, sub_id, edit=True)
    if not shown:
        await callback.answer("Подписка не найдена", show_alert=True)
        await _show_subscriptions_list(callback.message, callback.from_user.id, page=0, edit=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("subact:notif:"))
async def cb_subact_notif(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    sub = await get_subscription(sub_id, callback.from_user.id)
    if not sub:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    new_value = getattr(sub, "notify_enabled", True) is False
    await set_subscription_notifications(sub_id, callback.from_user.id, new_value)
    await _show_subscription_card(callback.message, callback.from_user.id, sub_id, edit=True)
    await callback.answer("Уведомления включены" if new_value else "Уведомления выключены")


@router.callback_query(F.data.startswith("subact:pause:"))
async def cb_subact_pause(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    sub = await get_subscription(sub_id, callback.from_user.id)
    if not sub:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    new_paused = not sub.is_paused
    await pause_subscription(sub_id, callback.from_user.id, paused=new_paused)
    await _show_subscription_card(callback.message, callback.from_user.id, sub_id, edit=True)
    await callback.answer("Слежение на паузе" if new_paused else "Слежение возобновлено")


@router.callback_query(F.data.startswith("subact:delask:"))
async def cb_subact_delask(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    sub = await get_subscription(sub_id, callback.from_user.id)
    if not sub:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    origin_name = city_resolver.get_city_name(sub.origin)
    dest_name = city_resolver.get_city_name(sub.destination)
    text = (
        f"<b>Удалить подписку?</b>\n\n"
        f"{origin_name} → {dest_name}\n"
        "Это остановит слежение и удалит подписку из списка. Маршрут останется в избранном (если был добавлен)."
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=subscription_delete_confirm_keyboard(sub_id),
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=subscription_delete_confirm_keyboard(sub_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("subact:delyes:"))
async def cb_subact_delyes(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    removed = await remove_subscription(sub_id, callback.from_user.id)
    if not removed:
        await callback.answer("Подписка не найдена", show_alert=True)
    else:
        await callback.answer("Подписка удалена")

    await _show_subscriptions_list(callback.message, callback.from_user.id, page=0, edit=True)


@router.callback_query(F.data.startswith("subact:mindrop:"))
async def cb_subact_mindrop(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    sub = await get_subscription(sub_id, callback.from_user.id)
    if not sub:
        await callback.answer("Подписка не найдена", show_alert=True)
        return

    settings = await get_user_settings(callback.from_user.id)
    default_min = settings.min_price_drop if settings and settings.min_price_drop else MIN_PRICE_DROP
    current = sub.min_price_drop

    current_display = format_price(current) if current else f"{format_price(default_min)} (общее)"
    text = (
        "<b>📉 Мин. снижение цены</b>\n\n"
        "Уведомление по этой подписке придёт, только если цена упадёт минимум на эту сумму.\n"
        f"Сейчас: <b>{current_display}</b>"
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_sub_min_drop_keyboard(sub_id, current, default_min),
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("subact:mindropset:"))
async def cb_subact_mindropset(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Ошибка")
        return
    try:
        sub_id = int(parts[2])
        value = int(parts[3])
    except ValueError:
        await callback.answer("Ошибка значения", show_alert=True)
        return

    await update_subscription_min_drop(sub_id, callback.from_user.id, value)
    await _show_subscription_card(callback.message, callback.from_user.id, sub_id, edit=True)
    await callback.answer(f"Обновлено: {format_price(value)}")


@router.callback_query(F.data.startswith("subact:mindropg:"))
async def cb_subact_mindropg(callback: CallbackQuery):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return
    await update_subscription_min_drop(sub_id, callback.from_user.id, None)
    await _show_subscription_card(callback.message, callback.from_user.id, sub_id, edit=True)
    await callback.answer("Используется общее значение")


@router.callback_query(F.data.startswith("subact:mindropcustom:"))
async def cb_subact_mindropcustom(callback: CallbackQuery, state: FSMContext):
    try:
        sub_id = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return

    await state.set_state(ManageSubscriptionsStates.waiting_sub_min_drop)
    await state.update_data(sub_id=sub_id)
    await callback.answer()
    await callback.message.answer(
        "Введите минимальное снижение цены для этой подписки (100..200000), например <code>700</code>",
        parse_mode="HTML",
    )


@router.message(ManageSubscriptionsStates.waiting_sub_min_drop)
async def process_sub_min_drop(message: Message, state: FSMContext):
    value = _parse_min_price_drop_input(message.text or "")
    if value is None:
        await message.answer(
            "Некорректное значение. Введите число от 100 до 200000, например <code>700</code>",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    sub_id = data.get("sub_id")
    await state.clear()

    if not sub_id:
        await message.answer("Подписка не найдена.")
        return

    await update_subscription_min_drop(sub_id, message.from_user.id, value)
    await message.answer(
        f"✅ Мин. снижение для подписки обновлено: <b>{format_price(value)}</b>",
        parse_mode="HTML",
    )
    await _show_subscription_card(message, message.from_user.id, sub_id, edit=False)


TARGET_PRESETS = (5000, 10000, 15000, 25000, 50000)


def _target_price(text: str) -> int | None:
    raw = text.strip().replace(" ", "").replace("\u00a0", "")
    if not re.fullmatch(r"[0-9]{1,8}", raw):
        return None
    value = int(raw)
    return value if 100 <= value <= 10_000_000 else None


def _alert_buttons(*pairs):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data)] for text, data in pairs
    ])


def _target_keyboard(prefix: str, back: str):
    return _alert_buttons(*[(format_price(v), f"{prefix}:{v}") for v in TARGET_PRESETS],
                          ("Без целевой цены", f"{prefix}:none"), ("Назад", back))


def _alert_today():
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _resolve_alert_city(text):
    code = city_resolver.resolve(text.strip())
    return code if code and re.fullmatch(r"[A-Z]{3}", code) else None


@router.callback_query(F.data == "alertnew:start")
async def alert_new_start(callback: CallbackQuery, state: FSMContext):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат с ботом", show_alert=True)
        return
    await callback.answer()
    await _ensure_user(callback.from_user)
    await state.clear()
    await state.set_state(NewAlertStates.origin)
    await callback.message.answer("🔔 <b>Новое оповещение</b>\nОткуда летите? Введите город.",
        reply_markup=_alert_buttons(("Отмена", "sublist:open")))


@router.message(NewAlertStates.origin, F.text)
async def alert_origin(message: Message, state: FSMContext):
    origin = _resolve_alert_city(message.text)
    if not origin:
        await message.answer("Город не найден. Введите другое название.")
        return
    await state.update_data(alert_origin=origin)
    await state.set_state(NewAlertStates.destination)
    await message.answer("Куда летите? Введите город назначения.",
        reply_markup=_alert_buttons(("Отмена", "sublist:open")))


@router.message(NewAlertStates.destination, F.text)
async def alert_destination(message: Message, state: FSMContext):
    destination = _resolve_alert_city(message.text)
    origin = (await state.get_data()).get("alert_origin")
    if not destination or destination == origin:
        await message.answer("Укажите другой город назначения.")
        return
    await state.update_data(alert_destination=destination)
    await state.set_state(NewAlertStates.departure)
    markup = calendar_keyboard("alertdep")
    markup.inline_keyboard.insert(-1, [InlineKeyboardButton(text="Любая дата · 30 дней", callback_data="alertnew:anydate")])
    await message.answer("Выберите дату вылета или ближайшие 30 дней.", reply_markup=markup)


async def _ask_target(message: Message, state: FSMContext):
    await state.set_state(NewAlertStates.target)
    await message.answer("🎯 Целевая цена за билет (₽). Выберите сумму, введите свою или пропустите.",
        reply_markup=_target_keyboard("alertnew:target", "sublist:open"))


@router.callback_query(NewAlertStates.departure, F.data == "alertnew:anydate")
async def alert_any_date(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(alert_departure=None, alert_return=None)
    await _ask_target(callback.message, state)


@router.callback_query(NewAlertStates.departure, F.data.startswith("caldate:alertdep:"))
async def alert_departure(callback: CallbackQuery, state: FSMContext):
    try:
        selected = date.fromisoformat(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная дата", show_alert=True)
        return
    if selected < _alert_today():
        await callback.answer("Дата уже прошла", show_alert=True)
        return
    await callback.answer()
    await state.update_data(alert_departure=selected.isoformat())
    await state.set_state(NewAlertStates.return_day)
    markup = calendar_keyboard("alertret", selected.year, selected.month)
    markup.inline_keyboard.insert(-1, [InlineKeyboardButton(text="В одну сторону", callback_data="alertnew:oneway")])
    await callback.message.answer("Выберите дату возвращения или билет в одну сторону.", reply_markup=markup)


@router.callback_query(NewAlertStates.return_day, F.data == "alertnew:oneway")
async def alert_one_way(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(alert_return=None)
    await _ask_target(callback.message, state)


@router.callback_query(NewAlertStates.return_day, F.data.startswith("caldate:alertret:"))
async def alert_return_date(callback: CallbackQuery, state: FSMContext):
    try:
        selected = date.fromisoformat(callback.data.rsplit(":", 1)[-1])
        departure = date.fromisoformat((await state.get_data()).get("alert_departure", ""))
    except (ValueError, TypeError):
        await callback.answer("Некорректная дата", show_alert=True)
        return
    if selected < departure:
        await callback.answer("Возвращение раньше вылета", show_alert=True)
        return
    await callback.answer()
    await state.update_data(alert_return=selected.isoformat())
    await _ask_target(callback.message, state)


async def _save_new_alert(message: Message, state: FSMContext, user, target):
    data = await state.get_data()
    origin, destination = data.get("alert_origin"), data.get("alert_destination")
    if not origin or not destination:
        await state.clear()
        await message.answer("Поиск устарел.", reply_markup=_subscriptions_header_keyboard())
        return
    departure = date.fromisoformat(data["alert_departure"]) if data.get("alert_departure") else None
    return_day = date.fromisoformat(data["alert_return"]) if data.get("alert_return") else None
    if departure and departure < _alert_today():
        await state.clear()
        await message.answer("Дата уже прошла.", reply_markup=_subscriptions_header_keyboard())
        return
    await _ensure_user(user)
    added = await add_subscription(user.id, origin, destination, max_price=target,
                                   depart_date=departure, return_date=return_day)
    await state.clear()
    settings = await get_user_settings(user.id)
    text = "✅ Оповещение создано. Проверка каждый час." if added else "Такое оповещение уже есть. Измените условия в карточке."
    if not _notifications_enabled(settings):
        text += "\nОбщие оповещения выключены. Включите их в настройках."
    await message.answer(text)
    await _show_subscriptions_list(message, user.id, edit=False)


@router.callback_query(NewAlertStates.target, F.data.startswith("alertnew:target:"))
async def alert_target_button(callback: CallbackQuery, state: FSMContext):
    raw = callback.data.rsplit(":", 1)[-1]
    target = None if raw == "none" else _target_price(raw)
    if raw != "none" and target is None:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    await callback.answer()
    await _save_new_alert(callback.message, state, callback.from_user, target)


@router.message(NewAlertStates.target, F.text)
async def alert_target_text(message: Message, state: FSMContext):
    target = _target_price(message.text)
    if target is None:
        await message.answer("Введите сумму от 100 до 10 000 000 ₽.")
        return
    await _save_new_alert(message, state, message.from_user, target)


@router.callback_query(F.data.regexp(r"^alertlimit:[0-9]+$"))
async def alert_edit_limit(callback: CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split(":")[1])
    if not await get_subscription(sub_id, callback.from_user.id):
        await callback.answer("Оповещение не найдено", show_alert=True)
        return
    await state.clear()
    await state.set_state(ManageSubscriptionsStates.waiting_max_price)
    await state.update_data(alert_sub_id=sub_id)
    await callback.answer()
    await callback.message.answer("🎯 Выберите целевую цену или введите сумму в рублях.",
        reply_markup=_target_keyboard(f"alertlimitset:{sub_id}", f"subcard:{sub_id}"))


@router.callback_query(ManageSubscriptionsStates.waiting_max_price, F.data.startswith("alertlimitset:"))
async def alert_set_limit_button(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    sub_id = int(parts[1])
    if sub_id != (await state.get_data()).get("alert_sub_id"):
        await callback.answer("Откройте настройки заново", show_alert=True)
        return
    raw = parts[2]
    target = None if raw == "none" else _target_price(raw)
    if raw != "none" and target is None:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    ok = await update_subscription_max_price(sub_id, callback.from_user.id, target)
    await state.clear()
    await callback.answer("Сохранено" if ok else "Оповещение не найдено", show_alert=not ok)
    if ok:
        await _show_subscription_card(callback.message, callback.from_user.id, sub_id, edit=True)


@router.message(ManageSubscriptionsStates.waiting_max_price, F.text)
async def alert_set_limit_text(message: Message, state: FSMContext):
    target = _target_price(message.text)
    if target is None:
        await message.answer("Введите сумму от 100 до 10 000 000 ₽.")
        return
    sub_id = (await state.get_data()).get("alert_sub_id")
    await state.clear()
    if sub_id and await update_subscription_max_price(sub_id, message.from_user.id, target):
        await _show_subscription_card(message, message.from_user.id, sub_id, edit=False)
    else:
        await message.answer("Оповещение не найдено.", reply_markup=_subscriptions_header_keyboard())


@router.callback_query(F.data == "alerttest:send")
async def alert_test_delivery(callback: CallbackQuery):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат с ботом", show_alert=True)
        return
    try:
        await callback.bot.send_message(callback.from_user.id,
            "🔔 <b>Тестовое оповещение</b>\nДоставка сообщений работает. Это тест, а не изменение цены.")
    except Exception:
        await callback.answer("Не удалось доставить тестовое сообщение", show_alert=True)
        return
    await callback.answer("Тест отправлен")
