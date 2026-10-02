import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def modules(monkeypatch):
    def module(name, **items):
        obj = types.ModuleType(name)
        obj.__dict__.update(items)
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    resolver = types.SimpleNamespace(
        get_country_code=lambda code: "RU" if code == "MOW" else "TR",
        get_city_name=lambda code: code,
    )
    source = types.SimpleNamespace(search_destinations_on_date=AsyncMock())
    provider = types.SimpleNamespace(search_avia=AsyncMock())
    cache = types.SimpleNamespace(put=lambda value: "key")
    module("bot", __path__=["bot"])
    module("bot.services", __path__=["bot/services"])
    module("bot.handlers", __path__=["bot/handlers"])
    module("bot.services.aviasales_api", aviasales_api=source)
    module("bot.services.city_resolver", city_resolver=resolver)
    module("bot.services.tutu_mcp", tutu_mcp=provider, tutu_search_cache=cache,
           tutu_checkout_cache=cache, passenger_booking_cache=cache)
    module("bot.handlers.tutu", _booking_summary=lambda x: "", _format_offer=lambda x: "offer")
    module("bot.keyboards", __path__=[])
    module("bot.keyboards.calendar_kb", calendar_keyboard=lambda x: None)
    module("bot.states", __path__=[])
    module("bot.states.search", InternationalDiscoveryStates=types.SimpleNamespace(
        choosing_origin=None, choosing_date=None))
    module("bot.utils", __path__=[])
    module("bot.utils.parsers", _parse_date=lambda x: x)
    for name in ("bot.services.international_discovery", "bot.handlers.discover"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    discovery = importlib.import_module("bot.services.international_discovery")
    handler = importlib.import_module("bot.handlers.discover")
    # Unit tests must never write fabricated offers to the server's archive.
    handler.discovery_archive = types.SimpleNamespace(put=lambda *args: None,
                                                       get=lambda key: None)
    return discovery, handler, source, provider


def rows(n):
    return [{"destination": f"D{i:03d}", "price": 1000 + i} for i in range(n)]


@pytest.mark.asyncio
async def test_all_foreign_candidates_including_beyond_120_are_checked_and_cheapest_wins(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(125)
    async def search(origin, destination, date, **kwargs):
        price = 1 if destination == "D124" else 10000
        return {"offers": [{"offer_id": destination, "price": {"amount": price, "currency": "RUB"}}], "meta": {"total_matched_exact": True}}
    provider.search_avia.side_effect = search
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert provider.search_avia.await_count == 125
    assert len(result) == 125
    assert result[0]["offer_id"] == "D124"
    assert result.unverified == 0


@pytest.mark.asyncio
async def test_failure_is_disclosed_and_does_not_become_no_fare(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(3)
    async def search(origin, destination, date, **kwargs):
        if destination == "D001":
            raise TimeoutError("provider timeout")
        return {"offers": [] if destination == "D002" else
                [{"offer_id": destination, "price": {"amount": 99, "currency": "RUB"}}], "meta": {"total_matched_exact": True}}
    provider.search_avia.side_effect = search
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert [offer["offer_id"] for offer in result] == ["D000"]
    assert result.unverified == 1
    assert result.no_offers == 1
    assert result.checked == 3


@pytest.mark.asyncio
async def test_price_sorted_route_only_requires_first_page(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    async def search(origin, destination, date, **kwargs):
        page = kwargs.get("page", 1)
        return {"offers": [{"offer_id": str(page), "price": {"amount": 50 if page == 1 else 200, "currency": "RUB"}}],
                "meta": {"has_more": page == 1, "total_matched_exact": True}}
    provider.search_avia.side_effect = search
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert result[0]["offer_id"] == "1"
    assert provider.search_avia.await_count == 1
    assert provider.search_avia.await_args.kwargs["page_size"] == 1

@pytest.mark.asyncio
async def test_search_respects_existing_tutu_wrapper_signature(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    async def search(origin, destination, departure_date, page_size, page):
        return {"offers": [{"offer_id": "ok", "price": {"amount": 9, "currency": "RUB"}}],
                "meta": {"total_matched_exact": True}}
    provider.search_avia.side_effect = search
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert result[0]["offer_id"] == "ok"
    assert result.unverified == 0


@pytest.mark.asyncio
async def test_handler_warns_and_shows_every_verified_offer_sorted_after_incomplete_search(modules):
    discovery, handler, source, provider = modules
    source.search_destinations_on_date.return_value = rows(3)
    async def search(origin, destination, date, **kwargs):
        if destination == "D001":
            raise TimeoutError("offline")
        price = 300 if destination == "D000" else 100
        return {"offers": [{"offer_id": destination, "price": {"amount": price, "currency": "RUB"}}], "meta": {"total_matched_exact": True}}
    provider.search_avia.side_effect = search
    cached = []
    handler.tutu_search_cache.put = lambda payload: cached.append(payload) or "key"
    class Message:
        def __init__(self):
            self.sent = []
            self.edits = []
        async def answer(self, text, **kwargs):
            self.sent.append(text)
            return self
        async def edit_text(self, text, **kwargs):
            self.edits.append(text)
    message = Message()
    await handler._run_discovery(message, "MOW", "2026-10-01")
    assert len(message.sent) == 2  # progress plus cheapest verified card
    assert "120" not in message.sent[0]
    assert "до 1000" not in message.sent[0]
    assert "2–5" not in message.sent[0]
    assert "непровер" in message.edits[-1].lower()
    assert "1" in message.edits[-1]
    assert "среди проверенных" in message.edits[-1].lower()
    assert [offer["offer_id"] for offer in cached[0]["offers"]] == ["D002", "D000"]
    assert "1 из 2" in message.sent[-1]
    keyboard = message.edits and handler._discovery_keyboard("key", cached[0]["offers"], 0)
    assert any(button.callback_data == "discpage:key:1" for row in keyboard.inline_keyboard for button in row)

@pytest.mark.asyncio
async def test_capped_route_pool_cannot_certify_cheapest(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    provider.search_avia.return_value = {
        "offers": [{"offer_id": "capped", "price": {"amount": 20, "currency": "RUB"}}],
        "meta": {"total_matched_exact": False, "has_more": False},
    }
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert not result
    assert result.unverified == 1


@pytest.mark.asyncio
async def test_missing_route_completeness_flag_is_unverified(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    provider.search_avia.return_value = {
        "offers": [{"offer_id": "unknown", "price": {"amount": 20, "currency": "RUB"}}], "meta": {},
    }
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert not result
    assert result.unverified == 1


@pytest.mark.asyncio
async def test_non_rub_route_price_is_unverified_without_conversion(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    provider.search_avia.return_value = {
        "offers": [{"offer_id": "usd", "price": {"amount": 100, "currency": "USD"}}],
        "meta": {"total_matched_exact": True},
    }
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert not result
    assert result.unverified == 1


@pytest.mark.asyncio
async def test_non_finite_route_price_is_unverified(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    provider.search_avia.return_value = {
        "offers": [{"offer_id": "invalid", "price": {"amount": float("inf"), "currency": "RUB"}}],
        "meta": {"total_matched_exact": True},
    }
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert not result
    assert result.unverified == 1


@pytest.mark.asyncio
async def test_unresolved_destination_blocks_cheapest_claim(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(2)
    discovery.city_resolver.get_country_code = lambda code: "RU" if code == "MOW" else (None if code == "D001" else "TR")
    provider.search_avia.return_value = {
        "offers": [{"offer_id": "known", "price": {"amount": 9, "currency": "RUB"}}],
        "meta": {"total_matched_exact": True},
    }
    result = await discovery.discover_international_offers("MOW", "2026-10-01")
    assert len(result) == 1
    assert result.unverified == 1
    assert provider.search_avia.await_count == 1


@pytest.mark.asyncio
async def test_empty_result_with_failed_route_is_not_declared_no_fares(modules):
    _, handler, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    provider.search_avia.side_effect = TimeoutError("offline")
    class Message:
        def __init__(self):
            self.sent = []
            self.edits = []
        async def answer(self, text, **kwargs):
            self.sent.append(text)
            return self
        async def edit_text(self, text, **kwargs):
            self.edits.append(text)
    message = Message()
    await handler._run_discovery(message, "MOW", "2026-10-01")
    assert len(message.sent) == 1
    assert "непровер" in message.edits[-1].lower()


@pytest.mark.asyncio
async def test_discovery_navigation_restores_archive_without_stale_booking(modules, tmp_path):
    _, handler, _, _ = modules
    from bot.services.discovery_archive import DiscoveryArchive
    archive = DiscoveryArchive(tmp_path / "archive.sqlite3")
    archive.put("searchkey", {
        "offers": [
            {"price": {"amount": 100, "currency": "RUB"}, "checkout_ref": {"secret": "ONE"},
             "_discovery": {"country": "Турция", "city": "Стамбул"}},
            {"price": {"amount": 200, "currency": "RUB"}, "checkout_ref": {"secret": "TWO"},
             "_discovery": {"country": "Грузия", "city": "Тбилиси"}},
        ], "meta": {"origin": "MOW", "departure_date": "2026-10-01", "unverified": 7},
    })
    handler.discovery_archive = archive
    handler.tutu_search_cache.get = lambda key: None  # process restarted
    handler.tutu_checkout_cache.put = lambda value: pytest.fail("stale checkout minted")
    edits, answers = [], []
    class Callback:
        data = "discpage:searchkey:1"
        message = types.SimpleNamespace(edit_text=AsyncMock(side_effect=lambda text, **kw: edits.append((text, kw))))
        async def answer(self, text=None, **kwargs):
            answers.append(text)
    await handler.discovery_page(Callback())
    assert len(edits) == 1
    assert "2 из 2" in edits[0][0]
    assert "устар" in edits[0][0].lower()
    assert "7 непроверенных" in edits[0][0]
    buttons = [button for row in edits[0][1]["reply_markup"].inline_keyboard for button in row]
    assert not any(button.callback_data and button.callback_data.startswith(("tutubuy:", "tutubook:")) for button in buttons)
    assert not any("устарели" in (answer or "") for answer in answers)


@pytest.mark.asyncio
async def test_progress_transport_failure_does_not_abort_search(modules):
    discovery, _, source, provider = modules
    source.search_destinations_on_date.return_value = rows(1)
    provider.search_avia.return_value = {"offers": [{"offer_id": "ok", "price": {"amount": 5, "currency": "RUB"}}], "meta": {"total_matched_exact": True}}
    async def broken_progress(*args):
        raise TimeoutError("Telegram unavailable")
    result = await discovery.discover_international_offers(
        "MOW", "2026-10-01", progress_callback=broken_progress)
    assert result[0]["offer_id"] == "ok"
