"""Фильтр «Без пересадок» в быстром поиске: прямые рейсы, которые дороже самого дешёвого билета."""
import pytest

from bot.handlers import browse
from bot.services import fast_browse
from bot.services.fast_browse import direct_view, load_indicative_destinations, visible_options


class _Resolver:
    def get_country_code(self, code):
        return {"MOW": "RU", "AER": "RU", "IST": "TR", "EVN": "AM", "DXB": "AE", "TBS": "GE"}.get(code)

    def get_city_name(self, code):
        return {"IST": "Стамбул", "EVN": "Ереван", "DXB": "Дубай", "TBS": "Тбилиси"}.get(code, code)


def _row(dest, price, transfers, airline="XX"):
    return {"destination": dest, "price": price, "transfers": transfers,
            "departure_at": "2026-10-24T09:00:00+03:00", "airline": airline,
            "flight_number": "100", "duration": 180}


class _Api:
    def __init__(self, all_rows, direct_rows, direct_fails=False):
        self.all_rows, self.direct_rows, self.direct_fails = all_rows, direct_rows, direct_fails
        self.calls = []

    async def search_destinations_on_date(self, origin, depart_date, limit=1000, direct=False):
        self.calls.append(direct)
        if direct and self.direct_fails:
            raise RuntimeError("boom")
        return self.direct_rows if direct else self.all_rows


@pytest.fixture(autouse=True)
def _resolver(monkeypatch):
    monkeypatch.setattr(fast_browse, "city_resolver", _Resolver())


def _patch_api(monkeypatch, api):
    monkeypatch.setattr(fast_browse, "aviasales_api", api)


@pytest.mark.asyncio
async def test_destination_with_pricier_direct_flight_survives_the_filter(monkeypatch):
    api = _Api(
        all_rows=[_row("IST", 8000, 1), _row("EVN", 9000, 0), _row("DXB", 12000, 1)],
        direct_rows=[_row("IST", 15000, 0), _row("EVN", 9000, 0)],
    )
    _patch_api(monkeypatch, api)

    result = await load_indicative_destinations("MOW", "2026-10-24")

    assert api.calls == [False, True]
    by_dest = {o["destination"]: o for o in result.options}
    assert by_dest["IST"]["price"] == 8000 and by_dest["IST"]["transfers"] == 1
    assert by_dest["IST"]["direct"]["price"] == 15000

    shown = {o["destination"]: o for _, o in visible_options(result.options, True)}
    assert set(shown) == {"IST", "EVN"}            # Дубай без прямых отсекается
    assert shown["IST"]["price"] == 15000 and shown["IST"]["transfers"] == 0


@pytest.mark.asyncio
async def test_direct_mode_is_sorted_by_direct_price(monkeypatch):
    api = _Api(
        all_rows=[_row("IST", 8000, 1), _row("EVN", 9000, 0)],
        direct_rows=[_row("IST", 15000, 0), _row("EVN", 9000, 0)],
    )
    _patch_api(monkeypatch, api)
    result = await load_indicative_destinations("MOW", "2026-10-24")

    order = [o["destination"] for _, o in visible_options(result.options, True)]
    assert order == ["EVN", "IST"]
    # в полном списке порядок прежний, а номера кнопок ссылаются на него
    assert [o["destination"] for o in result.options] == ["IST", "EVN", ]


@pytest.mark.asyncio
async def test_direct_only_destination_missing_from_main_list_is_added(monkeypatch):
    api = _Api(all_rows=[_row("IST", 8000, 1)], direct_rows=[_row("TBS", 20000, 0)])
    _patch_api(monkeypatch, api)

    result = await load_indicative_destinations("MOW", "2026-10-24")

    assert {o["destination"] for o in result.options} == {"IST", "TBS"}
    assert {o["destination"] for _, o in visible_options(result.options, True)} == {"TBS"}


@pytest.mark.asyncio
async def test_direct_request_failure_does_not_break_the_search(monkeypatch):
    api = _Api(all_rows=[_row("IST", 8000, 1), _row("EVN", 9000, 0)], direct_rows=[], direct_fails=True)
    _patch_api(monkeypatch, api)

    result = await load_indicative_destinations("MOW", "2026-10-24")

    assert result.direct_checked is False
    assert len(result.options) == 2
    assert {o["destination"] for _, o in visible_options(result.options, True)} == {"EVN"}


def test_warning_is_shown_when_direct_flights_were_not_checked():
    payload = {
        "options": [{"origin": "MOW", "destination": "EVN", "city": "Ереван", "country": "Армения",
                     "country_code": "AM", "price": 9000, "transfers": 0}],
        "meta": {"departure_date": "2026-10-24", "direct_checked": False},
    }

    assert "список может быть неполным" in browse._format_page(payload, 0, True)
    assert "список может быть неполным" not in browse._format_page(payload, 0, False)


def test_direct_view_of_connecting_option_without_direct_alternative_is_none():
    assert direct_view({"transfers": 1, "price": 1}) is None
    assert direct_view({"transfers": None, "price": 1}) is None
    assert direct_view({"transfers": 0, "price": 1}) == {"transfers": 0, "price": 1}


# ── Карточка и список в обработчиках ──

import tests.test_browse_direct_filter as helpers  # noqa: E402


def _connecting_with_direct():
    option = helpers._option(0, 1, price=8000)
    option["direct"] = {"price": 15000, "departure_at": "2026-10-03T18:30:00+03:00",
                        "airline": "TK", "flight_number": "412", "duration": 190}
    return [option, helpers._option(1, 1, price=9000)]


@pytest.mark.asyncio
async def test_card_in_direct_mode_shows_the_direct_fare(monkeypatch, helpers_prefs):
    helpers._use_archive(monkeypatch, _connecting_with_direct())

    callback = helpers._FakeCallback("brsel:abc:0:1")
    await browse.browse_select(callback)

    text, _ = callback.message.edits[0]
    assert "15 000" in text and "8 000" not in text
    assert "без пересадок" in text
    assert "TK" in text


@pytest.mark.asyncio
async def test_card_without_filter_still_shows_the_cheapest_fare(monkeypatch, helpers_prefs):
    helpers._use_archive(monkeypatch, _connecting_with_direct())

    callback = helpers._FakeCallback("brsel:abc:0:0")
    await browse.browse_select(callback)

    text, _ = callback.message.edits[0]
    assert "8 000" in text and "15 000" not in text
    assert "пересадок: 1" in text


def test_list_in_direct_mode_shows_the_direct_price():
    payload = helpers._payload(_connecting_with_direct())

    text = browse._format_page(payload, 0, True)

    assert "15 000" in text and "8 000" not in text
    assert "Вариантов: <b>1</b> из 2" in text


@pytest.fixture
def helpers_prefs(monkeypatch):
    async def load(user_id):
        return False

    async def save(user, value):
        return True

    monkeypatch.setattr(browse, "load_direct_only", load)
    monkeypatch.setattr(browse, "save_direct_only", save)
