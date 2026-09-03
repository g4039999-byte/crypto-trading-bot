import unittest

import pandas as pd

from src.stocks.features import atr, compute_features, relative_volume, rsi, sma, vwap
from tests.stocks.helpers import downtrend_bars, flat_bars, make_bars, uptrend_bars


class TestIndicators(unittest.TestCase):
    def test_sma_matches_manual_average(self):
        series = pd.Series([1, 2, 3, 4, 5])
        result = sma(series, 3)
        self.assertAlmostEqual(result.iloc[-1], (3 + 4 + 5) / 3)
        self.assertTrue(pd.isna(result.iloc[0]))  # not enough history yet

    def test_rsi_is_high_in_a_steady_uptrend(self):
        df = uptrend_bars(n=60)
        result = rsi(df["close"], 14)
        self.assertGreater(result.iloc[-1], 60)

    def test_rsi_is_low_in_a_steady_downtrend(self):
        df = downtrend_bars(n=60)
        result = rsi(df["close"], 14)
        self.assertLess(result.iloc[-1], 40)

    def test_atr_is_positive_and_larger_for_more_volatile_bars(self):
        calm = make_bars([100] * 30, volumes=[1000] * 30, high_pad=0.1, low_pad=0.1)
        wild = make_bars([100] * 30, volumes=[1000] * 30, high_pad=5.0, low_pad=5.0)
        self.assertGreater(atr(wild, 14).iloc[-1], atr(calm, 14).iloc[-1])
        self.assertGreater(atr(calm, 14).iloc[-1], 0)

    def test_relative_volume_flags_a_spike(self):
        volumes = [1_000_000] * 25 + [5_000_000]
        df = make_bars([100 + i * 0.1 for i in range(26)], volumes=volumes)
        result = relative_volume(df, lookback=20)
        self.assertGreater(result.iloc[-1], 3.0)

    def test_vwap_stays_within_the_cumulative_session_range(self):
        # VWAP is a cumulative average from the start of the series, so
        # it can fall outside any single *later* bar's own high/low --
        # the real invariant is that it never exceeds the running
        # min/max of the whole session up to that point.
        df = make_bars([100, 101, 99, 102, 103], volumes=[1000, 2000, 1500, 3000, 2500])
        result = vwap(df)
        running_low = df["low"].cummin()
        running_high = df["high"].cummax()
        self.assertTrue((result.dropna() >= running_low).all())
        self.assertTrue((result.dropna() <= running_high).all())


class TestComputeFeatures(unittest.TestCase):
    def test_empty_or_short_dataframe_returns_all_none(self):
        features = compute_features(pd.DataFrame())
        self.assertIsNone(features["price"])
        self.assertIsNone(features["atr"])

    def test_uptrend_features_are_internally_consistent(self):
        df = uptrend_bars(n=80)
        features = compute_features(df)
        self.assertTrue(features["above_sma20"])
        self.assertTrue(features["above_sma50"])
        self.assertGreater(features["pct_change_20d"], 0)
        self.assertIsInstance(features["above_sma20"], bool)  # not numpy.bool_ -- must be JSON-serializable

    def test_all_numeric_and_bool_fields_are_json_serializable(self):
        import json
        df = uptrend_bars(n=80)
        features = compute_features(df)
        json.dumps(features)  # raises if anything is a numpy scalar

    def test_flat_market_has_low_atr_pct(self):
        df = flat_bars(n=80, noise=0.05)
        features = compute_features(df)
        self.assertLess(features["atr_pct"], 2.0)


if __name__ == "__main__":
    unittest.main()
