import unittest
from unittest import mock

from src.stocks.discovery import passes_first_filter, scan_universe
from tests.stocks.helpers import make_bars, uptrend_bars


class TestPassesFirstFilter(unittest.TestCase):
    def test_rejects_missing_price(self):
        ok, reason = passes_first_filter({"price": None, "atr_pct": 3.0})
        self.assertFalse(ok)

    def test_rejects_price_out_of_range(self):
        ok, _ = passes_first_filter({"price": 1.0, "atr_pct": 3.0})
        self.assertFalse(ok)

    def test_rejects_too_quiet(self):
        ok, reason = passes_first_filter({"price": 100.0, "atr_pct": 0.01})
        self.assertFalse(ok)

    def test_rejects_too_wild(self):
        ok, reason = passes_first_filter({"price": 100.0, "atr_pct": 50.0})
        self.assertFalse(ok)

    def test_accepts_a_reasonable_candidate(self):
        ok, reason = passes_first_filter({"price": 100.0, "atr_pct": 3.0, "spread_pct": 1.0})
        self.assertTrue(ok)
        self.assertIsNone(reason)


class TestScanUniverse(unittest.TestCase):
    def test_filters_out_symbols_with_too_little_history(self):
        with mock.patch("src.stocks.discovery.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {
                "TOOSHORT": uptrend_bars(n=10),
            }
            result = scan_universe(symbols=["TOOSHORT"])
        self.assertEqual(result, {})

    def test_passes_a_qualifying_symbol_through_with_its_df_and_features(self):
        with mock.patch("src.stocks.discovery.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {
                "GOOD": uptrend_bars(n=80, daily_gain_pct=0.4),
            }
            result = scan_universe(symbols=["GOOD"])
        self.assertIn("GOOD", result)
        self.assertIn("features", result["GOOD"])
        self.assertIn("df", result["GOOD"])

    def test_a_symbol_that_fails_the_filter_is_excluded_not_fatal(self):
        # Truly near-zero high/low padding (not just make_bars()'s
        # default $0.5, which alone would already clear the ATR floor
        # regardless of close-to-close noise) -- genuinely too quiet to trade.
        near_zero_atr = make_bars([100.0 + i * 0.0001 for i in range(80)], high_pad=0.001, low_pad=0.001)
        with mock.patch("src.stocks.discovery.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {
                "FLAT": near_zero_atr,
                "GOOD": uptrend_bars(n=80, daily_gain_pct=0.4),
            }
            result = scan_universe(symbols=["FLAT", "GOOD"])
        self.assertNotIn("FLAT", result)
        self.assertIn("GOOD", result)

    def test_a_batch_fetch_failure_returns_no_candidates_not_a_crash(self):
        with mock.patch("src.stocks.discovery.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.side_effect = RuntimeError("data provider down")
            result = scan_universe(symbols=["AAPL"])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
