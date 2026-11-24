from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bot.services import smart_alerts as service
from bot.handlers import smart_alerts as handler
from bot.keyboards.common_kb import subscriptions_list_keyboard
from bot.handlers.subscriptions import _subscriptions_header_keyboard


def test_filters_exact_roundtrip_and_international():
    country = {"LED":"RU","IST":"TR","EVN":"AM"}
    def row(dest, price, ret="2026-12-06", **extra):
        return {"destination":dest,"price":price,"departure_at":"2026-12-01","return_at":ret,**extra}
    rows = [row("LED",1),row("IST",2,ret="2026-12-07"),row("EVN",300),row("IST",200),
            row("EVN",float("nan")),row("IST",-10),row("EVN",True)]
    fares = service.choose_fares(rows,"RU","2026-12-01","2026-12-06",country.get,False)
    assert [f["price"] for f in fares] == [300,200]


def test_direct_roundtrip_requires_both_legs():
    common = {"destination":"IST","departure_at":"2026-12-01","return_at":"2026-12-06","price":9000,"transfers":0}
    fares = service.choose_fares([common,{**common,"return_transfers":1},
        {**common,"return_transfers":0}], "RU","2026-12-01","2026-12-06",lambda _: "TR",True)
    assert len(fares) == 1


def test_dedup_and_new_lower_price():
    offer = {"destination":"IST","departure":"2026-12-01","returning":"2026-12-06","price":9000}
    sent = {service.offer_key(offer):9000}
    assert not service.next_signal(offer,sent,500)
    assert not service.next_signal({**offer,"price":8800},sent,500)
    assert service.next_signal({**offer,"price":8500},sent,500)
    assert service.next_signal({**offer,"destination":"EVN"},sent,500)


@pytest.mark.asyncio
async def test_scan_uses_exact_dates_and_reports_partial(monkeypatch):
    monkeypatch.setattr(service.city_resolver,"get_country_code",lambda code: {"AER":"RU","IST":"TR"}.get(code))
    async def fetch(origin, departure, **kwargs):
        if departure == "2026-12-02":
            raise RuntimeError("source error")
        return [{"destination":"IST","price":7000,"departure_at":departure,"return_at":kwargs["return_date"]}]
    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(service.aviasales_api,"search_destinations_on_date",mock)
    fares, failed = await service.scan_group(("AER",5,7,False),date(2026,12,1))
    assert failed == 1 and len(fares) == 6
    assert fares[0]["returning"] == "2026-12-06"
    assert mock.await_count == 7


def test_smart_button_in_both_alert_menus():
    for markup in [_subscriptions_header_keyboard(),subscriptions_list_keyboard([(1,"MOW-IST",False,True)],0,8,1)]:
        assert "smart:list" in {b.callback_data for row in markup.inline_keyboard for b in row}


@pytest.mark.asyncio
async def test_smart_setup_saves_user_selected_conditions(monkeypatch):
    class State:
        def __init__(self): self.data = {}; self.current = None
        async def clear(self): self.data.clear(); self.current = None
        async def set_state(self, value): self.current = value
        async def update_data(self, **kw): self.data.update(kw)
        async def get_data(self): return self.data.copy()
    state = State()
    user = SimpleNamespace(id=42,username="test",first_name="Test")
    message = SimpleNamespace(from_user=user,text="Сочи",answer=AsyncMock())
    callback = SimpleNamespace(message=message,from_user=user,data="",answer=AsyncMock())
    monkeypatch.setattr(handler.city_resolver,"resolve",lambda _: "AER")
    monkeypatch.setattr(handler.city_resolver,"get_country_code",lambda _: "RU")
    monkeypatch.setattr(handler,"get_or_create_user",AsyncMock())
    monkeypatch.setattr(handler,"show_list",AsyncMock())
    create = AsyncMock(return_value="ok")
    monkeypatch.setattr(service,"create_rule",create)
    await handler.start(callback,state)
    await handler.origin(message,state)
    callback.data="smart:nights:5"
    await handler.nights(callback,state)
    callback.data="smart:horizon:30"
    await handler.horizon(callback,state)
    callback.data="smart:direct:1"
    await handler.direct(callback,state)
    callback.data="smart:budget:15000"
    await handler.budget_button(callback,state)
    create.assert_awaited_once_with(42,"AER",5,30,15000,True)
    assert state.current is None


@pytest.mark.asyncio
async def test_scheduler_registers_smart_hourly():
    from bot.services.scheduler import setup_scheduler
    sched = await setup_scheduler(object())
    assert sched.get_job("check_prices") is not None
    job = sched.get_job("smart_travel_alerts")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 3600


@pytest.mark.asyncio
async def test_db_persistence_and_owner_isolation(monkeypatch,tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import select
    from database.models import Base, User, SmartTravelAlert
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "alerts.db"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service,"async_session_maker",factory)
    monkeypatch.setattr(service.city_resolver,"get_country_code",lambda _: "RU")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all([User(telegram_id=42),User(telegram_id=43)])
            await session.commit()
        assert await service.create_rule(42,"AER",5,30,15000,False) == "ok"
        assert await service.create_rule(42,"AER",5,30,15000,False) == "duplicate"
        rule, = await service.list_rules(42)
        assert await service.list_rules(43) == []
        assert await service.get_rule(43,rule.id) is None
        assert not await service.change_rule(43,rule.id,remove=True)
        assert await service.pause_all_rules(42,True) == 1
        assert (await service.get_rule(42,rule.id)).paused
        assert await service.change_rule(42,rule.id,remove=True)
        assert await service.list_rules(42) == []
    finally:
        await engine.dispose()
