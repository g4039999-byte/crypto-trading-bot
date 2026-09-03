import unittest
from unittest import mock

from src.stocks.features import compute_features
from src.stocks.regime import classify_regime, compute_regime_series, current_regime, risk_multiplier
from tests.stocks.helpers import downtrend_bars, flat_bars, make_bars, uptrend_bars


class TestClassifyRegime(unittest.TestCase):
    def test_bullish_low_vol_risk_on(self):
        regime = classify_regime({"pct_change_20d": 5.0, "above_sma50": True, "atr_pct": 0.5})
        self.assertEqual(regime["trend"], "BULLISH")
        self.assertEqual(regime["volatility"], "LOW")
        self.assertEqual(regime["risk_appetite"], "risk-on")

    def test_bearish_is_always_risk_off_even_if_calm(self):
        regime = classify_regime({"pct_change_20d": -5.0, "above_sma50": False, "atr_pct": 0.3})
        self.assertEqual(regime["trend"], "BEARISH")
        self.assertEqual(regime["risk_appetite"], "risk-off")

    def test_high_volatility_forces_risk_off_even_in_an_uptrend(self):
        regime = classify_regime({"pct_change_20d": 5.0, "above_sma50": True, "atr_pct": 5.0})
        self.assertEqual(regime["trend"], "BULLISH")
        self.assertEqual(regime["risk_appetite"], "risk-off")

    def test_missing_data_defaults_to_the_cautious_reading(self):
        regime = classify_regime({})
        self.assertEqual(regime["trend"], "SIDEWAYS")
        self.assertEqual(regime["volatility"], "HIGH")
        self.assertEqual(regime["risk_appetite"], "risk-off")


class TestRiskMultiplier(unittest.TestCase):
    def test_full_size_when_risk_on(self):
        self.assertEqual(risk_multiplier({"risk_appetite": "risk-on"}), 1.0)

    def test_reduced_size_when_risk_off(self):
        self.assertLess(risk_multiplier({"risk_appetite": "risk-off"}), 1.0)


class TestComputeRegimeSeries(unittest.TestCase):
    """Must match classify_regime(compute_features(window)) at every
    sampled date -- it's a vectorized reimplementation of the exact
    same rule, not a different, faster-but-different classification.
    """

    def _assert_matches_reference_at(self, df, index_positions):
        series = compute_regime_series(df)
        for i in index_positions:
            window = df.iloc[: i + 1]
            expected = classify_regime(compute_features(window))
            row = series.iloc[i]
            self.assertEqual(row["trend"], expected["trend"], f"trend mismatch at row {i}")
            self.assertEqual(row["volatility"], expected["volatility"], f"volatility mismatch at row {i}")
            self.assertEqual(row["risk_appetite"], expected["risk_appetite"], f"risk_appetite mismatch at row {i}")

    def test_matches_the_reference_implementation_on_an_uptrend(self):
        df = uptrend_bars(n=100, daily_gain_pct=0.5)
        self._assert_matches_reference_at(df, [60, 80, 99])

    def test_matches_the_reference_implementation_on_a_downtrend(self):
        df = downtrend_bars(n=100, daily_loss_pct=0.5)
        self._assert_matches_reference_at(df, [60, 80, 99])

    def test_matches_the_reference_implementation_on_a_flat_market(self):
        df = flat_bars(n=100)
        self._assert_matches_reference_at(df, [60, 80, 99])

    def test_early_rows_with_insufficient_history_default_to_the_cautious_reading(self):
        df = uptrend_bars(n=100, daily_gain_pct=0.5)
        series = compute_regime_series(df)
        self.assertEqual(series.iloc[5]["trend"], "SIDEWAYS")
        self.assertEqual(series.iloc[5]["volatility"], "HIGH")

    def test_empty_dataframe_returns_an_empty_series_not_a_crash(self):
        import pandas as pd
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        series = compute_regime_series(empty_df)
        self.assertTrue(series.empty)


class TestCurrentRegime(unittest.TestCase):
    def test_never_raises_when_the_data_provider_fails(self):
        with mock.patch("src.stocks.regime.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars.side_effect = RuntimeError("boom")
            regime = current_regime("SPY")
        self.assertEqual(regime["trend"], "SIDEWAYS")
        self.assertEqual(regime["symbol"], "SPY")


if __name__ == "__main__":
    unittest.main()
