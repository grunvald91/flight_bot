"""Страна вместо города: «Дешёвые на месяц» и «Дешёвые за границу»."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import tests.test_browse_direct_filter as bhelp
import tests.test_month_search as mhelp
from bot.handlers import browse, month
from bot.services import countries, country_search
from bot.services.city_resolver import city_resolver
from bot.services.fast_browse import IndicativeResults

CITY_COUNTRY = {"IST": "TR", "AYT": "TR", "DLM": "TR", "EVN": "AM", "AER": "RU", "MOW": "RU"}
CITY_NAME = {"IST": "Стамбул", "AYT": "Анталья", "DLM": "Даламан", "EVN": "Ереван", "AER": "Сочи"}


@pytest.fixture(autouse=True)
def _reference(monkeypatch):
    monkeypatch.setattr(city_resolver, "iata_to_country", dict(CITY_COUNTRY))
    monkeypatch.setattr(city_resolver, "get_country_code", lambda c: CITY_COUNTRY.get((c or "").upper()))
    monkeypatch.setattr(city_resolver, "get_city_name", lambda c: CITY_NAME.get(c, c))
    monkeypatch.setattr(city_resolver, "get_airline_name", lambda c: c)
    monkeypatch.setattr(city_resolver, "resolve",
                        lambda q: q.upper() if len(q) in (2, 3) and q.isalpha() and q.isascii() else None)


@pytest.fixture(autouse=True)
def prefs(monkeypatch):
    store = SimpleNamespace(data={})

    async def load(user_id):
        return store.data.get(user_id, False)

    async def save(user, value):
        store.data[user.id] = value
        return True

    monkeypatch.setattr(month, "load_direct_only", load)
    monkeypatch.setattr(month, "save_direct_only", save)
    monkeypatch.setattr(browse, "load_direct_only", load)
    monkeypatch.setattr(browse, "save_direct_only", save)
    return store


# ── Разбор названия страны ──

def test_country_names_are_recognised():
    for text in ("Турция", "турция", "  Турция! ", "Turkey", "Армения"):
        assert countries.resolve_country(text) is not None, text
    assert countries.resolve_country("Турция") == "TR"
    assert countries.resolve_country("turkey") == "TR"


def test_city_is_not_taken_for_a_country():
    assert countries.resolve_country("Сочи") is None
    assert countries.resolve_country("Стамбул") is None
    assert countries.resolve_country("") is None


def test_country_without_airports_in_reference_is_not_offered():
    assert countries.resolve_country("Франция") is None  # в тестовом справочнике городов нет


# ── Поиск по всем городам страны ──

class _Api:
    def __init__(self, month_rows, tickets, fail_for=()):
        self.month_rows, self.tickets, self.fail_for = month_rows, tickets, set(fail_for)
        self.month_calls, self.window_calls = [], []

    async def search_destinations_in_month(self, origin, month_, direct=False, **kw):
        self.month_calls.append((origin, month_, direct))
        return [{"destination": d} for d in self.month_rows]

    async def search_cheapest_in_window(self, origin, destination, start_date=None, days=30):
        self.window_calls.append(destination)
        if destination in self.fail_for:
            raise RuntimeError("boom")
        return [dict(t) for t in self.tickets.get(destination, [])]


def _t(price, days=3, transfers=0):
    day = datetime.now().date() + timedelta(days=days)
    return {"price": price, "transfers": transfers, "airline": "SU",
            "departure_at": f"{day.isoformat()}T10:00:00+03:00", "origin": "AER"}


@pytest.mark.asyncio
async def test_country_search_merges_cities_and_ranks_by_price(monkeypatch):
    api = _Api(["IST", "AYT", "EVN", "AER"], {
        "IST": [_t(9000), _t(6000)], "AYT": [_t(4000), _t(12000)], "EVN": [_t(1)],
    })
    monkeypatch.setattr(country_search, "aviasales_api", api)

    tickets = await country_search.search_cheapest_to_country("AER", "TR", days=30)

    assert [(t["destination"], t["price"]) for t in tickets] == [
        ("AYT", 4000), ("IST", 6000), ("IST", 9000), ("AYT", 12000),
    ]
    assert sorted(api.window_calls) == ["AYT", "IST"]  # Ереван и Сочи — не Турция


@pytest.mark.asyncio
async def test_country_search_direct_only_asks_for_direct_and_filters(monkeypatch):
    api = _Api(["IST"], {"IST": [_t(3000, transfers=1), _t(8000, transfers=0)]})
    monkeypatch.setattr(country_search, "aviasales_api", api)

    tickets = await country_search.search_cheapest_to_country("AER", "TR", direct_only=True)

    assert [t["price"] for t in tickets] == [8000]
    assert all(call[2] is True for call in api.month_calls)


@pytest.mark.asyncio
async def test_one_failing_city_does_not_break_the_country(monkeypatch):
    api = _Api(["IST", "AYT"], {"IST": [_t(7000)], "AYT": [_t(5000)]}, fail_for=["AYT"])
    monkeypatch.setattr(country_search, "aviasales_api", api)

    tickets = await country_search.search_cheapest_to_country("AER", "TR")

    assert [t["destination"] for t in tickets] == ["IST"]


@pytest.mark.asyncio
async def test_country_without_flights_gives_empty_list(monkeypatch):
    monkeypatch.setattr(country_search, "aviasales_api", _Api([], {}))

    assert await country_search.search_cheapest_to_country("AER", "TR") == []


# ── «Дешёвые на месяц»: страна вводится как направление ──

async def _month_with_country(monkeypatch, tickets, text="Турция"):
    async def fake(origin, country, days=30, direct_only=False):
        fake.calls.append((origin, country, direct_only))
        return [t for t in tickets if not direct_only or t["transfers"] == 0]

    fake.calls = []
    monkeypatch.setattr(month, "search_cheapest_to_country", fake)
    state = mhelp._FakeState({"month_origin": "AER"})
    message = mhelp._FakeUserMessage(text)
    await month.month_destination(message, state)
    return fake, state, message


def _country_tickets():
    return [{**_t(4000), "destination": "AYT"}, {**_t(6000, transfers=1), "destination": "IST"}]


@pytest.mark.asyncio
async def test_country_as_destination_shows_city_in_every_line(monkeypatch):
    fake, state, message = await _month_with_country(monkeypatch, _country_tickets())

    assert fake.calls == [("AER", "TR", False)]
    assert state.state is None
    text, kwargs = message.edits[0]
    assert "Сочи → Турция (все города)" in text
    assert "Анталья" in text and "Стамбул" in text
    assert text.index("4 000") < text.index("6 000")
    callbacks = [b.callback_data for b in mhelp._buttons(kwargs["reply_markup"])]
    assert "mt:AER:TR:1" in callbacks


@pytest.mark.asyncio
async def test_country_buy_links_use_each_tickets_own_city(monkeypatch):
    seen = []

    class Api:
        def build_ticket_link(self, ticket, origin, destination, *a, **k):
            seen.append(destination)
            return "https://example.test"

    monkeypatch.setattr(month, "aviasales_api", Api())
    await _month_with_country(monkeypatch, _country_tickets())

    assert seen == ["AYT", "IST"]


@pytest.mark.asyncio
async def test_toggle_in_a_country_search_keeps_the_country(monkeypatch):
    async def fake(origin, country, days=30, direct_only=False):
        return [t for t in _country_tickets() if not direct_only or t["transfers"] == 0]

    monkeypatch.setattr(month, "search_cheapest_to_country", fake)
    callback = mhelp._FakeCallback("mt:AER:TR:1")

    await month.cb_month_toggle(callback)

    text, kwargs = callback.message.edits[0]
    assert "Турция (все города)" in text
    assert "Стамбул" not in text and "Анталья" in text  # осталась только прямая
    assert "mo:AER:TR:1:0" not in [b.callback_data for b in mhelp._buttons(kwargs["reply_markup"])]


@pytest.mark.asyncio
async def test_empty_country_result_has_a_clear_message(monkeypatch):
    fake, state, message = await _month_with_country(monkeypatch, [])

    text, _ = message.edits[0]
    assert "в эту страну" in text


def test_country_callbacks_are_parsed_strictly():
    assert month._parse_callback("mo:AER:TR:0:2") == ("AER", "TR", False, 2)
    assert month._parse_callback("mo:AER:IST:1:0") == ("AER", "IST", True, 0)
    assert month._parse_toggle("mt:AER:TR:1") == ("AER", "TR", True)
    for bad in ("mo:AER:T:0:1", "mo:AER:TRXX:0:1", "mo:AER:t1:0:1", "mo:AER:T²:0:1", "mo:AE:TR:0:1"):
        assert month._parse_callback(bad) is None, bad


@pytest.mark.asyncio
async def test_two_letter_city_code_is_not_mistaken_for_a_country(monkeypatch):
    monkeypatch.setattr(city_resolver, "resolve", lambda q: "XX")
    state = mhelp._FakeState({"month_origin": "AER"})
    message = mhelp._FakeUserMessage("что-то")

    await month.month_destination(message, state)

    assert "Не нашёл" in message.answers[0][0]
    assert message.edits == []


@pytest.mark.asyncio
async def test_city_destination_still_works_as_before(monkeypatch):
    api = mhelp._use_api(monkeypatch, [mhelp._ticket(5000)])
    state = mhelp._FakeState({"month_origin": "AER"})

    await month.month_destination(mhelp._FakeUserMessage("LED"), state)

    assert api.calls == [("AER", "LED", 30)]


# ── «Дешёвые за границу»: шаг выбора страны ──

def _options():
    base = {"origin": "AER", "transfers": 0, "departure_at": "2026-10-24T10:00:00+03:00"}
    return [
        {**base, "destination": "AYT", "city": "Анталья", "country": "Турция", "country_code": "TR", "price": 4000},
        {**base, "destination": "EVN", "city": "Ереван", "country": "Армения", "country_code": "AM", "price": 5000},
        {**base, "destination": "IST", "city": "Стамбул", "country": "Турция", "country_code": "TR", "price": 6000},
    ]


@pytest.mark.asyncio
async def test_browse_origin_now_asks_for_the_country():
    state = mhelp._FakeState()
    message = mhelp._FakeUserMessage("AER")

    await browse.browse_origin(message, state)

    assert state.state == browse.FastBrowseStates.choosing_country
    text, kwargs = message.answers[0]
    assert "страну прилёта" in text
    assert "brc:any" in [b.callback_data for b in mhelp._buttons(kwargs["reply_markup"])]


@pytest.mark.asyncio
async def test_all_countries_button_goes_to_the_date_with_no_country():
    state = mhelp._FakeState({"browse_origin": "AER", "browse_country": "TR"})
    callback = mhelp._FakeCallback("brc:any")

    await browse.browse_any_country(callback, state)

    assert state.state == browse.FastBrowseStates.choosing_date
    assert state.data["browse_country"] is None


@pytest.mark.asyncio
async def test_typed_country_goes_to_the_date_and_is_remembered():
    state = mhelp._FakeState({"browse_origin": "AER"})
    message = mhelp._FakeUserMessage("Турция")

    await browse.browse_country(message, state)

    assert state.state == browse.FastBrowseStates.choosing_date
    assert state.data["browse_country"] == "TR"
    assert "Турция" in message.answers[0][0]


@pytest.mark.asyncio
async def test_unknown_country_is_asked_again():
    state = mhelp._FakeState({"browse_origin": "AER"})
    message = mhelp._FakeUserMessage("Нарния")

    await browse.browse_country(message, state)

    assert state.state is None  # состояние не менялось
    assert "Не нашёл такую страну" in message.answers[0][0]


async def _run(monkeypatch, country):
    archive = bhelp._FakeArchive()
    monkeypatch.setattr(browse, "discovery_archive", archive)

    async def fake_load(origin, departure_date):
        return IndicativeResults(_options(), source_rows=3, unresolved=0)

    monkeypatch.setattr(browse, "load_indicative_destinations", fake_load)
    message = bhelp._FakeMessage()
    await browse._run_browse(message, "AER", "2026-10-24", owner_id=42, country=country)
    return archive, message


@pytest.mark.asyncio
async def test_browse_with_a_country_lists_only_that_country(monkeypatch):
    archive, message = await _run(monkeypatch, "TR")

    text, _ = message.edits[0]
    assert "⚡ Турция" in text
    assert "Анталья" in text and "Стамбул" in text and "Ереван" not in text
    saved = next(iter(archive.saved.values()))
    assert [o["destination"] for o in saved["options"]] == ["AYT", "IST"]
    assert saved["meta"]["country_code"] == "TR"


@pytest.mark.asyncio
async def test_browse_without_a_country_lists_everything(monkeypatch):
    _, message = await _run(monkeypatch, None)

    text, _ = message.edits[0]
    assert "Зарубежные направления" in text and "Ереван" in text


@pytest.mark.asyncio
async def test_browse_country_without_options_says_so(monkeypatch):
    _, message = await _run(monkeypatch, "GE")

    text, _ = message.edits[0]
    assert "нет вариантов" in text


@pytest.mark.parametrize("text,code", [
    ("Гонконг", "HK"), ("Макао", "MO"), ("Палестина", "PS"), ("Македония", "MK"),
    ("Северная Корея", "KP"), ("Объединённые Арабские Эмираты", "AE"), ("ОАЭ", "AE"),
    ("Босния", "BA"), ("ЮАР", "ZA"), ("Сейшелы", "SC"), ("ДР Конго", "CD"),
    ("Южная Корея", "KR"), ("США", "US"), ("Англия", "GB"),
])
def test_colloquial_country_names(monkeypatch, text, code):
    monkeypatch.setattr(
        city_resolver, "iata_to_country",
        {f"X{i:02d}": c for i, c in enumerate(
            ["HK", "MO", "PS", "MK", "KP", "AE", "BA", "ZA", "SC", "CD", "KR", "US", "GB"])},
    )

    assert countries.resolve_country(text) == code
