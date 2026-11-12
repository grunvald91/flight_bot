"""Focused destination-pagination tests; isolate optional bot/database dependencies."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    config = ModuleType("bot.config")
    config.TRAVELPAYOUTS_TOKEN = "test-token"
    config.TRAVELPAYOUTS_BASE_URL = "https://example.invalid"
    config.CACHE_TTL_MINUTES = 10
    config.AFFILIATE_MARKER = ""
    database = ModuleType("database.database")
    database.async_session_maker = lambda: None
    models = ModuleType("database.models")
    models.PriceCache = type("PriceCache", (), {})
    sqlalchemy = ModuleType("sqlalchemy")
    sqlalchemy.select = lambda *args: None
    sqlalchemy.delete = lambda *args: None
    for name, module in (("bot", ModuleType("bot")), ("bot.config", config),
                         ("database", ModuleType("database")), ("database.database", database),
                         ("database.models", models), ("sqlalchemy", sqlalchemy)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("destination_pagination_under_test", Path(__file__).parents[1] / "bot/services/aviasales_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AviasalesAPI()


def install_pages(api, pages):
    calls = []

    async def cached(cache_key, endpoint, params):
        page = params["page"]
        calls.append((cache_key, endpoint, dict(params)))
        return pages[page]

    api._cached_request = cached
    return calls


def test_collects_all_pages_until_short_page(api):
    calls = install_pages(api, {
        1: {"success": True, "data": [{"destination": "A"}, {"destination": "B"}]},
        2: {"success": True, "data": [{"destination": "C"}, {"destination": "D"}]},
        3: {"success": True, "data": [{"destination": "E"}]},
    })
    result = asyncio.run(api.search_destinations_on_date("MOW", "2026-10-01", limit=2))
    assert [row["destination"] for row in result] == list("ABCDE")
    assert [params["page"] for _, _, params in calls] == [1, 2, 3]
    assert len({key for key, _, _ in calls}) == 3
    assert all(endpoint == "/aviasales/v3/prices_for_dates" for _, endpoint, _ in calls)
    assert all(params["limit"] == 2 and params["unique"] == "true" for _, _, params in calls)


def test_repeated_full_page_fails_closed(api):
    repeated = {"success": True, "data": [{"destination": "A"}]}
    calls = install_pages(api, {1: repeated, 2: repeated})
    with pytest.raises(RuntimeError, match="[Ii]ncomplete.*page 2"):
        asyncio.run(api.search_destinations_on_date("MOW", "2026-10-01", limit=1))
    assert len(calls) == 2

def test_failed_second_page_never_returns_first_page_only(api):
    install_pages(api, {
        1: {"success": True, "data": [{"destination": "A"}]},
        2: None,
    })
    with pytest.raises(RuntimeError, match="page 2"):
        asyncio.run(api.search_destinations_on_date("MOW", "2026-10-01", limit=1))

def test_exactly_full_last_page_requires_empty_followup(api):
    calls = install_pages(api, {
        1: {"success": True, "data": [{"destination": "A"}]},
        2: {"success": True, "data": []},
    })
    assert asyncio.run(api.search_destinations_on_date("MOW", "2026-10-01", limit=1)) == [{"destination": "A"}]
    assert [params["page"] for _, _, params in calls] == [1, 2]

def test_overlapping_distinct_pages_fail_closed(api):
    install_pages(api, {
        1: {"success": True, "data": [{"destination": "A"}, {"destination": "B"}]},
        2: {"success": True, "data": [{"destination": "B"}, {"destination": "C"}]},
    })
    with pytest.raises(RuntimeError, match="overlap"):
        asyncio.run(api.search_destinations_on_date("MOW", "2026-10-01", limit=2))


@pytest.mark.parametrize("bad_link", [
    "https://example.com/search/MOW1011AYT1",
    "//example.com/search/MOW1011AYT1",
    "/search/../../redirect",
    "/search/MOW1011AYT1#external",
    "/search/MOW1011AYT1\\nHost: evil",
])
def test_rejects_untrusted_provider_link(api, bad_link):
    ticket = {"departure_at": "2026-11-10T08:00:00+03:00", "link": bad_link}
    assert api.build_ticket_link(ticket, "MOW", "AYT") == "https://www.aviasales.ru/search/MOW1011AYT1"

def test_uses_specific_supplier_offer_link(api):
    source = "/search/MOW1011AYT1?t=SU123&search_date=03112026"
    ticket = {"departure_at": "2026-11-10T08:00:00+03:00", "link": source}
    assert api.build_ticket_link(ticket, "MOW", "AYT") == "https://www.aviasales.ru" + source

def test_provider_link_keeps_query_and_encodes_affiliate_marker(api, monkeypatch):
    monkeypatch.setitem(api.build_ticket_link.__func__.__globals__, "AFFILIATE_MARKER", "my marker/1")
    ticket = {"link": "/search/MOW1011AYT1?t=flight123"}
    assert api.build_ticket_link(ticket, "MOW", "AYT") == (
        "https://www.aviasales.ru/search/MOW1011AYT1?t=flight123&marker=my+marker%2F1"
    )

def test_explicit_date_uses_requested_search_instead_of_ticket_deeplink(api):
    ticket = {"link": "/search/MOW1011AYT1?t=old-fare"}
    assert api.build_ticket_link(ticket, "MOW", "AYT", "2026-11-11") == (
        "https://www.aviasales.ru/search/MOW1111AYT1"
    )


@pytest.mark.asyncio
async def test_cheapest_30_day_window_has_no_extra_day_or_fake_nonstop(api):
    """Regression: an unknown transfer count is not a direct flight."""
    from datetime import date, timedelta
    start = date(2026, 11, 12)
    calls = []

    async def fake_prices(**kwargs):
        calls.append(kwargs["depart_date"])
        return [
            {"price": 3000, "departure_at": (start + timedelta(days=0)).isoformat() + "T08:00"},
            {"price": 4000, "departure_at": (start + timedelta(days=29)).isoformat() + "T11:00",
             "number_of_changes": 1},
            {"price": 2000, "departure_at": (start + timedelta(days=30)).isoformat() + "T12:00",
             "transfers": 0},
        ]

    api.search_prices_for_dates = fake_prices
    results = await api.search_cheapest_in_window("AER", "IST", start_date=start, days=30)
    assert {row["departure_at"][:10] for row in results} == {
        start.isoformat(), (start + timedelta(days=29)).isoformat(),
    }
    assert [row["transfers"] for row in results] == [None, 1]
    assert sorted(set(calls)) == ["2026-11", "2026-12"]
