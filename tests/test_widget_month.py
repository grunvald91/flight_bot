"""Telegram Bot API 10.4 month widget: no external network, no fake prices."""
from datetime import date, timedelta
from types import SimpleNamespace
import json
import zlib

import pytest

from bot.services.discovery_archive import DiscoveryArchive
from bot.widgets import month as mw
from bot.widgets import browse as browse_widget


def make_payload():
    today = date.today()
    dep = lambda day: (today + timedelta(days=day)).isoformat() + "T09:30"
    return {
        "meta": {"kind": "month", "owner_id": 42, "chat_id": 42,
                 "origin": "AER", "destination": "IST", "origin_name": "Сочи",
                 "target_name": "Стамбул", "start": today.isoformat(), "days": 30},
        "options": [
            {"origin": "AER", "destination": "IST", "price": 9800,
             "departure_at": dep(4), "transfers": 0, "airline": "SU", "duration": 145},
            {"origin": "AER", "destination": "IST", "price": 6900,
             "departure_at": dep(8), "transfers": 1, "airline": "TK"},
            {"origin": "AER", "destination": "IST", "price": 8100,
             "departure_at": dep(8), "transfers": None, "airline": "TK"},
            {"origin": "AER", "destination": "IST", "price": 7500,
             "departure_at": dep(22), "transfers": 0, "airline": "DP"},
        ],
    }


def test_month_widget_price_date_order_and_strict_nonstop():
    payload = make_payload()
    page = mw.page_data(payload)
    assert page["m"] == "month" and page["total"] == 4
    assert [r["i"] for r in page["rows"]] == [1, 3, 2, 0]
    assert page["best"]["p"] == 6900
    direct = mw.page_data(payload, 0, True)
    assert direct["total"] == 2
    assert [r["i"] for r in direct["rows"]] == [3, 0]
    date_ordered = mw.page_data(payload, 0, False, "date")
    assert [r["i"] for r in date_ordered["rows"]] == [0, 1, 2, 3]
    chosen_day = mw.page_data(payload, 0, False, "price", 8)
    assert [r["i"] for r in chosen_day["rows"]] == [1, 2]


def test_calendar_is_30_days_and_marks_minimum_for_each_date():
    calendar = mw.calendar_data(make_payload())
    assert calendar["n"] == 30
    assert [(x["i"], x["p"]) for x in calendar["rows"]] == [(4, 9800), (8, 6900), (22, 7500)]
    direct = mw.calendar_data(make_payload(), True)
    assert [x["i"] for x in direct["rows"]] == [4, 22]
    assert len(browse_widget._encode(calendar).encode("utf-8")) <= 1024


def test_month_widget_payloads_and_html_fit_telegram():
    raw = __import__("pathlib").Path("bot/widgets/month.html").read_bytes()
    assert len(raw) <= 32768
    compressed = len(zlib.compress(raw, 9))
    assert compressed <= 4096, f"Month HTML compressed to {compressed} bytes"
    assert mw._template().encode("utf-8") == raw
    payload = make_payload()
    for sort in ("price", "date"):
        assert len(browse_widget._encode(mw.page_data(payload, sort=sort)).encode()) <= 1024
    assert len(browse_widget._encode(mw.detail_data(payload, 0)).encode()) <= 1024


def test_month_archive_scoped_and_strips_unknown_data(tmp_path):
    archive = DiscoveryArchive(tmp_path / "month.sqlite")
    payload = make_payload()
    payload["options"][0]["passport"] = "not-allowed"
    archive.put_month("test-month", payload)
    restored = archive.get("test-month")
    assert restored["meta"]["kind"] == "month"
    assert restored["meta"]["owner_id"] == 42
    assert restored["options"][0]["destination"] == "IST"
    assert "passport" not in restored["options"][0]
    assert mw.page_data(restored)["rows"]
    payload["meta"]["chat_id"] = 77
    with pytest.raises(ValueError):
        archive.put_month("other", payload)


@pytest.mark.asyncio
async def test_month_queries_allow_private_owner_and_reject_invalid(monkeypatch):
    monkeypatch.setattr(mw, "discovery_archive", SimpleNamespace(get=lambda key: make_payload()))
    user = {"from": {"id": 42}, "chat": {"id": 42}}
    result, job = await mw.query_result({**user, "data": "ml:key:0:1:p:-1"})
    assert result["total"] == 2 and job is None
    result, job = await mw.query_result({**user, "data": "mc:key:0"})
    assert result["cal"] == 1 and job is None
    result, job = await mw.query_result({**user, "data": "md:key:0"})
    assert result["detail"] == 1 and job is None
    result, job = await mw.query_result({**user, "data": "ma:key:0"})
    assert result["ok"] == 1 and job == (42, "key", 0)
    for cmd in ("ml:key:0:2:p:-1", "ml:key:100:0:p:-1",
                "ml:key:0:0:x:-1", "ml:key:0:0:p:30", "md:key:-1", "ma:key:999",
                "mc:key:2", "ma:key:abc"):
        result, job = await mw.query_result({**user, "data": cmd})
        assert result["ok"] == 0 and job is None, cmd
    result, job = await mw.query_result({"from": {"id": 73}, "chat": {"id": 73},
                                         "data": "ma:key:0"})
    assert result["ok"] == 0 and job is None


@pytest.mark.asyncio
async def test_widget_sent_only_to_private_chat_and_real_widget_reply(monkeypatch):
    monkeypatch.setenv("FLIGHT_BOT_HTML_BUBBLES", "1")
    calls = []

    async def fake_api(bot, method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 99, "widget": {"height": 560}}}

    monkeypatch.setattr(mw, "_api", fake_api)
    payload = make_payload()
    assert await mw.send_month_widget(SimpleNamespace(token="test"), 42, "key", payload)
    assert len(calls) == 1 and calls[0][0] == "sendWidget"
    assert json.loads(calls[0][1]["chat_data"]) == {"k": "key"}
    assert not await mw.send_month_widget(SimpleNamespace(token="test"), 71, "key", payload)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_month_link_uses_only_aviasales_search_and_no_payment(monkeypatch):
    monkeypatch.setattr(mw, "discovery_archive", SimpleNamespace(get=lambda key: make_payload()))

    class FakeAvia:
        def build_search_link(self, origin, destination, depart_date):
            assert origin == "AER" and destination == "IST"
            return "https://www.aviasales.ru/search/AER01IST1"

    from bot.services import aviasales_api
    monkeypatch.setattr(aviasales_api, "aviasales_api", FakeAvia())
    outgoing = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup):
            outgoing.append((chat_id, text, reply_markup))

    await mw.send_month_link(FakeBot(), 42, "key", 0)
    assert len(outgoing) == 1
    assert outgoing[0][2].inline_keyboard[0][0].url.startswith("https://www.aviasales.ru/")
    await mw.send_month_link(FakeBot(), 73, "key", 0)
    assert len(outgoing) == 1


@pytest.mark.asyncio
async def test_month_job_not_triggered_when_answer_widget_query_fails(monkeypatch):
    jobs = []

    async def fake_query(query):
        return {"ok": 1}, (42, "key", 0)

    async def fake_api(bot, method, params):
        return None

    async def send_actions(*args):
        jobs.append(args)

    monkeypatch.setattr(mw, "query_result", fake_query)
    monkeypatch.setattr(mw, "send_month_link", send_actions)
    monkeypatch.setattr(browse_widget, "_api", fake_api)
    await browse_widget.handle_widget_query(SimpleNamespace(), {"id": "1", "data": "ma:key:0"})
    assert jobs == []

@pytest.mark.asyncio
async def test_month_filter_button_persists_user_preference(monkeypatch):
    from bot.services import search_prefs
    stored = []

    async def save(user, direct):
        stored.append((user.id, direct))
        return True

    monkeypatch.setattr(search_prefs, "save_direct_only", save)
    monkeypatch.setattr(mw, "discovery_archive", SimpleNamespace(get=lambda key: make_payload()))
    owner = {"from": {"id": 42}, "chat": {"id": 42}}
    changed, job = await mw.query_result({**owner, "data": "mt:key:1"})
    assert changed["ok"] == 1 and changed["d"] == 1 and job is None
    assert stored == [(42, True)]
    paged, _ = await mw.query_result({**owner, "data": "ml:key:0:1:p:-1"})
    assert paged["ok"] == 1
    assert stored == [(42, True)], "Pagination must not write preferences"


def test_selected_calendar_date_highlight_uses_only_that_dates_fares():
    payload = make_payload()
    day_four = mw.page_data(payload, page=0, direct=False, sort="price", day=4)
    assert day_four["total"] == 1
    assert day_four["best"]["p"] == 9800
    day_eight = mw.page_data(payload, page=0, direct=False, sort="price", day=8)
    assert day_eight["total"] == 2
    assert day_eight["best"]["p"] == 6900
    direct_day_eight = mw.page_data(payload, page=0, direct=True, sort="price", day=8)
    assert direct_day_eight["total"] == 0
    assert direct_day_eight["best"] is None


def test_outdated_monthly_fare_cannot_be_opened_or_purchased():
    payload = make_payload()
    payload["options"][0]["departure_at"] = (date.today() - timedelta(days=1)).isoformat() + "T10:00"
    page = mw.page_data(payload)
    assert page["total"] == 3
    assert all(r["i"] != 0 for r in page["rows"])
    with pytest.raises(ValueError, match="active search window"):
        mw.detail_data(payload, 0)


def test_month_page_has_all_four_fares_even_with_long_carrier_labels(monkeypatch):
    payload = make_payload()
    for option in payload["options"]:
        option["airline"] = "✈️" * 50
        option["destination"] = "IST"
    page = mw.page_data(payload)
    assert len(page["rows"]) == 4
    assert len(browse_widget._encode(page).encode("utf-8")) <= 1024
