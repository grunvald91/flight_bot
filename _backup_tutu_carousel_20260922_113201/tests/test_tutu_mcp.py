import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.handlers.tutu import _format_offer
from bot.services.tutu_mcp import CheckoutRefCache, _decode_tool_result


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
