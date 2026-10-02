"""Synthetic callback ownership regression; no Telegram or database calls."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
from bot.handlers import profile


def test_profile_callback_launch_belongs_to_clicking_user(monkeypatch):
    owners = []

    async def launch(owner, purpose):
        owners.append(owner)
        return "synthetic-token"

    monkeypatch.setattr(profile, "create_web_launch", launch)
    monkeypatch.setattr(profile, "has_passenger_profile", AsyncMock(return_value=False))
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=111),  # Bot that authored the callback message.
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=222), message=message, answer=AsyncMock(),
    )
    asyncio.run(profile.cb_profile_open(callback))
    assert owners == [222]


def test_booking_callback_from_group_does_not_create_launch(monkeypatch):
    launch = AsyncMock()
    monkeypatch.setattr(profile, "create_web_launch", launch)
    monkeypatch.setattr(profile.passenger_booking_cache, "get", lambda _key: {
        "checkout_ref": {"product_type": "avia", "passengers_full": 1},
        "offer_summary": {},
    })
    callback = SimpleNamespace(
        data="tutubook:synthetic", from_user=SimpleNamespace(id=222),
        message=SimpleNamespace(chat=SimpleNamespace(type="group"), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    asyncio.run(profile.cb_book_with_profile(callback))
    launch.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
