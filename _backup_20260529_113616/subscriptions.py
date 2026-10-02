import logging
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import MIN_PRICE_DROP
from bot.keyboards.common_kb import subscription_keyboard
from bot.services.city_resolver import city_resolver
from bot.utils.formatters import format_price
from database.crud import (
    add_subscription,
    get_or_create_user,
    get_subscriptions,
    get_user_settings,
    pause_all_subscriptions,
    pause_subscription,
    remove_subscription,
    update_user_settings,
)

logger = logging.getLogger(__name__)

router = Router()

MIN_DROP_PRESETS = (300, 500, 1000, 2000)


class ManageSubscriptionsStates(StatesGroup):
    waiting_min_price_drop = State()


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

    normalized = "".join(ch for ch in text if ch.isdigit())
    if not normalized:
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
            [InlineKeyboardButton(text="⚙️ Управление подписками", callback_data="submgr:open")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:back")],
        ]
    )


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
    await callback.answer()
    await _send_subscriptions(callback.bot, callback.from_user.id)


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
    await callback.answer()
    await _send_subscriptions(callback.bot, callback.from_user.id)


@router.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message, state: FSMContext):
    await state.clear()
    await _send_subscriptions(message.bot, message.from_user.id)


async def _send_subscriptions(bot, telegram_id: int):
    """Отправляет список активных подписок пользователю."""
    subscriptions = await get_subscriptions(telegram_id)

    if not subscriptions:
        await bot.send_message(
            telegram_id,
            "У вас пока нет активных подписок.\n\n"
            "Используйте <code>/subscribe MOW-IST</code>, чтобы создать подписку.",
            parse_mode="HTML",
            reply_markup=_subscriptions_header_keyboard(),
        )
        return

    await bot.send_message(
        telegram_id,
        f"<b>🔔 Активные подписки ({len(subscriptions)})</b>",
        parse_mode="HTML",
        reply_markup=_subscriptions_header_keyboard(),
    )

    for sub in subscriptions:
        origin_name = city_resolver.get_city_name(sub.origin)
        destination_name = city_resolver.get_city_name(sub.destination)
        price = format_price(sub.last_price) if sub.last_price else "еще не проверено"
        threshold = sub.min_price_drop or MIN_PRICE_DROP
        status = "⏸ На паузе" if sub.is_paused else "✅ Активна"

        text = f"{origin_name} -> {destination_name}\n"
        text += f"Статус: {status}\n"

        if sub.depart_date:
            text += f"Дата: {sub.depart_date.strftime('%d.%m.%Y')}\n"
        else:
            text += "Период: ближайшие 30 дней\n"

        text += f"Текущая цена: {price}"

        if sub.max_price:
            text += f"\nЛимит: {format_price(sub.max_price)}"

        text += f"\nМин. снижение: {format_price(threshold)}"

        await bot.send_message(
            telegram_id,
            text,
            parse_mode="HTML",
            reply_markup=subscription_keyboard(
                sub.id,
                sub.origin,
                sub.destination,
                is_paused=sub.is_paused,
            ),
        )
