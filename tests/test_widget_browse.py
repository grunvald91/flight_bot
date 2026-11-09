"""Tests for gated Telegram 10.4 browse widget. No Telegram network required."""
import json
import zlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from bot.services.discovery_archive import DiscoveryArchive
from bot.widgets import browse as widgets


def future_date():
    return (date.today() + timedelta(days=30)).isoformat()


def example():
    return {
        "options": [
            {"origin": "MOW", "destination": "IST", "city": "Стамбул",
             "country": "Турция", "country_code": "TR", "price": 8000,
             "transfers": 1, "direct": {"price": 11000, "transfers": 0}},
            {"origin": "MOW", "destination": "EVN", "city": "Ереван",
             "country": "Армения", "country_code": "AM", "price": 9000,
             "transfers": 0},
        ],
        "meta": {"owner_id": 42, "chat_id": 42, "origin": "MOW",
                 "departure_date": future_date(), "country_code": None,
                 "direct_checked": True},
    }


def test_archive_keeps_alternative_direct_price_and_country_filter(tmp_path):
    db = DiscoveryArchive(tmp_path / "archive.db")
    source = example()
    db.put_browse("test-key", source)
    restored = db.get("test-key")
    assert restored["options"][0]["direct"]["price"] == 11000
    assert restored["options"][0]["direct"]["transfers"] == 0
    assert restored["meta"]["direct_checked"] is True
    page = widgets.page_data(restored, 0, True)
    assert [row["c"] for row in page["rows"]] == ["Ереван", "Стамбул"]
    assert [row["p"] for row in page["rows"]] == [9000, 11000]
    assert all(row["t"] == 0 for row in page["rows"])


def test_price_and_transfer_unknown_do_not_become_direct():
    source = example()
    source["options"][0]["transfers"] = None
    source["options"][0].pop("direct")
    source["options"][1]["transfers"] = None
    p = widgets.page_data(source, 0, True)
    assert p["total"] == 0
    assert p["rows"] == []


def test_page_data_under_tg_1024_bytes_limit():
    source = example()
    source["options"] = [
        {"origin": "MOW", "destination": "IST", "city": "Очень длинное наименование города" * 3,
         "country": "Очень длинное наименование государства" * 2,
         "price": 12345, "transfers": 0} for _ in range(21)
    ]
    page = widgets.page_data(source, 2, False)
    assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 1024
    assert page["pages"] == 6
    assert len(page["rows"]) == widgets.PAGE_SIZE


@pytest.mark.parametrize("page", [-1, 5, True, 1000])
def test_out_of_bounds_page_rejected(page):
    with pytest.raises(ValueError):
        widgets.page_data(example(), page, False)


def test_html_fits_telegram_uncompressed_and_zlib_limits():
    html = widgets._template().encode("utf-8")
    assert len(html) <= 32768
    assert len(zlib.compress(html, 9)) <= 4096
    assert b"TelegramWidget.request" not in html or b"W.request" in html


@pytest.mark.asyncio
async def test_disabled_widget_never_sends(monkeypatch):
    monkeypatch.delenv("FLIGHT_BOT_HTML_BUBBLES", raising=False)
    assert await widgets.send_browse_widget(SimpleNamespace(token="unused"), 42, "key", example(), False) is False


@pytest.mark.asyncio
async def test_send_has_initial_data_and_privacy_scope(monkeypatch):
    monkeypatch.setenv("FLIGHT_BOT_HTML_BUBBLES", "1")
    requests = []
    async def fake_api(bot, method, params):
        requests.append((method, params))
        return {"ok": True, "result": {"message_id": 120, "widget": {"width": 340, "height": 460}}}
    monkeypatch.setattr(widgets, "_api", fake_api)
    assert await widgets.send_browse_widget(SimpleNamespace(token="no-network"), 42, "key", example(), False)
    method, params = requests[0]
    assert method == "sendWidget"
    assert params["allow_queries"] is True
    assert params["is_shared"] is False
    assert json.loads(params["chat_data"]) == {"k": "key"}
    assert json.loads(params["public_data"])["rows"][0]["c"] == "Стамбул"
    assert not await widgets.send_browse_widget(SimpleNamespace(token="no-network"), 99, "key", example(), False)


@pytest.mark.asyncio
async def test_query_cannot_open_other_users_search(monkeypatch):
    class FakeArchive:
        def get(self, key):
            return example()
    monkeypatch.setattr(widgets, "discovery_archive", FakeArchive())
    query = {"from": {"id": 99}, "chat": {"id": 99}, "data": "b:key:0:0"}
    result, task = await widgets._query_result(query, None)
    assert result["ok"] == 0
    assert task is None


@pytest.mark.asyncio
async def test_invalid_and_out_of_bounds_widget_queries_fail_closed(monkeypatch):
    monkeypatch.setattr(widgets, "discovery_archive", SimpleNamespace(get=lambda key: example()))
    base = {"from": {"id": 42}, "chat": {"id": 42}}
    for value in ["b:key:1:0", "b:key:0:2", "c:key:99:0", "v:key:-1", "b:key:999999:1"]:
        result, task = await widgets._query_result({**base, "data": value}, None)
        assert result["ok"] == 0, value
        assert task is None


@pytest.mark.asyncio
async def test_valid_verification_job_only_for_owner(monkeypatch):
    monkeypatch.setattr(widgets, "discovery_archive", SimpleNamespace(get=lambda key: example()))
    base = {"from": {"id": 42}, "chat": {"id": 42}, "data": "c:key:0:1"}
    result, job = await widgets._query_result(base, None)
    assert result["ok"] == 1
    assert job == (42, 42, "MOW", "IST", future_date())


@pytest.mark.asyncio
async def test_answer_query_does_not_schedule_job_if_answer_failed(monkeypatch):
    monkeypatch.setattr(widgets, "_query_result", lambda *a: None)
    async def answer(q, bot):
        return {"ok": 1, "message": "OK"}, ("fake-job",)
    monkeypatch.setattr(widgets, "_query_result", answer)
    monkeypatch.setattr(widgets, "_api", lambda *args: _no_result())
    triggered = []
    monkeypatch.setattr(widgets, "_check_ticket", lambda *args: triggered.append(args))
    await widgets.handle_widget_query(None, {"id": "test"})
    assert triggered == []


async def _no_result():
    return None


@pytest.mark.asyncio
async def test_filter_toggle_persists_setting_but_paging_does_not(monkeypatch):
    from bot.services import search_prefs

    calls = []

    async def save(user, value):
        calls.append((user.id, value))
        return True

    monkeypatch.setattr(search_prefs, "save_direct_only", save)
    monkeypatch.setattr(widgets, "discovery_archive", SimpleNamespace(get=lambda key: example()))
    q = {"from": {"id": 42, "first_name": "Test"}, "chat": {"id": 42}}

    toggled, job = await widgets._query_result({**q, "data": "t:key:0:1"}, None)
    assert toggled["ok"] == 1
    assert toggled["d"] == 1
    assert job is None
    assert calls == [(42, True)]

    paged, job = await widgets._query_result({**q, "data": "b:key:0:0"}, None)
    assert paged["ok"] == 1
    assert calls == [(42, True)], "Paging must not write user preferences"

    invalid, _ = await widgets._query_result({**q, "data": "t:key:1:1"}, None)
    assert invalid["ok"] == 0


@pytest.mark.asyncio
async def test_filter_toggle_handles_setting_store_failure(monkeypatch):
    from bot.services import search_prefs

    async def fail_save(user, value):
        return False

    monkeypatch.setattr(search_prefs, "save_direct_only", fail_save)
    monkeypatch.setattr(widgets, "discovery_archive", SimpleNamespace(get=lambda key: example()))
    q = {"from": {"id": 42}, "chat": {"id": 42}, "data": "t:key:0:1"}
    answer, job = await widgets._query_result(q, None)
    assert answer["ok"] == 1
    assert answer["preference_saved"] is False
    assert job is None


@pytest.mark.asyncio
async def test_stale_widget_does_not_search_past_departure(monkeypatch):
    payload = example()
    payload["meta"]["departure_date"] = "2000-01-01"
    monkeypatch.setattr(widgets, "discovery_archive", SimpleNamespace(get=lambda key: payload))
    q = {"from": {"id": 42}, "chat": {"id": 42}, "data": "c:key:0:0"}
    answer, job = await widgets._query_result(q, None)
    assert answer["ok"] == 0
    assert "прошла" in answer["error"]
    assert job is None


@pytest.mark.asyncio
async def test_unknown_aiogram_update_field_is_handled(monkeypatch):
    from aiogram.types import Update

    calls = []

    async def handler(bot, query):
        calls.append(query)

    monkeypatch.setattr(widgets, "enabled", lambda: True)
    monkeypatch.setattr(widgets, "handle_widget_query", handler)
    update = Update.model_validate({
        "update_id": 101,
        "widget_query": {
            "id": "q101", "from": {"id": 42}, "chat": {"id": 42},
            "message_id": 123, "data": "b:key:0:0",
        },
    })
    dp = widgets.WidgetDispatcher()
    result = await dp.feed_update(SimpleNamespace(), update)
    assert result is True
    assert calls and calls[0]["data"] == "b:key:0:0"


@pytest.mark.asyncio
async def test_long_unicode_visa_answer_fits_1024_utf8_bytes(monkeypatch):
    from bot.services import entry_rules

    async def rich_rule(country, code):
        return SimpleNamespace(
            country="🌍" * 50, ordinary_passport="🌍" * 700,
            stay="🌍" * 400, details="🌍" * 500,
            source_url="https://www.kdmid.ru/" + "x" * 150,
            updated_at="2026-11-06",
        )

    monkeypatch.setattr(entry_rules, "find_entry_rule", rich_rule)
    answer = await widgets._visa(example(), {"country": "Турция", "country_code": "TR"})
    assert answer["ok"] == 1
    assert len(widgets._encode(answer).encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_invalid_widget_api_setting_fails_safe(monkeypatch):
    monkeypatch.setenv("FLIGHT_BOT_WIDGET_API", "unexpected")
    response = await widgets._api(SimpleNamespace(token="unused"), "sendWidget", {})
    assert response is None


@pytest.mark.asyncio
async def test_widget_method_probe_does_not_send_to_chat(monkeypatch):
    async def fake_api(bot, method, params, *, errors=None):
        assert method == "sendWidget"
        assert "chat_id" not in params
        assert "html" in params
        errors.append("HTTP 404: Not Found")
        return None

    monkeypatch.setattr(widgets, "_api", fake_api)
    result = await widgets.probe_widget_api(SimpleNamespace(token="not-used"))
    assert result == "HTTP 404: Not Found"


@pytest.mark.asyncio
async def test_widgettest_reports_delivery_error(monkeypatch):
    async def fake_api(bot, method, params, *, errors=None):
        assert method == "sendWidget"
        assert params["chat_id"] == 42
        assert "html" in params
        errors.append("HTTP 404: Not Found")
        return None

    monkeypatch.setattr(widgets, "_api", fake_api)
    sent, reason = await widgets.test_widget_delivery(SimpleNamespace(token="not-used"), 42)
    assert sent is False
    assert reason == "HTTP 404: Not Found"


def test_widget_delivery_is_confirmed_by_api_message_widget():
    assert widgets._widget_confirmed({"ok": True, "result": {"message_id": 9, "widget": {"width": 300}}})
    assert not widgets._widget_confirmed({"ok": True})
    assert not widgets._widget_confirmed({"ok": True, "result": {"message_id": 9}})
    assert not widgets._widget_confirmed({"ok": False, "result": {"widget": {}}})


@pytest.mark.asyncio
async def test_api_ok_without_widget_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("FLIGHT_BOT_HTML_BUBBLES", "1")

    async def missing_widget(bot, method, params):
        return {"ok": True, "result": {"message_id": 120}}

    monkeypatch.setattr(widgets, "_api", missing_widget)
    accepted = await widgets.send_browse_widget(SimpleNamespace(token="unused"), 42, "key", example(), False)
    assert accepted is False
