from datetime import date

import pytest

from bot.handlers import inline_mode


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
