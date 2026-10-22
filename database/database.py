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
        if conn.dialect.name == "postgresql":
            # create_all can create an index on model columns missing from an
            # older table. Migrate existing tables before that index is built.
            if await conn.scalar(text("SELECT to_regclass('passenger_profiles') IS NOT NULL")):
                await _ensure_passenger_profile_columns(conn)
            if await conn.scalar(text("SELECT to_regclass('passenger_web_launches') IS NOT NULL")):
                await _ensure_passenger_web_launch_columns(conn)
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_user_settings_columns(conn)
        await _ensure_price_subscriptions_columns(conn)
        await _ensure_passenger_profile_columns(conn)


async def _ensure_passenger_web_launch_columns(conn):
    columns = {
        "checkout_hash": "VARCHAR(64)",
        "checkout_state": "VARCHAR(24)",
        "snapshot_hash": "VARCHAR(64)",
        "confirmed_profile_version": "INTEGER",
        "confirmation_hash": "VARCHAR(64)",
        "booking_attempted_at": "TIMESTAMP WITHOUT TIME ZONE",
        "booking_status": "VARCHAR(20)",
        "result_encrypted": "BYTEA",
    }
    for name, kind in columns.items():
        await conn.execute(text(f"ALTER TABLE passenger_web_launches ADD COLUMN IF NOT EXISTS {name} {kind}"))


async def _ensure_passenger_profile_columns(conn):
    """Add nullable crypto metadata to older profile tables without rewriting ciphertext."""
    columns = {"profile_id": "VARCHAR(32)", "wrapped_dek": "BYTEA", "kek_id": "VARCHAR(64)"}
    if conn.dialect.name == "postgresql":
        for name, kind in columns.items():
            await conn.execute(text(f"ALTER TABLE passenger_profiles ADD COLUMN IF NOT EXISTS {name} {kind}"))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_passenger_profiles_profile_id "
            "ON passenger_profiles (profile_id) WHERE profile_id IS NOT NULL"
        ))
    elif conn.dialect.name == "sqlite":
        existing = {row[1] for row in (await conn.execute(text("PRAGMA table_info(passenger_profiles)"))).fetchall()}
        for name, kind in {**columns, "wrapped_dek": "BLOB"}.items():
            if name not in existing:
                await conn.execute(text(f"ALTER TABLE passenger_profiles ADD COLUMN {name} {kind}"))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_passenger_profiles_profile_id ON passenger_profiles (profile_id)"
        ))


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
        await conn.execute(
            text(
                "ALTER TABLE user_settings "
                "ADD COLUMN IF NOT EXISTS search_direct_only BOOLEAN NOT NULL DEFAULT FALSE"
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
        if "search_direct_only" not in columns:
            await conn.execute(
                text("ALTER TABLE user_settings ADD COLUMN search_direct_only BOOLEAN NOT NULL DEFAULT 0")
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
        await conn.execute(
            text(
                "ALTER TABLE price_subscriptions "
                "ADD COLUMN IF NOT EXISTS notify_enabled BOOLEAN NOT NULL DEFAULT TRUE"
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
        if "notify_enabled" not in columns:
            await conn.execute(
                text("ALTER TABLE price_subscriptions ADD COLUMN notify_enabled BOOLEAN DEFAULT 1")
            )


async def get_session() -> AsyncSession:
    async with async_session_maker() as session:
        yield session
