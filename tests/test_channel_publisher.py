import pytest

from bot.services import channel_publisher


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    state_path = tmp_path / "channel_publication.json"
    monkeypatch.setattr(channel_publisher, "STATE_PATH", state_path)
    return state_path


def _offer(city: str, country: str, code: str, price: int, url: str = "") -> dict:
    return {
        "price": {"amount": price, "currency": "RUB"},
        "departure_at": "2026-10-03T08:30:00",
        "legs": [{"segments": [{"voyage_no": "XX1"}]}],
        "search_results_url": url,
        "_discovery": {
            "city": city,
            "country": country,
            "country_code": code,
        },
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


def test_format_channel_post_contains_ranked_offers():
    text = channel_publisher.format_channel_post(
        "Москвы",
        "2026-10-03",
        [
            _offer("Стамбул", "Турция", "TR", 9900, "https://example.com/1"),
            _offer("Ереван", "Армения", "AM", 10500),
        ],
    )

    assert "Самые дешёвые билеты за границу из Москвы" in text
    assert "03.10.2026" in text
    assert "1." in text and "Стамбул" in text and "9 900 ₽" in text
    assert "2." in text and "Ереван" in text and "10 500 ₽" in text
    assert "@a_ticketbot" in text


@pytest.mark.asyncio
async def test_daily_publish_uses_bindings_and_prevents_duplicate(isolated_state, monkeypatch):
    channel_publisher.bind_channel("moscow", chat_id=-1001, title="Москва")
    channel_publisher.bind_channel("sochi", chat_id=-1002, title="Сочи")

    class Offers(list):
        unverified = 0

    async def fake_discover(origin, departure_date, result_limit=None):
        city = "Стамбул" if origin == "MOW" else "Ереван"
        code = "TR" if origin == "MOW" else "AM"
        country = "Турция" if origin == "MOW" else "Армения"
        return Offers([_offer(city, country, code, 10000)])

    monkeypatch.setattr(channel_publisher, "discover_international_offers", fake_discover)

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
