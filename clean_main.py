NEW_MAIN = '''import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from bot.config import BOT_TOKEN
from bot.handlers import common, inline_mode, favorites, history, subscriptions, search, stats, settings
from bot.middlewares.logging_mw import RequestLoggingMiddleware
from bot.services.city_resolver import city_resolver
from bot.services.scheduler import setup_scheduler
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

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # Logging middleware — logs all requests to DB
    dp.message.middleware(RequestLoggingMiddleware())
    dp.callback_query.middleware(RequestLoggingMiddleware())
    dp.inline_query.middleware(RequestLoggingMiddleware())

    dp.include_router(common.router)
    dp.include_router(stats.router)
    dp.include_router(settings.router)
    dp.include_router(inline_mode.router)
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
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "inline_query"])
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
'''

def main():
    ssh = paramiko.SSHClient()
