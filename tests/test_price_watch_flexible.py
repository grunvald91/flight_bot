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


def test_alert_entry_buttons():
    from bot.keyboards.common_kb import persistent_menu_keyboard, main_menu_keyboard, subscription_keyboard, subscriptions_list_keyboard
    from bot.handlers.subscriptions import _subscriptions_header_keyboard, _subscription_manage_keyboard
    def callbacks(markup):
        return {button.callback_data for row in markup.inline_keyboard for button in row}
    assert any(b.text == "🔔 Оповещения" for row in persistent_menu_keyboard().keyboard for b in row)
    assert "menu:subscriptions" in callbacks(main_menu_keyboard())
    assert "alertnew:start" in callbacks(_subscriptions_header_keyboard())
    assert "alertnew:start" in callbacks(subscriptions_list_keyboard([(1,"MOW-IST",False,True)],0,8,1))
    assert "alerttest:send" in callbacks(_subscription_manage_keyboard(None))
    assert {"alertlimit:1", "alertcheck:1", "subact:notif:1", "subact:pause:1"} <= callbacks(subscription_keyboard(1))


def test_calendar_options_survive_month_navigation():
    from bot.keyboards.calendar_kb import calendar_keyboard
    for purpose, action in (("alertdep","alertnew:anydate"), ("alertret","alertnew:oneway")):
        markup = calendar_keyboard(purpose, 2027, 1)
        callbacks = {button.callback_data for row in markup.inline_keyboard for button in row}
        assert {action,"sublist:open"} <= callbacks


def test_target_parser():
    from bot.handlers.subscriptions import _target_price
    assert _target_price("15 000") == 15000
    assert _target_price("15\u00a0000") == 15000
    assert _target_price("-15000") is None
    assert _target_price("12.50") is None
    assert _target_price("50000000") is None


@pytest.mark.asyncio
async def test_hourly_and_manual_checks_share_baseline(monkeypatch):
    item = sub(id=7, is_paused=False, last_checked_at=None, min_price_drop=None, notify_enabled=True, last_price=1000)
    monkeypatch.setattr(scheduler, "get_subscription", AsyncMock(return_value=item))
    monkeypatch.setattr(scheduler, "get_user_settings", AsyncMock(return_value=SimpleNamespace(direct_flights_only=False, notifications_enabled=True, min_price_drop=100)))
    monkeypatch.setattr(scheduler, "_subscription_tickets", AsyncMock(return_value=[{"price":800}]))
    send = AsyncMock(return_value=False)
    write = AsyncMock()
    monkeypatch.setattr(scheduler, "notify_price_drop", send)
    monkeypatch.setattr(scheduler, "update_subscription_price", write)
    assert await scheduler.check_subscription(None,7,42,manual=True) == "delivery_failed"
    write.assert_not_awaited()
    send.return_value = True
    assert await scheduler.check_subscription(None,7,42) == "ok"
    write.assert_awaited_once_with(7,800)
    item.last_price = 800
    send.reset_mock()
    assert await scheduler.check_subscription(None,7,42) == "ok"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_alert_button_flow(monkeypatch):
    from bot.handlers import subscriptions as handler
    class State:
        def __init__(self): self.data = {}; self.current = None
        async def clear(self): self.data.clear(); self.current = None
        async def set_state(self, value): self.current = value
        async def update_data(self, **kwargs): self.data.update(kwargs)
        async def get_data(self): return self.data.copy()
    state = State()
    user = SimpleNamespace(id=42, username="test", first_name="Test")
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), from_user=user, text="", answer=AsyncMock())
    callback = SimpleNamespace(message=message, from_user=user, answer=AsyncMock(), data="")
    monkeypatch.setattr(handler, "_ensure_user", AsyncMock())
    monkeypatch.setattr(handler.city_resolver, "resolve", lambda q: {"Москва":"MOW","Стамбул":"IST"}.get(q))
    monkeypatch.setattr(handler, "_alert_today", lambda: date(2026,11,24))
    monkeypatch.setattr(handler, "get_user_settings", AsyncMock(return_value=None))
    monkeypatch.setattr(handler, "_show_subscriptions_list", AsyncMock())
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(handler, "add_subscription", add)
    await handler.alert_new_start(callback,state)
    message.text="Москва"
    await handler.alert_origin(message,state)
    message.text="Стамбул"
    await handler.alert_destination(message,state)
    callback.data="caldate:alertdep:2026-12-01"
    await handler.alert_departure(callback,state)
    callback.data="caldate:alertret:2026-12-06"
    await handler.alert_return_date(callback,state)
    callback.data="alertnew:target:15000"
    await handler.alert_target_button(callback,state)
    add.assert_awaited_once_with(42,"MOW","IST",max_price=15000,depart_date=date(2026,12,1),return_date=date(2026,12,6))
    assert state.current is None
