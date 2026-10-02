import pytest

from bot.handlers import travel
from bot.services.tutu_mcp import TutuMCPClient


@pytest.mark.asyncio
async def test_multitransport_client_uses_verified_tutu_args(monkeypatch):
    client = TutuMCPClient()
    seen = {}

    async def fake_call(tool_name, arguments):
        seen["tool_name"] = tool_name
        seen["arguments"] = arguments
        return {"variants": []}

    monkeypatch.setattr(client, "_call", fake_call)

    await client.search_multitransport(
        "Москва",
        "Казань",
        "2026-10-15",
        adults=1,
        optimize_for="price",
        page_size=12,
    )

    assert seen["tool_name"] == "search_multitransport"
    assert seen["arguments"] == {
        "origin": "Москва",
        "destination": "Казань",
        "departure_date": "2026-10-15",
        "adults": 1,
        "optimize_for": "price",
        "page": 1,
        "page_size": 12,
        "view": "compact",
    }


@pytest.mark.asyncio
async def test_hotel_client_uses_whole_stay_dates(monkeypatch):
    client = TutuMCPClient()
    seen = {}

    async def fake_call(tool_name, arguments):
        seen["tool_name"] = tool_name
        seen["arguments"] = arguments
        return {"hotels": []}

    monkeypatch.setattr(client, "_call", fake_call)
    await client.search_hotels("Сочи", "2026-10-15", "2026-10-18", adults=1)

    assert seen["tool_name"] == "search_hotels"
    assert seen["arguments"]["city_name"] == "Сочи"
    assert seen["arguments"]["check_in"] == "2026-10-15"
    assert seen["arguments"]["check_out"] == "2026-10-18"
    assert seen["arguments"]["adults"] == 1


def test_hotel_format_does_not_multiply_stay_total():
    hotel = {
        "name": "Тест Отель",
        "stars": 3,
        "rating": 8.5,
        "best_offer": {
            "price": {"amount": 15000, "currency": "RUB"},
            "price_basis": "stay_total",
            "breakfast_included": True,
            "free_cancellation": True,
        },
    }

    text = travel._format_hotels(
        "Казань", "2026-10-15", "2026-10-18", 3, [hotel]
    )

    assert "15 000 ₽" in text
    assert "за всё проживание" in text
    assert "45 000" not in text


def test_trip_total_is_transport_both_ways_plus_whole_stay_hotel():
    outbound = {
        "transport": "railway",
        "price": {"amount": 2500, "currency": "RUB"},
        "duration_min": 600,
    }
    inbound = {
        "transport": "avia",
        "price": {"amount": 6000, "currency": "RUB"},
        "duration_min": 90,
    }
    hotel = {
        "name": "Тест Отель",
        "best_offer": {
            "price": {"amount": 12000, "currency": "RUB"},
            "price_basis": "stay_total",
        },
    }

    text = travel._format_trip(
        "Москва", "Казань", "2026-10-15", "2026-10-18",
        outbound, inbound, hotel,
    )

    assert "20 500 ₽" in text
    assert "три" in text.lower() or "трёх" in text.lower()


def test_merge_variants_deduplicates_same_offer():
    cheap = {
        "offer_id": "same",
        "transport": "railway",
        "price": {"amount": 2500, "currency": "RUB"},
        "duration_min": 600,
    }
    duplicate = dict(cheap)
    fast = {
        "offer_id": "fast",
        "transport": "avia",
        "price": {"amount": 6000, "currency": "RUB"},
        "duration_min": 90,
    }

    merged = travel._merge_variants([
        {"variants": [cheap, fast]},
        {"variants": [duplicate, fast]},
    ])

    assert [item["offer_id"] for item in merged] == ["same", "fast"]
