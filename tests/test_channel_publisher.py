import pytest

from bot.services import channel_publisher
from bot.services.fast_browse import IndicativeResults


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    state_path = tmp_path / "channel_publication.json"
    monkeypatch.setattr(channel_publisher, "STATE_PATH", state_path)
    return state_path


def _option(city: str, country: str, price: int, transfers: int = 0) -> dict:
    return {
        "origin": "MOW",
        "destination": "XXX",
        "city": city,
        "country": country,
        "price": price,
        "departure_at": "2026-10-03T08:30:00",
        "transfers": transfers,
        "airline": "XX",
        "flight_number": "XX1",
        "duration": 180,
    }


def test_bind_channel_persists(isolated_state):
    channel_publisher.bind_channel(
        "moscow",
        chat_id=-1001234567890,
        title="Москва",
        username="cheap_moscow",
    )

    binding = channel_publisher.get_channel_bindings()["moscow"]
    assert binding["chat_id"] == -1001234567890
    assert binding["title"] == "Москва"
    assert binding["username"] == "cheap_moscow"


def test_format_channel_post_contains_ranked_options():
    text = channel_publisher.format_channel_post(
        "Москвы",
        "2026-10-03",
        [
            _option("Стамбул", "Турция", 9900),
            _option("Ереван", "Армения", 10500, transfers=1),
        ],
    )

    assert "Самые дешёвые билеты за границу из Москвы" in text
    assert "03.10.2026" in text
    assert "Стамбул" in text and "9 900 ₽" in text
    assert "Ереван" in text and "10 500 ₽" in text
    assert "Цены Aviasales ориентировочные" in text
    assert "@a_ticketbot" in text


@pytest.mark.asyncio
async def test_daily_publish_uses_fast_browse_and_prevents_duplicate(isolated_state, monkeypatch):
    channel_publisher.bind_channel("moscow", chat_id=-1001, title="Москва")
    channel_publisher.bind_channel("sochi", chat_id=-1002, title="Сочи")

    async def fake_load(origin, departure_date):
        city = "Стамбул" if origin == "MOW" else "Ереван"
        country = "Турция" if origin == "MOW" else "Армения"
        return IndicativeResults(
            options=[_option(city, country, 10000)],
            source_rows=1,
            unresolved=0,
        )

    monkeypatch.setattr(channel_publisher, "load_indicative_destinations", fake_load)

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = FakeBot()
    first = await channel_publisher.publish_tomorrow_channels(bot)
    second = await channel_publisher.publish_tomorrow_channels(bot)

    assert first == {"moscow": 1, "sochi": 1}
    assert second == {"moscow": 0, "sochi": 0}
    assert [item["chat_id"] for item in bot.sent] == [-1001, -1002]
