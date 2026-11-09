import asyncio
import logging
import os
import sys

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from bot.config import BOT_TOKEN
from bot.handlers import admin, browse, calendar, common, discover, entry, inline_mode, favorites, history, month, profile, subscriptions, search, stats, settings, travel, tutu
from bot.middlewares.logging_mw import RequestLoggingMiddleware
from bot.services.city_resolver import city_resolver
from bot.services.scheduler import setup_scheduler
from bot.widgets.browse import WidgetDispatcher, enabled as widgets_enabled, probe_widget_api
from database.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Starting Flight Bot...")

    await init_db()
    logger.info("Database initialized")

    await city_resolver.load()
    logger.info("City resolver loaded: %d cities", len(city_resolver.cities))

    # In beta, polling and widget methods must use the SAME API environment.
    # This path is opt-in and is never used by the production bot by default.
    session = None
    if widgets_enabled() and os.getenv("FLIGHT_BOT_WIDGET_API") == "beta":
        session = AiohttpSession(api=TelegramAPIServer(
            base="https://api.telegram.org/beta/bot{token}/test/{method}",
            file="https://api.telegram.org/file/bot{token}/test/{path}",
        ))
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        **({"session": session} if session is not None else {}),
    )
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="search", description="Поиск билетов по маршруту"),
            BotCommand(command="tutu", description="Актуальные авиабилеты через Туту"),
            BotCommand(command="travel", description="Транспорт, отели и поездка целиком"),
            BotCommand(command="discover", description="Куда дешевле за границу"),
            BotCommand(command="browse", description="Быстрый обзор цен Aviasales"),
            BotCommand(command="widgettest", description="Проверить HTML-карточку Telegram"),
            BotCommand(command="month", description="Дешёвые билеты на месяц по направлению"),
            BotCommand(command="profile", description="Защищённый профиль пассажира"),
            BotCommand(command="favorites", description="Избранные маршруты"),
            BotCommand(command="subscriptions", description="Слежение за ценой"),
            BotCommand(command="history", description="История поиска"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="help", description="Справка"),
        ])
    except Exception as exc:
        logger.warning("Could not update Telegram command menu: %s", exc)

    # Restore a neutral profile description; remove a promotional description
    # that a previous deployment may have left persisted in Telegram.
    try:
        await bot.set_my_short_description(
            short_description="Поиск авиабилетов и маршрутов, сравнение транспорта и отслеживание цен."
        )
    except Exception as exc:
        logger.warning("Could not restore Telegram short description: %s", exc)

    dp = WidgetDispatcher()

    # Logging middleware — logs all requests to DB
    dp.message.middleware(RequestLoggingMiddleware())
    dp.callback_query.middleware(RequestLoggingMiddleware())
    dp.inline_query.middleware(RequestLoggingMiddleware())

    dp.include_router(common.router)
    dp.include_router(calendar.router)
    dp.include_router(admin.router)
    dp.include_router(stats.router)
    dp.include_router(settings.router)
    dp.include_router(profile.router)
    dp.include_router(inline_mode.router)
    dp.include_router(tutu.router)
    dp.include_router(travel.router)
    dp.include_router(discover.router)
    dp.include_router(entry.router)
    dp.include_router(browse.router)
    dp.include_router(month.router)
    dp.include_router(search.router)
    dp.include_router(favorites.router)
    dp.include_router(history.router)
    dp.include_router(subscriptions.router)

    scheduler = await setup_scheduler(bot)
    scheduler.start()
    logger.info("Scheduler started")

    try:
        bot_info = await bot.get_me()
        logger.info("Bot started: @%s", bot_info.username)
        if widgets_enabled():
            # Check the actual Bot API method, not the Telegram client version.
            # The call has no chat_id and therefore cannot deliver a message.
            logger.info("Widget API probe: %s", await probe_widget_api(bot))
        updates = ["message", "channel_post", "callback_query", "inline_query"]
        if widgets_enabled():
            updates.append("widget_query")
        await dp.start_polling(bot, allowed_updates=updates)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
