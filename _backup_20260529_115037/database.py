from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from sqlalchemy import text

from bot.config import DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    poolclass=NullPool,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    from database.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_user_settings_columns(conn)
        await _ensure_price_subscriptions_columns(conn)


async def _ensure_user_settings_columns(conn):
    """Lightweight in-place migration for new optional settings fields."""
    dialect = conn.dialect.name

    if dialect == "postgresql":
        await conn.execute(
            text(
                "ALTER TABLE user_settings "
                "ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE"
            )
        )
        return

    if dialect == "sqlite":
        result = await conn.execute(text("PRAGMA table_info(user_settings)"))
        columns = [row[1] for row in result.fetchall()]
        if "notifications_enabled" not in columns:
            await conn.execute(
                text("ALTER TABLE user_settings ADD COLUMN notifications_enabled BOOLEAN DEFAULT 1")
            )


async def _ensure_price_subscriptions_columns(conn):
    """Lightweight in-place migration for optional price subscription fields."""
    dialect = conn.dialect.name

    if dialect == "postgresql":
        await conn.execute(
            text(
                "ALTER TABLE price_subscriptions "
                "ADD COLUMN IF NOT EXISTS is_paused BOOLEAN DEFAULT FALSE"
            )
        )
        return

    if dialect == "sqlite":
        result = await conn.execute(text("PRAGMA table_info(price_subscriptions)"))
        columns = [row[1] for row in result.fetchall()]
        if "is_paused" not in columns:
            await conn.execute(
                text("ALTER TABLE price_subscriptions ADD COLUMN is_paused BOOLEAN DEFAULT 0")
            )


async def get_session() -> AsyncSession:
    async with async_session_maker() as session:
        yield session
