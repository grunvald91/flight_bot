import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN", "")
AFFILIATE_MARKER = os.getenv("AFFILIATE_MARKER", "")

DB_TYPE = os.getenv("DB_TYPE", "postgresql")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "flight_bot")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

if DB_TYPE == "sqlite":
    DATABASE_URL = f"sqlite+aiosqlite:///{DB_NAME}.db"
else:
    DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
CACHE_TTL_MINUTES = int(os.getenv("CACHE_TTL_MINUTES", "5"))

TRAVELPAYOUTS_BASE_URL = "https://api.travelpayouts.com"
AVIASALES_LINK = "https://www.aviasales.ru/search"

# Минимальное снижение цены (руб.) для отправки уведомления
MIN_PRICE_DROP = int(os.getenv("MIN_PRICE_DROP", "500"))
