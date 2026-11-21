import asyncio
from datetime import datetime

import pytest

from bot.services import channel_publisher
from bot.services.fast_browse import IndicativeResults


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    channel_publisher._SENT_THIS_PROCESS.clear()
    monkeypatch.setenv("ADMIN_IDS", "")
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


@pytest.mark.asyncio
async def test_retry_after_temporary_aviasales_failure(isolated_state, monkeypatch):
    channel_publisher.bind_channel("moscow", chat_id=-1001)
    channel_publisher.bind_channel("sochi", chat_id=-1002)
    calls = {"MOW": 0, "AER": 0}

    async def fake_load(origin, departure_date):
        calls[origin] += 1
        if origin == "MOW" and calls[origin] == 1:
            raise RuntimeError("temporary upstream outage")
        return IndicativeResults([_option("Ереван", "Армения", 7900)], 1, 0)

    monkeypatch.setattr(channel_publisher, "load_indicative_destinations", fake_load)

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = FakeBot()
    first = await channel_publisher.publish_tomorrow_channels(bot)
    second = await channel_publisher.publish_tomorrow_channels(bot)
    third = await channel_publisher.publish_tomorrow_channels(bot)

    assert first == {"moscow": 0, "sochi": 1}
    assert second == {"moscow": 1, "sochi": 0}
    assert third == {"moscow": 0, "sochi": 0}
    assert calls == {"MOW": 2, "AER": 1}
    assert [msg["chat_id"] for msg in bot.sent] == [-1002, -1001]
    assert channel_publisher.get_channel_bindings()["moscow"]["last_attempt_status"] == "published"


@pytest.mark.asyncio
async def test_empty_results_retry_and_alert_once(isolated_state, monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "777")
    channel_publisher.bind_channel("moscow", chat_id=-1001)
    channel_publisher.bind_channel("sochi", chat_id=-1002)
    calls = {"MOW": 0, "AER": 0}

    async def fake_load(origin, departure_date):
        calls[origin] += 1
        return IndicativeResults([], 0, 0)

    monkeypatch.setattr(channel_publisher, "load_indicative_destinations", fake_load)

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = FakeBot()
    assert await channel_publisher.publish_tomorrow_channels(bot) == {"moscow": 0, "sochi": 0}
    assert await channel_publisher.publish_tomorrow_channels(bot) == {"moscow": 0, "sochi": 0}
    assert calls == {"MOW": 2, "AER": 2}
    assert [msg["chat_id"] for msg in bot.sent] == [777, 777]
    bindings = channel_publisher.get_channel_bindings()
    assert bindings["moscow"]["last_attempt_status"] == "no_valid_fares"


@pytest.mark.asyncio
async def test_overlapping_jobs_cannot_duplicate_posts(isolated_state, monkeypatch):
    channel_publisher.bind_channel("moscow", chat_id=-1001)
    channel_publisher.bind_channel("sochi", chat_id=-1002)

    async def fake_load(origin, departure_date):
        await asyncio.sleep(0.01)
        return IndicativeResults([_option("Ереван", "Армения", 7900)], 1, 0)

    monkeypatch.setattr(channel_publisher, "load_indicative_destinations", fake_load)

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = FakeBot()
    first, second = await asyncio.gather(
        channel_publisher.publish_tomorrow_channels(bot),
        channel_publisher.publish_tomorrow_channels(bot),
    )
    assert first == {"moscow": 1, "sochi": 1}
    assert second == {"moscow": 0, "sochi": 0}
    assert [msg["chat_id"] for msg in bot.sent] == [-1001, -1002]


@pytest.mark.asyncio
async def test_scheduler_catchup_starts_only_after_posting_time(monkeypatch):
    from bot.services import scheduler

    monkeypatch.setattr(scheduler, "DAILY_CHANNEL_HOUR", 8)
    monkeypatch.setattr(scheduler, "DAILY_CHANNEL_MINUTE", 0)
    assert not scheduler._channel_publication_due(datetime(2026, 11, 21, 7, 59))
    assert scheduler._channel_publication_due(datetime(2026, 11, 21, 8, 0))
    assert scheduler._channel_publication_due(datetime(2026, 11, 21, 10, 24))

    calls = []

    async def fake_publish(bot):
        calls.append(bot)
        return {"moscow": 1}

    monkeypatch.setattr(scheduler, "publish_tomorrow_channels", fake_publish)

    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 11, 21, 7, 59, tzinfo=tz)

    monkeypatch.setattr(scheduler, "datetime", FakeDateTime)
    assert await scheduler.retry_missed_channel_publications("bot") == {}
    assert calls == []
    FakeDateTime.now = staticmethod(lambda tz=None: datetime(2026, 11, 21, 8, 5, tzinfo=tz))
    assert await scheduler.retry_missed_channel_publications("bot") == {"moscow": 1}
    assert calls == ["bot"]


@pytest.mark.asyncio
async def test_scheduler_registers_startup_and_periodic_recovery():
    from bot.services.scheduler import setup_scheduler

    scheduler = await setup_scheduler(object())
    assert scheduler.get_job("daily_cheapest_channels") is not None
    assert scheduler.get_job("retry_missed_daily_channels") is not None
    assert scheduler.get_job("recover_daily_channels_on_startup") is not None
