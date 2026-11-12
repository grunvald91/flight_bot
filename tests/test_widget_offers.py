"""Feature tests for private Telegram 10.4 Tutu/discovery offer widgets."""
import json
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.services.discovery_archive import DiscoveryArchive
from bot.widgets import offers as ow
from bot.widgets import browse as widgets


def sample(kind="tutu"):
    return {
        "meta": {"kind": kind, "owner_id": 42, "chat_id": 42,
                 "departure_date": "2026-12-23", "origin": "MOW",
                 "unverified": 2 if kind == "discover" else 0},
        "offers": [
            {
                "_source_index": 0,
                "offer_id": "flight-A",
                "price": {"amount": 12000, "currency": "RUB"},
                "_discovery": {"city": "Ереван", "country": "Армения", "country_code": "AM"},
                "departure_at": "2026-12-23T11:20",
                "arrival_at": "2026-12-23T13:20",
                "duration_min": 120,
                "carriers": ["SU"],
                "checkout_ref": {"search_results_url": "https://tutu.ru"},
                "legs": [{"from": {"name": "Москва"}, "to": {"name": "Ереван"},
                          "segments": [{"voyage_no": "SU1"}]}],
                "variants": [{"conditions": {"baggage": {"pieces": 0},
                                              "refundable": False}}],
            },
            {
                "_source_index": 1,
                "offer_id": "flight-B",
                "price": {"amount": 8000, "currency": "RUB"},
                "_discovery": {"city": "Стамбул", "country": "Турция", "country_code": "TR"},
                "departure_at": "2026-12-23T19:20",
                "carriers": ["TK"],
                "checkout_ref": {"search_results_url": "https://tutu.ru"},
                "legs": [{"from": {"name": "Москва"}, "to": {"name": "Стамбул"},
                          "segments": [{"voyage_no": "TK1"}, {"voyage_no": "TK2"}]}],
            },
            {
                "_source_index": 2,
                "offer_id": "flight-C",
                "price": {"amount": 18000, "currency": "RUB"},
                "_discovery": {"city": "Алматы", "country": "Казахстан", "country_code": "KZ"},
                "checkout_ref": {"search_results_url": "https://tutu.ru"},
                "legs": [{"from": {"name": "Москва"}, "to": {"name": "Алматы"},
                          "segments": []}],
            },
        ],
    }


def test_pages_sorting_and_direct_only():
    data = sample()
    page = ow.page_data(data)
    assert page["m"] == "tutu"
    assert page["total"] == 3
    assert [r["i"] for r in page["rows"]] == [1, 0, 2]
    assert [r["s"] for r in page["rows"]] == [1, 0, None]
    direct = ow.page_data(data, 0, True, "price")
    assert direct["total"] == 1
    assert direct["rows"][0]["i"] == 0
    by_stops = ow.page_data(data, 0, False, "stops")
    assert [r["i"] for r in by_stops["rows"]] == [0, 1, 2]


def test_ui_and_payload_under_telegram_limits():
    html = Path("bot/widgets/offers.html").read_bytes()
    compressed = len(zlib.compress(html, 9))
    assert len(html) <= 32768
    assert compressed <= 4096, f"HTML widget compressed to {compressed} bytes; limit 4096"
    assert ow._template().encode() == html
    for kind in ("tutu", "discover"):
        data = sample(kind)
        assert len(ow._encode(ow.page_data(data)).encode("utf-8")) <= 1024
        assert len(ow._encode(ow.detail_data(data["offers"][0], kind)).encode("utf-8")) <= 1024


def test_archive_keeps_country_code_and_type(tmp_path):
    archive = DiscoveryArchive(tmp_path / "offers.sqlite3")
    payload = sample("discover")
    archive.put("offerkey", payload)
    restored = archive.get("offerkey")
    assert restored["meta"]["kind"] == "discover"
    assert restored["offers"][0]["_discovery"]["country_code"] == "AM"
    assert ow.page_data(restored)["m"] == "discover"
    assert ow.detail_data(restored["offers"][0], "discover")["visa"] is True


@pytest.mark.asyncio
async def test_send_to_private_owner_only_and_fallback(monkeypatch):
    monkeypatch.setenv("FLIGHT_BOT_HTML_BUBBLES", "1")
    calls = []

    async def mock_api(bot, method, data):
        calls.append((method, data))
        return {"ok": True, "result": {"message_id": 9, "widget": {"width": 330}}}

    monkeypatch.setattr(ow, "_api", mock_api)
    payload = sample()
    assert await ow.send_offers_widget(SimpleNamespace(token="not-used"), 42, "testkey", payload, "tutu")
    assert calls[0][0] == "sendWidget"
    assert json.loads(calls[0][1]["chat_data"]) == {"k": "testkey"}
    assert calls[0][1]["is_shared"] is False
    assert not await ow.send_offers_widget(SimpleNamespace(token="not-used"), 99, "testkey", payload, "tutu")
    assert len(calls) == 1
    async def no_widget(bot, method, data):
        return {"ok": True, "result": {"message_id": 9}}
    monkeypatch.setattr(ow, "_api", no_widget)
    assert not await ow.send_offers_widget(SimpleNamespace(token="not-used"), 42, "testkey", payload, "tutu")


@pytest.mark.asyncio
async def test_query_authorization_and_actions(monkeypatch):
    monkeypatch.setattr(ow, "discovery_archive", SimpleNamespace(get=lambda key: sample("discover")))
    q = {"from": {"id": 42}, "chat": {"id": 42}}
    result, job = await ow.query_result({**q, "data": "of:testkey:0:1:p"})
    assert result["total"] == 1
    assert job is None
    result, job = await ow.query_result({**q, "data": "od:testkey:0"})
    assert result["detail"] == 1 and result["visa"]
    assert job is None
    result, job = await ow.query_result({**q, "data": "oa:testkey:0"})
    assert result["ok"] == 1
    assert job == (42, "testkey", 0, "discover")
    result, job = await ow.query_result({"from": {"id": 100}, "chat": {"id": 100},
                                          "data": "oa:testkey:0"})
    assert result["ok"] == 0 and job is None
    for action in ["oa:testkey:1000", "oa:testkey:-1", "od:testkey:no",
                   "of:testkey:0:2:p", "of:testkey:0:0:u", "of:testkey:999:0:p"]:
        result, job = await ow.query_result({**q, "data": action})
        assert result["ok"] == 0 and job is None, action


@pytest.mark.asyncio
async def test_action_keyboard_routes_to_existing_handlers(monkeypatch):
    payload = sample("discover")
    monkeypatch.setattr(ow, "discovery_archive", SimpleNamespace(get=lambda key: payload))
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup):
            sent.append((chat_id, text, reply_markup))

    await ow.send_offer_actions(FakeBot(), 42, "testkey", 0, "discover")
    assert len(sent) == 1
    keys = [item.callback_data for row in sent[0][2].inline_keyboard for item in row]
    assert "discbuy:testkey:0" in keys
    assert "discbook:testkey:0" in keys
    await ow.send_offer_actions(FakeBot(), 99, "testkey", 0, "discover")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_tutu_action_matches_price_sorted_callback_index(monkeypatch):
    payload = sample("tutu")
    monkeypatch.setattr(ow, "discovery_archive", SimpleNamespace(get=lambda key: payload))
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup):
            sent.append(reply_markup)

    await ow.send_offer_actions(FakeBot(), 42, "testkey", 0, "tutu")
    callbacks = [item.callback_data for row in sent[0].inline_keyboard for item in row]
    # Flight-A is 2nd among the valid offers sorted by price.
    assert "tutulink:testkey:1:price" in callbacks


@pytest.mark.asyncio
async def test_widget_dispatcher_sends_offer_action_only_after_query_answer(monkeypatch):
    monkeypatch.setattr(widgets, "enabled", lambda: True)
    jobs = []

    async def mock_offer(query):
        return {"ok": 1}, (42, "testkey", 0, "discover")

    async def mock_api(bot, method, params):
        return None

    async def fake_send(*args):
        jobs.append(args)

    monkeypatch.setattr(ow, "query_result", mock_offer)
    monkeypatch.setattr(widgets, "_api", mock_api)
    monkeypatch.setattr(ow, "send_offer_actions", fake_send)
    await widgets.handle_widget_query(SimpleNamespace(), {"id": "x", "data": "oa:testkey:0"})
    assert jobs == []


def test_long_unicode_offer_names_keep_every_flight_on_page():
    payload = sample("discover")
    for offer in payload["offers"]:
        offer["_discovery"] = {"country": "🛫" * 80, "city": "🌍" * 80}
        offer["carriers"] = ["✈️" * 60]
    page = ow.page_data(payload, 0)
    assert len(page["rows"]) == 3
    assert page["total"] == 3
    assert len(widgets._encode(page).encode("utf-8")) <= 1024
