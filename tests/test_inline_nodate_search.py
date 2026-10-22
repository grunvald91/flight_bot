from datetime import date

import pytest

from bot.handlers import inline_mode
from bot.services.aviasales_api import AviasalesAPI


# ── Режим «дешёвые на месяц» выключен: прежнее поведение (окно 7 дней по дням) ──

class _FakeAviasalesAPI:
    def __init__(self):
        self.direct_calls = []
        self.price_calls = []

    async def search_direct_on_date(
        self,
        origin: str,
        destination: str,
        depart_date: str,
    ):
        self.direct_calls.append(
            {
                "origin": origin,
                "destination": destination,
                "depart_date": depart_date,
            }
        )
        return [
            {
                "price": 12000 + len(self.direct_calls),
                "airline": "SU",
                "transfers": 0,
                "departure_at": f"{depart_date}T10:15:00+03:00",
            }
        ]

    async def search_prices_for_dates(
        self,
        origin: str,
        destination: str,
        depart_date: str | None = None,
        return_date: str | None = None,
        one_way: bool = True,
        direct: bool = False,
        limit: int = 50,
        unique: bool = False,
        sorting: str = "price",
    ):
        self.price_calls.append(
            {
                "origin": origin,
                "destination": destination,
                "depart_date": depart_date,
                "return_date": return_date,
                "one_way": one_way,
                "direct": direct,
                "limit": limit,
                "unique": unique,
                "sorting": sorting,
            }
        )
        return [
            {
                "value": 18000 + len(self.price_calls),
                "airline": "DP",
                "number_of_changes": 1,
                "departure_at": f"{depart_date}T16:40:00+03:00",
            },
            {
                "value": 15000 + len(self.price_calls),
                "airline": "SU",
                "number_of_changes": 0,
                "departure_at": f"{depart_date}T09:20:00+03:00",
            },
        ]

    async def search_all_options(self, *args, **kwargs):
        raise AssertionError("No-date inline search should not call heavy search_all_options()")


@pytest.mark.asyncio
async def test_search_route_without_date_uses_direct_search_by_default(monkeypatch):
    fake_api = _FakeAviasalesAPI()
    monkeypatch.setattr(inline_mode, "aviasales_api", fake_api)

    tickets = await inline_mode._search_route_without_date(
        "AER",
        "LED",
        start_date=date(2026, 4, 7),
        horizon_days=3,
    )

    assert [call["depart_date"] for call in fake_api.direct_calls] == [
        "2026-04-07",
        "2026-04-08",
        "2026-04-09",
    ]
    assert fake_api.price_calls == []
    assert len(tickets) == 3
    assert all(ticket["transfers"] == 0 for ticket in tickets)
    assert tickets[0]["price"] == 12001


@pytest.mark.asyncio
async def test_search_route_without_date_marks_day_labels(monkeypatch):
    fake_api = _FakeAviasalesAPI()
    monkeypatch.setattr(inline_mode, "aviasales_api", fake_api)

    tickets = await inline_mode._search_route_without_date(
        "AER",
        "LED",
        start_date=date(2026, 4, 7),
        horizon_days=3,
    )

    assert tickets[0]["inline_day_label"] == "Сегодня"
    assert tickets[1]["inline_day_label"] == "Завтра"
    assert tickets[2]["inline_day_label"] == "Послезавтра"


@pytest.mark.asyncio
async def test_search_route_without_date_connections_mode_uses_prices_and_filters_direct(monkeypatch):
    fake_api = _FakeAviasalesAPI()
    monkeypatch.setattr(inline_mode, "aviasales_api", fake_api)

    tickets = await inline_mode._search_route_without_date(
        "AER",
        "LED",
        start_date=date(2026, 4, 7),
        horizon_days=2,
        connections_only=True,
    )

    assert fake_api.direct_calls == []
    assert [call["depart_date"] for call in fake_api.price_calls] == [
        "2026-04-07",
        "2026-04-08",
    ]
    assert all(ticket["transfers"] > 0 for ticket in tickets)
    assert len(tickets) == 2
    assert tickets[0]["price"] == 18001


# ── Режим «дешёвые на месяц» включён ──

def _ticket(price, departure_at, transfers=0, airline="SU"):
    return {
        "price": price,
        "airline": airline,
        "transfers": transfers,
        "departure_at": departure_at,
    }


class _FakeMonthAPI:
    def __init__(self, tickets):
        self.tickets = tickets
        self.calls = []

    async def search_cheapest_in_window(self, origin, destination, start_date=None, days=30):
        self.calls.append(
            {"origin": origin, "destination": destination, "start_date": start_date, "days": days}
        )
        return [dict(t) for t in self.tickets]

    async def search_all_options(self, *args, **kwargs):
        raise AssertionError("No-date inline search should not call heavy search_all_options()")


@pytest.mark.asyncio
async def test_cheapest_month_asks_for_month_window(monkeypatch):
    fake_api = _FakeMonthAPI([_ticket(9000, "2026-04-10T10:00:00+03:00")])
    monkeypatch.setattr(inline_mode, "aviasales_api", fake_api)

    tickets = await inline_mode._search_route_cheapest_month(
        "AER", "LED", start_date=date(2026, 4, 7),
    )

    assert fake_api.calls == [
        {"origin": "AER", "destination": "LED", "start_date": date(2026, 4, 7), "days": 30}
    ]
    assert len(tickets) == 1


@pytest.mark.asyncio
async def test_cheapest_month_marks_day_labels(monkeypatch):
    fake_api = _FakeMonthAPI([
        _ticket(9000, "2026-04-07T10:00:00+03:00"),
        _ticket(9500, "2026-04-08T10:00:00+03:00"),
        _ticket(9800, "2026-04-09T10:00:00+03:00"),
    ])
    monkeypatch.setattr(inline_mode, "aviasales_api", fake_api)

    tickets = await inline_mode._search_route_cheapest_month(
        "AER", "LED", start_date=date(2026, 4, 7),
    )

    assert tickets[0]["inline_day_label"] == "Сегодня"
    assert tickets[1]["inline_day_label"] == "Завтра"
    assert tickets[2]["inline_day_label"] == "Послезавтра"


@pytest.mark.asyncio
async def test_cheapest_month_connections_mode_filters_direct(monkeypatch):
    fake_api = _FakeMonthAPI([
        _ticket(8000, "2026-04-07T09:00:00+03:00", transfers=0),
        _ticket(7000, "2026-04-08T09:00:00+03:00", transfers=1),
        _ticket(7500, "2026-04-09T09:00:00+03:00", transfers=2),
    ])
    monkeypatch.setattr(inline_mode, "aviasales_api", fake_api)

    tickets = await inline_mode._search_route_cheapest_month(
        "AER", "LED", start_date=date(2026, 4, 7), connections_only=True,
    )

    assert all(ticket["transfers"] > 0 for ticket in tickets)
    assert len(tickets) == 2


# ── search_cheapest_in_window поверх подменённого запроса цен ──

class _FakeMonthPricesAPI(AviasalesAPI):
    """Реальный search_cheapest_in_window поверх подменённого запроса цен."""

    def __init__(self, by_month):
        self.by_month = by_month
        self.month_calls = []

    async def search_prices_for_dates(self, origin, destination, depart_date=None, **kwargs):
        self.month_calls.append((depart_date, kwargs.get("one_way"), kwargs.get("limit")))
        return [dict(t) for t in self.by_month.get(depart_date, [])]


@pytest.mark.asyncio
async def test_cheapest_in_window_sorts_by_price_and_cuts_to_window():
    api = _FakeMonthPricesAPI({
        "2026-04": [
            _ticket(12000, "2026-04-20T10:00:00+03:00"),
            _ticket(5000, "2026-04-03T10:00:00+03:00"),   # раньше окна
            _ticket(7000, "2026-04-25T08:00:00+03:00", transfers=1),
        ],
        "2026-05": [
            _ticket(6000, "2026-05-02T10:00:00+03:00"),   # внутри окна
            _ticket(4000, "2026-05-20T10:00:00+03:00"),   # позже окна
        ],
    })

    tickets = await api.search_cheapest_in_window(
        "AER", "LED", start_date=date(2026, 4, 7), days=30,
    )

    assert [t["price"] for t in tickets] == [6000, 7000, 12000]
    assert [month for month, _, _ in api.month_calls] == ["2026-04", "2026-05"]


@pytest.mark.asyncio
async def test_cheapest_in_window_normalizes_and_dedupes():
    api = _FakeMonthPricesAPI({
        "2026-04": [
            {"value": 9000, "airline": "SU", "number_of_changes": 1,
             "departure_at": "2026-04-10T10:00:00+03:00"},
            {"price": 9000, "airline": "SU", "transfers": 1,
             "departure_at": "2026-04-10T10:00:00+03:00"},
            {"price": 0, "airline": "DP", "departure_at": "2026-04-11T10:00:00+03:00"},
        ],
    })

    tickets = await api.search_cheapest_in_window(
        "AER", "LED", start_date=date(2026, 4, 7), days=10,
    )

    assert len(tickets) == 1
    assert tickets[0]["price"] == 9000
    assert tickets[0]["transfers"] == 1
    assert tickets[0]["origin"] == "AER"
    assert tickets[0]["destination"] == "LED"
