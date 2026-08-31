import unittest
from unittest import mock

from src.observation import analyze_observation


class TestAnalyzeObservation(unittest.TestCase):
    def test_insufficient_data_with_fewer_than_two_snapshots(self):
        with mock.patch("src.observation.load_snapshots", return_value=[{"price_usd": "1"}]):
            result = analyze_observation("token-a")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_strong_trend_on_rising_price_and_buy_pressure(self):
        history = [
            {"price_usd": "1.00", "liquidity_usd": 10000, "buys_24h": 100, "sells_24h": 100},
            {"price_usd": "1.10", "liquidity_usd": 11000, "buys_24h": 130, "sells_24h": 105},
        ]
        with mock.patch("src.observation.load_snapshots", return_value=history):
            result = analyze_observation("token-a")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["trend"], "STRONG")
        self.assertEqual(result["new_buys"], 30)
        self.assertEqual(result["new_sells"], 5)

    def test_weak_trend_on_falling_price(self):
        history = [
            {"price_usd": "1.00", "liquidity_usd": 10000, "buys_24h": 100, "sells_24h": 100},
            {"price_usd": "0.80", "liquidity_usd": 8000, "buys_24h": 100, "sells_24h": 100},
        ]
        with mock.patch("src.observation.load_snapshots", return_value=history):
            result = analyze_observation("token-a")
        self.assertEqual(result["trend"], "WEAK")

    def test_malformed_price_does_not_raise(self):
        history = [
            {"price_usd": "not-a-number", "liquidity_usd": 10000, "buys_24h": 1, "sells_24h": 1},
            {"price_usd": None, "liquidity_usd": None, "buys_24h": 2, "sells_24h": 1},
        ]
        with mock.patch("src.observation.load_snapshots", return_value=history):
            result = analyze_observation("token-a")
        self.assertEqual(result["status"], "OK")

    def test_load_snapshots_failure_returns_error_status(self):
        with mock.patch("src.observation.load_snapshots", side_effect=RuntimeError("boom")):
            result = analyze_observation("token-a")
        self.assertEqual(result["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
