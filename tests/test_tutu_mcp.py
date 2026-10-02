import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.handlers.tutu import _carousel_keyboard, _format_offer, _sort_offers
from bot.services.tutu_mcp import CheckoutRefCache, SearchResultCache, _decode_tool_result


class TutuMCPTests(unittest.TestCase):
    def test_decodes_nested_result(self):
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=json.dumps({"result": json.dumps({"offers": [{"offer_id": "x"}]})}))],
        )
        self.assertEqual(_decode_tool_result(result)["offers"][0]["offer_id"], "x")

    def test_checkout_cache_expires(self):
        cache = CheckoutRefCache(ttl_seconds=10)
        with patch("bot.services.tutu_mcp.time.monotonic", return_value=100):
            key = cache.put({"transport": "avia"})
        with patch("bot.services.tutu_mcp.time.monotonic", return_value=105):
            self.assertEqual(cache.get(key)["transport"], "avia")
        with patch("bot.services.tutu_mcp.time.monotonic", return_value=111):
            self.assertIsNone(cache.get(key))

    def test_search_cache_expires(self):
        cache = SearchResultCache(ttl_seconds=10)
        with patch("bot.services.tutu_mcp.time.monotonic", return_value=100):
            key = cache.put({"offers": [1, 2]})
        with patch("bot.services.tutu_mcp.time.monotonic", return_value=105):
            self.assertEqual(cache.get(key)["offers"], [1, 2])
        with patch("bot.services.tutu_mcp.time.monotonic", return_value=111):
            self.assertIsNone(cache.get(key))

    def test_sort_modes_and_carousel_callbacks(self):
        offers = [
            {"offer_id": "exp-direct", "price": {"amount": 200}, "legs": [{"segments": [{}]}]},
            {"offer_id": "cheap-stop", "price": {"amount": 100}, "legs": [{"segments": [{}, {}]}]},
            {"offer_id": "cheap-direct", "price": {"amount": 100}, "legs": [{"segments": [{}]}]},
        ]
        self.assertEqual(
            [x["offer_id"] for x in _sort_offers(offers, "price")],
            ["cheap-direct", "cheap-stop", "exp-direct"],
        )
        self.assertEqual(
            [x["offer_id"] for x in _sort_offers(offers, "stops")],
            ["cheap-direct", "exp-direct", "cheap-stop"],
        )
        markup = _carousel_keyboard("abcdefghij", _sort_offers(offers, "price"), 1, "price")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
        self.assertTrue(any(value.startswith("tutupage:") for value in callbacks))
        self.assertTrue(any(value.startswith("tutusort:") for value in callbacks))
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))

    def test_offer_format_includes_grounded_fare_data(self):
        offer = {
            "price": {"amount": 6866.01, "currency": "RUB"},
            "duration_min": 220,
            "carriers": ["Победа"],
            "departure_at": "2026-10-01T18:30:00+03:00",
            "arrival_at": "2026-10-01T22:10:00+03:00",
            "legs": [{
                "from": "Москва — Шереметьево (SVO), терм. D",
                "to": "Сочи, AER",
                "segments": [{"voyage_no": "DP-6949"}],
            }],
            "variants": [{"conditions": {
                "fare_family": "Базовый",
                "baggage": {"kg": 0, "pieces": 0},
                "cabin_baggage": {"kg": None, "pieces": 1, "dimensions": "36 × 30 × 27"},
                "refundable": False,
                "changeable": False,
            }}],
        }
        text = _format_offer(offer)
        self.assertIn("6 866 руб.", text)
        self.assertIn("DP-6949", text)
        self.assertIn("Шереметьево (SVO)", text)
        self.assertIn("без багажа", text)
        self.assertIn("невозвратный", text)


if __name__ == "__main__":
    unittest.main()
