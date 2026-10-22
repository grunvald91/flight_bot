from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.services import search_prefs
from database import crud
from database.database import _ensure_user_settings_columns
from database.models import Base


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(crud, "async_session_maker", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    await engine.dispose()


# ── Хранение выбора ──

@pytest.mark.asyncio
async def test_unknown_person_has_the_switch_off(db):
    assert await crud.get_search_direct_only(111) is False


@pytest.mark.asyncio
async def test_choice_is_saved_and_read_back(db):
    await crud.set_search_direct_only(111, True, username="artur", first_name="Artur")
    assert await crud.get_search_direct_only(111) is True

    await crud.set_search_direct_only(111, False)
    assert await crud.get_search_direct_only(111) is False


@pytest.mark.asyncio
async def test_saving_creates_the_person_when_missing(db):
    await crud.set_search_direct_only(222, True, username="new_user", first_name="New")

    user = await crud.get_or_create_user(222)
    assert user.username == "new_user" and user.first_name == "New"


@pytest.mark.asyncio
async def test_choice_belongs_to_one_person(db):
    await crud.set_search_direct_only(111, True)

    assert await crud.get_search_direct_only(111) is True
    assert await crud.get_search_direct_only(222) is False


@pytest.mark.asyncio
async def test_search_choice_is_independent_from_the_notifications_filter(db):
    await crud.set_search_direct_only(111, True)
    settings = await crud.get_user_settings(111)
    assert settings.direct_flights_only is False  # уведомления не затронуты

    await crud.update_user_settings(111, direct_flights_only=True)
    await crud.set_search_direct_only(111, False)
    settings = await crud.get_user_settings(111)
    assert settings.direct_flights_only is True and settings.search_direct_only is False


@pytest.mark.asyncio
async def test_saving_the_choice_keeps_other_settings(db):
    await crud.get_or_create_user(111)
    await crud.update_user_settings(111, min_price_drop=700, notifications_enabled=False)

    await crud.set_search_direct_only(111, True)

    settings = await crud.get_user_settings(111)
    assert settings.min_price_drop == 700 and settings.notifications_enabled is False
    assert settings.search_direct_only is True


# ── Слой, который не роняет поиск ──

@pytest.mark.asyncio
async def test_load_returns_off_when_the_database_fails(monkeypatch):
    async def boom(_user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(crud, "get_search_direct_only", boom)

    assert await search_prefs.load_direct_only(1) is False


@pytest.mark.asyncio
async def test_save_reports_failure_instead_of_raising(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(crud, "set_search_direct_only", boom)

    assert await search_prefs.save_direct_only(SimpleNamespace(id=1), True) is False


@pytest.mark.asyncio
async def test_save_passes_the_person_data_through(db):
    user = SimpleNamespace(id=333, username="u333", first_name="Third")

    assert await search_prefs.save_direct_only(user, True) is True
    assert await search_prefs.load_direct_only(333) is True


# ── Миграция существующей базы ──

@pytest.mark.asyncio
async def test_migration_adds_the_column_to_an_old_table_without_losing_data(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        # таблица в старом формате, как на сервере до этой правки
        await conn.execute(text(
            "CREATE TABLE user_settings (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "direct_flights_only BOOLEAN, notifications_enabled BOOLEAN DEFAULT 1, min_price_drop INTEGER)"
        ))
        await conn.execute(text(
            "INSERT INTO user_settings (user_id, direct_flights_only, min_price_drop) VALUES (1, 1, 500)"
        ))

        await _ensure_user_settings_columns(conn)
        await _ensure_user_settings_columns(conn)  # повторный старт бота не должен падать

        row = (await conn.execute(text(
            "SELECT direct_flights_only, min_price_drop, search_direct_only FROM user_settings"
        ))).fetchone()
    await engine.dispose()

    assert tuple(row) == (1, 500, 0)  # старые данные целы, новый выбор по умолчанию выключен
