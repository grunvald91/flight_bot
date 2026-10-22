from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.handlers import inline_mode, settings as settings_handler
from bot.services.city_resolver import city_resolver
from bot.utils.parsers import parse_inline_query
from database import crud
from database.crud import SearchFilters


def _dep(days_ahead, hour=10):
    day = datetime.now().date() + timedelta(days=days_ahead)
    return f"{day.isoformat()}T{hour:02d}:00:00+03:00"


# ── Чтение фильтров из настроек ──

@pytest.mark.asyncio
async def test_get_search_filters_defaults_without_settings(monkeypatch):
    async def no_settings(_telegram_id):
        return None

    monkeypatch.setattr(crud, "get_user_settings", no_settings)

    assert await crud.get_search_filters(1) == SearchFilters(direct_only=False, cheapest_month=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direct, cheapest, expected",
    [
        (True, True, SearchFilters(True, True)),
        (False, False, SearchFilters(False, False)),
        (True, False, SearchFilters(True, False)),
        (False, None, SearchFilters(False, True)),  # NULL в базе считается «включено»
    ],
)
async def test_get_search_filters_reads_both_switches(monkeypatch, direct, cheapest, expected):
    async def settings(_telegram_id):
        return SimpleNamespace(direct_flights_only=direct, cheapest_month_search=cheapest)

    monkeypatch.setattr(crud, "get_user_settings", settings)

    assert await crud.get_search_filters(1) == expected


def test_settings_screen_has_both_switches():
    callbacks = {
        button.callback_data
        for row in settings_handler._settings_keyboard().inline_keyboard
        for button in row
    }
    assert "settings:direct_flights" in callbacks
    assert "settings:cheapest_month" in callbacks


@pytest.mark.parametrize("enabled, expected", [(True, "settings:cheapest_off"), (False, "settings:cheapest_on")])
def test_cheapest_month_toggle_button_flips_state(enabled, expected):
    keyboard = settings_handler._cheapest_month_keyboard(enabled)
    assert keyboard.inline_keyboard[0][0].callback_data == expected


# ── Inline-поиск с фильтрами ──

class _FakeInlineQuery:
    def __init__(self):
        self.from_user = SimpleNamespace(id=42)
        self.answers = []

    async def answer(self, results, **kwargs):
        self.answers.append((results, kwargs))


class _FakeApi:
    def __init__(self, month_tickets, daily_tickets=None):
        self.month_tickets = month_tickets
        self.daily_tickets = daily_tickets or []
        self.month_calls = 0
        self.daily_calls = 0

    async def search_cheapest_in_window(self, origin, destination, start_date=None, days=30):
        self.month_calls += 1
        return [dict(t) for t in self.month_tickets]

    async def search_direct_on_date(self, origin, destination, depart_date):
        self.daily_calls += 1
        return [dict(t) for t in self.daily_tickets if t["departure_at"].startswith(depart_date)]

    def build_ticket_link(self, *args, **kwargs):
        return "https://example.test/ticket"


def _ticket(price, days_ahead, transfers=0, hour=10):
    return {
        "price": price,
        "airline": "SU",
        "transfers": transfers,
        "departure_at": _dep(days_ahead, hour),
    }


@pytest.fixture(autouse=True)
def _known_cities(monkeypatch):
    """Справочник городов в тестах не загружается: трёхбуквенные коды считаем распознанными."""
    monkeypatch.setattr(
        city_resolver, "resolve", lambda q: q.upper() if len(q) == 3 and q.isalpha() else None,
    )
    monkeypatch.setattr(city_resolver, "get_city_name", lambda code: code)


async def _run_inline(monkeypatch, filters, api, query="AER-LED"):
    async def fake_filters(_telegram_id):
        return filters

    async def no_save(*args, **kwargs):
        return None

    monkeypatch.setattr(inline_mode, "get_search_filters", fake_filters)
    monkeypatch.setattr(inline_mode, "_save_search", no_save)
    monkeypatch.setattr(inline_mode, "aviasales_api", api)
    # Заголовок = цена, чтобы порядок выдачи было легко проверить
    monkeypatch.setattr(inline_mode, "format_ticket_inline_title", lambda t: str(t["price"]))

    inline_query = _FakeInlineQuery()
    await inline_mode._handle_route_search(inline_query, parse_inline_query(query), 0)
    results, kwargs = inline_query.answers[0]
    assert "not_found" not in [r.id for r in results], "маршрут не распознан, тест проверял бы не то"
    return results, kwargs


def _titles(results):
    return [r.title for r in results if not r.id.startswith(("connections-", "no_results"))]


@pytest.mark.asyncio
async def test_inline_no_date_cheapest_month_is_ranked_by_price(monkeypatch):
    api = _FakeApi([_ticket(9000, 3), _ticket(5000, 20), _ticket(7000, 10, transfers=1)])

    results, kwargs = await _run_inline(monkeypatch, SearchFilters(False, True), api)

    assert api.month_calls == 1 and api.daily_calls == 0
    assert _titles(results) == ["5000", "7000", "9000"]
    assert kwargs["is_personal"] is True  # выдача зависит от настроек, общий кеш Telegram нельзя


@pytest.mark.asyncio
async def test_inline_no_date_old_mode_when_switch_is_off(monkeypatch):
    api = _FakeApi(
        month_tickets=[_ticket(1, 3)],
        daily_tickets=[_ticket(12000, 0), _ticket(8000, 2)],
    )

    results, _ = await _run_inline(monkeypatch, SearchFilters(False, False), api)

    assert api.month_calls == 0 and api.daily_calls == 7
    # прежний порядок: по дате вылета, а не по цене
    assert _titles(results) == ["12000", "8000"]


@pytest.mark.asyncio
async def test_inline_direct_only_hides_connections_and_shortcut(monkeypatch):
    api = _FakeApi([_ticket(4000, 5, transfers=1), _ticket(9000, 6), _ticket(8000, 7, transfers=2)])

    results, _ = await _run_inline(monkeypatch, SearchFilters(True, True), api)

    assert _titles(results) == ["9000"]
    assert not any(r.id.startswith("connections-") for r in results)


@pytest.mark.asyncio
async def test_inline_without_direct_only_keeps_connections_shortcut(monkeypatch):
    api = _FakeApi([_ticket(4000, 5, transfers=1), _ticket(9000, 6)])

    results, _ = await _run_inline(monkeypatch, SearchFilters(False, True), api)

    assert _titles(results) == ["4000", "9000"]
    assert any(r.id.startswith("connections-") for r in results)


@pytest.mark.asyncio
async def test_inline_direct_only_does_not_block_explicit_connections_query(monkeypatch):
    api = _FakeApi([_ticket(4000, 5, transfers=1), _ticket(9000, 6)])

    results, _ = await _run_inline(
        monkeypatch, SearchFilters(True, True), api, query="пересадки AER-LED",
    )

    assert _titles(results) == ["4000"]  # явный запрос про пересадки важнее общего фильтра


@pytest.mark.asyncio
async def test_inline_direct_only_with_no_direct_flights_explains_why(monkeypatch):
    api = _FakeApi([_ticket(4000, 5, transfers=1)])

    results, _ = await _run_inline(monkeypatch, SearchFilters(True, True), api)

    assert [r.id for r in results] == ["no_results"]
    assert "/settings" in results[0].description


# ── Обычный поиск (/search) с фильтрами ──

from bot.handlers import search as search_handler


class _FakeMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=999)  # у сообщения бота автор — сам бот
        self.texts = []

    async def answer(self, text, **kwargs):
        self.texts.append(text)


class _FakeSearchApi:
    def __init__(self, month_tickets, all_tickets=None):
        self.month_tickets = month_tickets
        self.all_tickets = all_tickets or []
        self.month_calls = 0
        self.all_calls = 0

    async def search_cheapest_in_window(self, origin, destination, **kwargs):
        self.month_calls += 1
        return [dict(t) for t in self.month_tickets]

    async def search_all_options(self, **kwargs):
        self.all_calls += 1
        return [dict(t) for t in self.all_tickets]

    def build_ticket_link(self, *args, **kwargs):
        return "https://example.test/ticket"


def _patch_search(monkeypatch, filters, api, seen_user_ids=None):
    async def fake_filters(user_id):
        if seen_user_ids is not None:
            seen_user_ids.append(user_id)
        return filters

    monkeypatch.setattr(search_handler, "get_search_filters", fake_filters)
    monkeypatch.setattr(search_handler, "aviasales_api", api)
    monkeypatch.setattr(search_handler, "format_ticket_message", lambda t, link: f"T{t['price']}")
    monkeypatch.setattr(search_handler, "ticket_keyboard", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_search_no_date_cheapest_month_shows_ranking(monkeypatch):
    api = _FakeSearchApi([_ticket(5000, 20), _ticket(9000, 3)])
    _patch_search(monkeypatch, SearchFilters(False, True), api)
    message = _FakeMessage()

    await search_handler._do_search(message, "AER", "LED", None, user_id=42)

    assert api.month_calls == 1 and api.all_calls == 0
    assert "ближайший месяц" in message.texts[0]
    assert [t for t in message.texts if t.startswith("T")] == ["T5000", "T9000"]


@pytest.mark.asyncio
async def test_search_no_date_old_mode_when_switch_is_off(monkeypatch):
    api = _FakeSearchApi([_ticket(1, 3)], all_tickets=[_ticket(9000, 3), _ticket(5000, 20)])
    _patch_search(monkeypatch, SearchFilters(False, False), api)
    message = _FakeMessage()

    await search_handler._do_search(message, "AER", "LED", None, user_id=42)

    assert api.all_calls == 1 and api.month_calls == 0
    assert "ближайший месяц" not in " ".join(message.texts)


@pytest.mark.asyncio
async def test_search_no_date_direct_only_filters_and_explains_when_empty(monkeypatch):
    api = _FakeSearchApi([_ticket(4000, 5, transfers=1), _ticket(9000, 6)])
    _patch_search(monkeypatch, SearchFilters(True, True), api)
    message = _FakeMessage()

    await search_handler._do_search(message, "AER", "LED", None, user_id=42)
    assert [t for t in message.texts if t.startswith("T")] == ["T9000"]

    api_none = _FakeSearchApi([_ticket(4000, 5, transfers=1)])
    _patch_search(monkeypatch, SearchFilters(True, True), api_none)
    message_none = _FakeMessage()

    await search_handler._do_search(message_none, "AER", "LED", None, user_id=42)
    assert "Без пересадок" in message_none.texts[-1] and "/settings" in message_none.texts[-1]


@pytest.mark.asyncio
async def test_search_callback_reads_filters_of_the_person_not_the_bot(monkeypatch):
    api = _FakeSearchApi([_ticket(5000, 20)])
    seen = []
    _patch_search(monkeypatch, SearchFilters(False, True), api, seen_user_ids=seen)

    class _State:
        async def get_data(self):
            return {"origin": "AER", "destination": "LED"}

        async def clear(self):
            pass

    class _Callback:
        from_user = SimpleNamespace(id=77)
        message = _FakeMessage()

        async def answer(self, *args, **kwargs):
            pass

    await search_handler.cb_search_nodate(_Callback(), _State())

    assert seen == [77]
