import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from bot.handlers.discover import _discovery_keyboard, _format_discovery_card
from bot.keyboards.calendar_kb import calendar_keyboard
from bot.keyboards.common_kb import persistent_menu_keyboard
from bot.services.international_discovery import country_name_ru, discover_international_offers


class DiscoveryFeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_calendar_only_contains_today_or_future_dates(self):
        markup = calendar_keyboard("discover")
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        selected_dates = [value.split(":", 2)[2] for value in callbacks if value.startswith("caldate:")]
        self.assertTrue(selected_dates)
        self.assertTrue(all(value >= date.today().isoformat() for value in selected_dates))
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))

    def test_persistent_menu_contains_new_discovery_action(self):
        markup = persistent_menu_keyboard()
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertTrue(markup.is_persistent)
        self.assertIn("🌍 Куда дешевле за границу", labels)
        self.assertIn("🏠 Главное меню", labels)

    def test_country_is_localized_and_card_warns_about_price(self):
        self.assertIn("Таиланд", country_name_ru("TH"))
        offer = {
            "price": {"amount": 12500},
            "carriers": ["Авиакомпания"],
            "departure_at": "2026-10-01T08:00:00+03:00",
            "arrival_at": "2026-10-01T16:00:00+07:00",
            "duration_min": 420,
            "legs": [{"from": "MOW", "to": "HKT", "segments": [{"voyage_no": "XX100"}]}],
            "variants": [{"conditions": {"baggage": {"pieces": 1, "kg": 23}}}],
            "_discovery": {"country": "Таиланд", "city": "Пхукет"},
        }
        text = _format_discovery_card(offer, 0, 3)
        self.assertIn("Таиланд — Пхукет", text)
        self.assertIn("Только туда", text)
        self.assertIn("может измениться", text)

    async def test_candidates_are_international_and_sorted_by_tutu_total(self):
        candidates = [
            {"destination": "AER", "price": 2000},
            {"destination": "IST", "price": 5000},
            {"destination": "HKT", "price": 7000},
        ]

        async def fake_tutu(origin, destination, departure_date, page_size=1, page=1):
            amount = 19000 if destination == "Стамбул" else 17000
            code = "IST" if destination == "Стамбул" else "HKT"
            return {
                "offers": [{
                    "offer_id": code,
                    "price": {"amount": amount, "currency": "RUB"},
                    "checkout_ref": {"transport": "avia"},
                }],
                "meta": {"to": {"name": destination}, "total_matched_exact": True},
            }

        country_codes = {"MOW": "RU", "AER": "RU", "IST": "TR", "HKT": "TH"}
        city_names = {"MOW": "Москва", "AER": "Сочи", "IST": "Стамбул", "HKT": "Пхукет"}
        progress = []

        async def on_progress(checked, total, found):
            progress.append((checked, total, found))

        source_mock = AsyncMock(return_value=candidates)
        with (
            patch(
                "bot.services.international_discovery.aviasales_api.search_destinations_on_date",
                new=source_mock,
            ),
            patch(
                "bot.services.international_discovery.city_resolver.get_country_code",
                side_effect=lambda code: country_codes.get(code),
            ),
            patch(
                "bot.services.international_discovery.city_resolver.get_city_name",
                side_effect=lambda code: city_names.get(code, code),
            ),
            patch(
                "bot.services.international_discovery.tutu_mcp.search_avia",
                new=AsyncMock(side_effect=fake_tutu),
            ),
        ):
            offers = await discover_international_offers(
                "MOW",
                "2026-10-01",
                progress_callback=on_progress,
            )

        self.assertEqual([offer["offer_id"] for offer in offers], ["HKT", "IST"])
        self.assertTrue(all(offer["_discovery"]["country_code"] != "RU" for offer in offers))
        source_mock.assert_awaited_once_with("MOW", "2026-10-01", limit=1000)
        self.assertEqual(progress[0], (0, 2, 0))
        self.assertEqual(progress[-1], (2, 2, 2))
        markup = _discovery_keyboard("abcdefghij", offers, 0)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))


if __name__ == "__main__":
    unittest.main()
