from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bot.services import scheduler


def sub(**kw):
    return SimpleNamespace(**({"origin":"MOW", "destination":"IST", "depart_date":None,
        "return_date":None, "last_price":1000, "max_price":None} | kw))


def test_limit_crosses_once():
    assert scheduler._should_notify(sub(max_price=900), 900, 500)
    assert not scheduler._should_notify(sub(max_price=900, last_price=900), 900, 500)
    assert scheduler._should_notify(sub(), 500, 500)


@pytest.mark.asyncio
async def test_round_trip_exact_dates(monkeypatch):
    mock = AsyncMock(return_value=[
        {"price":100,"departure_at":"2026-12-01","return_at":"2026-12-05"},
        {"price":200,"departure_at":"2026-12-01","return_at":"2026-12-06"},
        {"price":1,"departure_at":"2026-12-02","return_at":"2026-12-06"},
    ])
    monkeypatch.setattr(scheduler.aviasales_api, "search_prices_for_dates", mock)
    result = await scheduler._subscription_tickets(sub(depart_date=date(2026,12,1),
        return_date=date(2026,12,6)), date(2026,11,24), False)
    assert [row["price"] for row in result] == [200]
    assert mock.call_args.kwargs["one_way"] is False
    assert mock.call_args.kwargs["return_date"] == "2026-12-06"


@pytest.mark.asyncio
async def test_window_crosses_month(monkeypatch):
    mock = AsyncMock(return_value=[
        {"price":100,"departure_at":"2026-11-23"},
        {"price":200,"departure_at":"2026-12-01"},
        {"price":300,"departure_at":"2026-12-24"},
    ])
    monkeypatch.setattr(scheduler.aviasales_api, "search_prices_for_dates", mock)
    rows = await scheduler._subscription_tickets(sub(), date(2026,11,24), False)
    assert {row["price"] for row in rows} == {200}
    assert [c.kwargs["depart_date"] for c in mock.call_args_list] == ["2026-11", "2026-12"]
