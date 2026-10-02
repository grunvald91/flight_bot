import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import MIN_PRICE_DROP
from bot.utils.formatters import format_price
from database.crud import get_or_create_user, get_user_settings, update_user_settings

logger = logging.getLogger(__name__)

router = Router()

MIN_DROP_PRESETS = (300, 500, 1000, 2000)


class SettingsStates(StatesGroup):
    waiting_min_price_drop = State()


def parse_min_price_drop_input(text: str) -> int | None:
    if not text:
        return None

    normalized = "".join(ch for ch in text if ch.isdigit())
    if not normalized.isdigit():
        return None

    value = int(normalized)
    if value < 100 or value > 200000:
        return None

    return value


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✈️ Только прямые рейсы", callback_data="settings:direct_flights")],
            [InlineKeyboardButton(text="🔔 Уведомления о ценах", callback_data="settings:notifications")],
            [InlineKeyboardButton(text="📉 Мин. снижение цены", callback_data="settings:min_price_drop")],
            [InlineKeyboardButton(text="⚙️ Управление подписками", callback_data="submgr:open")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="settings:close")],
        ]
    )


def _direct_flights_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "Выключить фильтр прямых" if enabled else "Включить фильтр прямых"
    toggle_data = "settings:direct_off" if enabled else "settings:direct_on"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")],
        ]
    )


def _notifications_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "Выключить уведомления" if enabled else "Включить уведомления"
    toggle_data = "settings:notifications_off" if enabled else "settings:notifications_on"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")],
        ]
    )


def _min_price_drop_keyboard(current_value: int) -> InlineKeyboardMarkup:
    rows = []

    for value in MIN_DROP_PRESETS:
        mark = "✅ " if value == current_value else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{format_price(value)}",
                    callback_data=f"settings:min_drop_set:{value}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="settings:min_drop_custom")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ensure_user(message_user) -> None:
    await get_or_create_user(
        message_user.id,
        username=message_user.username,
        first_name=message_user.first_name,
    )


def _notifications_enabled(settings) -> bool:
    return not settings or settings.notifications_enabled is not False


async def _build_settings_text(telegram_id: int) -> str:
    settings = await get_user_settings(telegram_id)

    direct_enabled = bool(settings and settings.direct_flights_only)
    notifications_enabled = _notifications_enabled(settings)
    min_drop_value = settings.min_price_drop if settings and settings.min_price_drop else MIN_PRICE_DROP

    return (
        "<b>⚙️ Настройки уведомлений</b>\n\n"
        f"Только прямые рейсы: <b>{'ВКЛ' if direct_enabled else 'ВЫКЛ'}</b>\n"
        f"Уведомления о ценах: <b>{'ВКЛ' if notifications_enabled else 'ВЫКЛ'}</b>\n"
        f"Мин. снижение цены: <b>{format_price(min_drop_value)}</b>\n\n"
        "Выберите действие:" 
    )


async def _show_settings(target_message: Message, telegram_id: int, edit: bool = False):
    text = await _build_settings_text(telegram_id)

    if edit:
        try:
            await target_message.edit_text(text, reply_markup=_settings_keyboard(), parse_mode="HTML")
            return
        except Exception:
            pass

    await target_message.answer(text, reply_markup=_settings_keyboard(), parse_mode="HTML")


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    await state.clear()
    await _ensure_user(message.from_user)
    await _show_settings(message, message.from_user.id)


@router.callback_query(F.data == "menu:settings")
async def cb_menu_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _ensure_user(callback.from_user)
    await _show_settings(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


@router.message(Command("direct"))
async def cmd_direct(message: Message):
    args = (message.text or "").split()
    await _ensure_user(message.from_user)

    if len(args) < 2:
        settings = await get_user_settings(message.from_user.id)
        status = "ВКЛ" if settings and settings.direct_flights_only else "ВЫКЛ"
        await message.answer(
            "<b>Фильтр прямых рейсов</b>\n\n"
            f"Сейчас: <b>{status}</b>\n\n"
            "Команды:\n"
            "<code>/direct on</code>\n"
            "<code>/direct off</code>\n\n"
            "Или используйте <code>/settings</code> для управления кнопками.",
            parse_mode="HTML",
        )
        return

    action = args[1].lower()
    if action == "on":
        await update_user_settings(message.from_user.id, direct_flights_only=True)
        await message.answer("✅ Фильтр прямых рейсов <b>включен</b>.", parse_mode="HTML")
        return

    if action == "off":
        await update_user_settings(message.from_user.id, direct_flights_only=False)
        await message.answer("✅ Фильтр прямых рейсов <b>выключен</b>.", parse_mode="HTML")
        return

    await message.answer(
        "Некорректный аргумент. Используйте <code>/direct on</code> или <code>/direct off</code>.",
        parse_mode="HTML",
    )


@router.message(Command("notifications"))
async def cmd_notifications(message: Message):
    args = (message.text or "").split()
    await _ensure_user(message.from_user)

    if len(args) < 2:
        settings = await get_user_settings(message.from_user.id)
        status = "ВКЛ" if _notifications_enabled(settings) else "ВЫКЛ"
        await message.answer(
            "<b>Уведомления о ценах</b>\n\n"
            f"Сейчас: <b>{status}</b>\n\n"
            "Команды:\n"
            "<code>/notifications on</code>\n"
            "<code>/notifications off</code>\n\n"
            "Или используйте <code>/settings</code> для управления кнопками.",
            parse_mode="HTML",
        )
        return

    action = args[1].lower()
    if action == "on":
        await update_user_settings(message.from_user.id, notifications_enabled=True)
        await message.answer("✅ Уведомления о ценах <b>включены</b>.", parse_mode="HTML")
        return

    if action == "off":
        await update_user_settings(message.from_user.id, notifications_enabled=False)
        await message.answer("✅ Уведомления о ценах <b>выключены</b>.", parse_mode="HTML")
        return

    await message.answer(
        "Некорректный аргумент. Используйте <code>/notifications on</code> или <code>/notifications off</code>.",
        parse_mode="HTML",
    )


@router.message(SettingsStates.waiting_min_price_drop)
async def process_min_price_drop_input(message: Message, state: FSMContext):
    value = parse_min_price_drop_input(message.text or "")

    if value is None:
        await message.answer(
            "Введите число от 100 до 200000, например <code>700</code>.",
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
    await _show_settings(message, message.from_user.id)


@router.callback_query(F.data.startswith("settings:"))
async def callback_settings(callback: CallbackQuery, state: FSMContext):
    action = (callback.data or "").split(":", 1)[1]

    if action == "close":
        await state.clear()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        return

    if action == "main":
        await state.clear()
        await _show_settings(callback.message, callback.from_user.id, edit=True)
        await callback.answer()
        return

    if action == "direct_flights":
        await _ensure_user(callback.from_user)
        settings = await get_user_settings(callback.from_user.id)
        enabled = bool(settings and settings.direct_flights_only)

        text = (
            "<b>✈️ Только прямые рейсы</b>\n\n"
            f"Текущее состояние: <b>{'ВКЛ' if enabled else 'ВЫКЛ'}</b>\n\n"
            "Если фильтр включен, уведомления приходят только по прямым рейсам."
        )

        await callback.message.edit_text(
            text,
            reply_markup=_direct_flights_keyboard(enabled),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if action in ["direct_on", "direct_off"]:
        await _ensure_user(callback.from_user)
        enable = action == "direct_on"
        await update_user_settings(callback.from_user.id, direct_flights_only=enable)

        text = (
            "<b>✈️ Только прямые рейсы</b>\n\n"
            f"Текущее состояние: <b>{'ВКЛ' if enable else 'ВЫКЛ'}</b>\n\n"
            "Если фильтр включен, уведомления приходят только по прямым рейсам."
        )

        await callback.message.edit_text(
            text,
            reply_markup=_direct_flights_keyboard(enable),
            parse_mode="HTML",
        )
        await callback.answer(f"Фильтр: {'ВКЛ' if enable else 'ВЫКЛ'}")
        return

    if action == "notifications":
        await _ensure_user(callback.from_user)
        settings = await get_user_settings(callback.from_user.id)
        enabled = _notifications_enabled(settings)

        text = (
            "<b>🔔 Уведомления о ценах</b>\n\n"
            f"Текущее состояние: <b>{'ВКЛ' if enabled else 'ВЫКЛ'}</b>\n\n"
            "При выключении уведомления не отправляются, но проверки цен продолжаются."
        )

        await callback.message.edit_text(
            text,
            reply_markup=_notifications_keyboard(enabled),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if action in ["notifications_on", "notifications_off"]:
        await _ensure_user(callback.from_user)
        enable = action == "notifications_on"
        await update_user_settings(callback.from_user.id, notifications_enabled=enable)

        text = (
            "<b>🔔 Уведомления о ценах</b>\n\n"
            f"Текущее состояние: <b>{'ВКЛ' if enable else 'ВЫКЛ'}</b>\n\n"
            "При выключении уведомления не отправляются, но проверки цен продолжаются."
        )

        await callback.message.edit_text(
            text,
            reply_markup=_notifications_keyboard(enable),
            parse_mode="HTML",
        )
        await callback.answer(f"Уведомления: {'ВКЛ' if enable else 'ВЫКЛ'}")
        return

    if action == "min_price_drop":
        await _ensure_user(callback.from_user)
        settings = await get_user_settings(callback.from_user.id)
        current_value = settings.min_price_drop if settings and settings.min_price_drop else MIN_PRICE_DROP

        text = (
            "<b>📉 Мин. снижение цены</b>\n\n"
            "Уведомление приходит, если цена упала минимум на выбранную сумму.\n"
            f"Текущее значение: <b>{format_price(current_value)}</b>"
        )

        await callback.message.edit_text(
            text,
            reply_markup=_min_price_drop_keyboard(current_value),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if action.startswith("min_drop_set:"):
        try:
            value = int(action.split(":", 1)[1])
        except ValueError:
            await callback.answer("Ошибка значения", show_alert=True)
            return

        await _ensure_user(callback.from_user)
        await update_user_settings(callback.from_user.id, min_price_drop=value)

        text = (
            "<b>📉 Мин. снижение цены</b>\n\n"
            "Уведомление приходит, если цена упала минимум на выбранную сумму.\n"
            f"Текущее значение: <b>{format_price(value)}</b>"
        )

        await callback.message.edit_text(
            text,
            reply_markup=_min_price_drop_keyboard(value),
            parse_mode="HTML",
        )
        await callback.answer(f"Обновлено: {format_price(value)}")
        return

    if action == "min_drop_custom":
        await state.set_state(SettingsStates.waiting_min_price_drop)
        await callback.answer()
        await callback.message.answer(
            "Введите минимальное снижение цены (100..200000), например <code>700</code>",
            parse_mode="HTML",
        )
        return

    await callback.answer()
