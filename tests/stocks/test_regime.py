import unittest
from unittest import mock

from src.stocks.regime import classify_regime, current_regime, risk_multiplier


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


class TestCurrentRegime(unittest.TestCase):
    def test_never_raises_when_the_data_provider_fails(self):
        with mock.patch("src.stocks.regime.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars.side_effect = RuntimeError("boom")
            regime = current_regime("SPY")
        self.assertEqual(regime["trend"], "SIDEWAYS")
        self.assertEqual(regime["symbol"], "SPY")


if __name__ == "__main__":
    unittest.main()
