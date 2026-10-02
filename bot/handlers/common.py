import logging
import os

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from bot.keyboards.common_kb import main_menu_keyboard, persistent_menu_keyboard
from database.database import async_session_maker
from database.models import User
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = Router()


WELCOME_TEXT = (
    "<b>✈️ A_TicketBot — билеты и поездки</b>\n\n"
    "Ищу дешёвые билеты, сравниваю варианты и помогаю быстро понять условия поездки.\n\n"
    "<b>Что умеет бот:</b>\n"
    "⚡ <b>Быстрый</b> — самые дешёвые билеты за границу из выбранного города на конкретную дату. "
    "У каждого направления есть <b>ℹ️ Въезд</b> с краткими правилами для граждан РФ.\n"
    "🌍 <b>Глубокий</b> — более тщательный поиск зарубежных направлений с проверкой вариантов.\n"
    "🟣 <b>Туту</b> — актуальные авиабилеты по конкретному маршруту.\n"
    "🧭 <b>Поездка</b> — сравнение самолёта, поезда, автобуса и электрички, поиск отеля и расчёт поездки туда-обратно.\n"
    "🔍 <b>Найти билеты</b> — обычный поиск по маршруту.\n\n"
    "<b>Ещё:</b> ⭐ избранное · 🔔 слежение за ценой · 📜 история · 👤 профиль пассажира.\n\n"
    "<b>Поиск прямо в любом Telegram-чате:</b>\n"
    "<code>@{bot_username} Москва-Стамбул</code>\n"
    "<code>@{bot_username} Москва-Стамбул 15.10</code>\n\n"
    "Выберите нужную функцию кнопкой ниже 👇"
)
HELP_TEXT = (
    "<b>Справка по боту</b>\n\n"
    "<b>Inline-поиск</b> (в любом чате):\n"
    "   <code>@{bot_username} Москва-Стамбул</code> — поиск по маршруту\n"
    "   <code>@{bot_username} Москва-Стамбул 15.03</code> — на конкретную дату\n"
    "   <code>@{bot_username} Москва-Стамбул 15.03-22.03</code> — туда-обратно\n"
    "   <code>@{bot_username} Москва</code> — дешёвые направления + избранное\n"
    "   <code>@{bot_username} избранные</code> — все избранные маршруты\n\n"
    "<b>Команды:</b>\n"
    "/tutu — поиск актуальных авиабилетов через Туту\n"
    "/travel — сравнение транспорта, отели и поездка целиком\n"
    "/discover — дешёвые международные направления на дату\n"
    "/browse — быстрый обзор направлений и проверка выбранного\n"
    "/profile — защищённый профиль пассажира\n"
    "/guide — полная инструкция по использованию\n"
    "/favorites — избранные маршруты\n"
    "/subscribe — слежение за ценой (подписки)\n"
    "/subscriptions — управление подписками\n"
    "/history — история поиска\n"
    "/settings — общие настройки\n"
    "/help — эта справка"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await _ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    bot_info = await message.bot.get_me()
    text = WELCOME_TEXT.format(bot_username=bot_info.username)

    await message.answer(text, reply_markup=persistent_menu_keyboard())


@router.message(F.text.in_(("🏠 Главное меню", "🏠 Меню")))
async def persistent_home(message: Message, state: FSMContext):
    await state.clear()
    bot_info = await message.bot.get_me()
    await message.answer(
        WELCOME_TEXT.format(bot_username=bot_info.username),
        reply_markup=persistent_menu_keyboard(),
    )


@router.message(F.text == "🔍 Найти билеты")
async def persistent_search(message: Message, state: FSMContext):
    from bot.handlers.search import cmd_search
    await cmd_search(message, state)


@router.message(F.text == "🟣 Поиск на Туту")
async def persistent_tutu(message: Message, state: FSMContext):
    from bot.handlers.tutu import _start_tutu_search
    await _start_tutu_search(message, state)


@router.message(F.text == "🧭 Поездка")
async def persistent_travel(message: Message, state: FSMContext):
    from bot.handlers.travel import cmd_travel
    await cmd_travel(message, state)


@router.message(F.text.in_(("🌍 Куда дешевле за границу", "🌍 Глубокий")))
async def persistent_discover(message: Message, state: FSMContext):
    from bot.handlers.discover import _start_discovery
    await _start_discovery(message, state)

@router.message(F.text.in_(("⚡ Быстрый обзор направлений", "⚡ Быстрый")))
async def persistent_browse(message: Message, state: FSMContext):
    from bot.handlers.browse import _start_browse
    await _start_browse(message, state)


@router.message(F.text == "👤 Пассажир")
async def persistent_passenger_profile(message: Message, state: FSMContext):
    from bot.handlers.profile import _profile_message
    await state.clear()
    await _profile_message(message)


@router.message(F.text == "⭐ Избранное")
async def persistent_favorites(message: Message, state: FSMContext):
    from bot.handlers.favorites import _send_favorites
    await state.clear()
    await _send_favorites(message.bot, message.from_user.id)


@router.message(F.text == "🔔 Слежение")
async def persistent_subscriptions(message: Message, state: FSMContext):
    from bot.handlers.subscriptions import cmd_subscriptions
    await cmd_subscriptions(message, state)


@router.message(F.text == "📜 История")
async def persistent_history(message: Message, state: FSMContext):
    from bot.handlers.history import _show_history
    await state.clear()
    await _show_history(message)


@router.message(F.text == "⚙️ Настройки")
async def persistent_settings(message: Message, state: FSMContext):
    from bot.handlers.settings import cmd_settings
    await cmd_settings(message, state)


@router.message(Command("help"))
async def cmd_help(message: Message):
    bot_info = await message.bot.get_me()
    text = HELP_TEXT.format(bot_username=bot_info.username)
    await message.answer(text)


@router.message(Command("guide"))
async def cmd_guide(message: Message):
    """Отправляет полную инструкцию по использованию бота"""
    guide_parts = [
        (
            "<b>🛫 Инструкция по использованию Flight Bot</b>\n\n"
            "<b>1️⃣ Быстрый поиск по городам</b>\n\n"
            "Введите названия городов <b>на русском</b>:\n"
            "<code>Москва-Стамбул</code>\n"
            "<code>Санкт-Петербург-Прага</code>\n\n"
            "<b>✅ Преимушество:</b> не нужно знать IATA коды!"
        ),
        (
            "<b>2️⃣ Поиск с датами</b>\n\n"
            "Добавьте дату вылета:\n"
            "<code>Москва-Стамбул 15.03</code> → на конкретный день\n"
            "<code>Москва-Стамбул 15.03-22.03</code> → туда-обратно\n\n"
            "<b>Формат:</b> ДД.MM (день и месяц)"
        ),
        (
            "<b>3️⃣ Дешёвые направления из города</b>\n\n"
            "Введите только название города:\n"
            "<code>Москва</code>\n"
            "<code>Берлин</code>\n\n"
            "Бот покажет:\n"
            "• ⭐ Ваши избранные маршруты на ближайшие 3 дня\n"
            "• 💰 Самые дешёвые популярные направления"
        ),
        (
            "<b>4️⃣ Избранные маршруты</b>\n\n"
            "Сохраняйте интересующие маршруты:\n"
            "• Нажмите <b>«В избранное»</b> в результатах поиска\n"
            "• Введите <code>избранные</code> для быстрого поиска\n"
            "• Или используйте /favorites"
        ),
        (
            "<b>🔔 Слежение за ценой (Подписки)</b>\n\n"
            "<b>Подписаться:</b>\n"
            "/subscribe Москва-Стамбул\n\n"
            "<b>С лимитом цены:</b>\n"
            "/subscribe Москва-Стамбул 15000\n\n"
            "Бот проверяет цены <b>каждый час</b> и уведомляет о снижении."
        ),
        (
            "<b>💰 Как работает лимит цены</b>\n\n"
            "<code>/subscribe Москва-Стамбул 15000</code>\n\n"
            "Бот уведомит только если цена ≤ 15000₽"
        ),
        (
            "<b>📋 Все команды</b>\n\n"
            "/start → Главное меню\n"
            "/guide → Полная инструкция (то что вы читаете!)\n"
            "/help → Быстрая справка\n"
            "/subscribe → Мои подписки\n"
            "/favorites → Избранные маршруты\n"
            "/history → История поисков"
        ),
        (
            "<b>🎯 Примеры использования</b>\n\n"
            "<b>Одноразовый поиск:</b>\n"
            "1. Введите в инлайне: \"Москва-Стамбул 15.03\"\n"
            "2. Нажмите на билет\n"
            "3. Нажмите \"Купить на Aviasales\"\n\n"
            "<b>Мониторинг цен:</b>\n"
            "1. /subscribe Москва-Стамбул 15000\n"
            "2. Получайте уведомления каждый час\n"
            "3. Покупайте когда цена упала!"
        ),
        (
            "<b>💡 Советы</b>\n\n"
            "1. <b>Используйте русские названия</b> - это проще, чем IATA коды\n"
            "2. <b>Устанавливайте лимиты цены</b> - не ждите самую низкую цену\n"
            "3. <b>Проверяйте альтернативы</b> - города рядом часто дешевле\n"
            "4. <b>Сохраняйте в избранное</b> - быстрый поиск по популярным маршрутам\n"
            "5. <b>Введите просто город</b> (например, \"Москва\") - вы увидите ваше избранное и дешёвые направления"
        ),
    ]
    
    try:
        for part in guide_parts:
            await message.answer(part, parse_mode="HTML")
            
    except Exception as e:
        logger.error("Failed to send guide: %s", e)
        await message.answer(
            "❌ Ошибка при отправке инструкции.\n\n"
            "Используйте <code>/help</code> для справки."
        )


@router.callback_query(F.data == "menu:back")
async def cb_back_to_menu(callback: CallbackQuery):
    bot_info = await callback.bot.get_me()
    text = WELCOME_TEXT.format(bot_username=bot_info.username)
    await callback.message.edit_text(
        text,
        reply_markup=main_menu_keyboard(user_id=callback.from_user.id),
    )
    await callback.answer()


async def _ensure_user(telegram_id: int, username: str | None, first_name: str | None):
    """Создаёт пользователя в БД если его ещё нет."""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                session.add(User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                ))
                await session.commit()
    except Exception as e:
        logger.error("Failed to ensure user: %s", e)
