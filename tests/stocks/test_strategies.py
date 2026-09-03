import unittest

from src.stocks.features import compute_features
from src.stocks.strategies import breakout, evaluate_all, mean_reversion, momentum, vwap_reclaim
from tests.stocks.helpers import breakout_bars, downtrend_bars, flat_bars, make_bars, uptrend_bars


class TestMomentum(unittest.TestCase):
    def test_buys_a_confirmed_uptrend_with_volume(self):
        df = uptrend_bars(n=80, daily_gain_pct=0.4, volume=2_000_000)
        df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000  # today's volume spikes above the trailing average
        features = compute_features(df)
        signal = momentum.generate_signal(features, df)
        self.assertEqual(signal["action"], "BUY")
        self.assertGreater(signal["confidence"], 0)

    def test_skips_a_flat_market(self):
        df = flat_bars(n=80)
        features = compute_features(df)
        signal = momentum.generate_signal(features, df)
        self.assertEqual(signal["action"], "SKIP")

    def test_skips_a_downtrend(self):
        df = downtrend_bars(n=80)
        features = compute_features(df)
        signal = momentum.generate_signal(features, df)
        self.assertEqual(signal["action"], "SKIP")

    def test_never_raises_on_missing_features(self):
        signal = momentum.generate_signal({}, None)
        self.assertEqual(signal["action"], "SKIP")


class TestBreakout(unittest.TestCase):
    def test_buys_a_high_volume_breakout(self):
        df = breakout_bars(n=80, breakout_pct=10.0, breakout_volume_mult=4.0)
        features = compute_features(df)
        signal = breakout.generate_signal(features, df)
        self.assertEqual(signal["action"], "BUY")

    def test_skips_a_breakout_without_volume_confirmation(self):
        df = breakout_bars(n=80, breakout_pct=10.0, breakout_volume_mult=1.0)
        features = compute_features(df)
        signal = breakout.generate_signal(features, df)
        self.assertEqual(signal["action"], "SKIP")
        self.assertIn("volume", signal["reason"])

    def test_skips_when_not_near_a_high(self):
        df = flat_bars(n=80)
        features = compute_features(df)
        signal = breakout.generate_signal(features, df)
        self.assertEqual(signal["action"], "SKIP")


class TestMeanReversion(unittest.TestCase):
    def test_buys_an_oversold_pullback_in_an_uptrend(self):
        # A long, gentle rally (so the 50d average lags well behind
        # price) followed by a real multi-day decline -- see
        # src/stocks/strategies/mean_reversion.py's own comment on why
        # this needs several days, not one, to be RSI-oversold *and*
        # still above SMA50 at the same time.
        closes = [100.0] * 40
        for _ in range(30):
            closes.append(closes[-1] * 1.02)
        for _ in range(10):
            closes.append(closes[-1] * 0.97)
        df = make_bars(closes)
        features = compute_features(df)
        signal = mean_reversion.generate_signal(features, df)
        self.assertEqual(signal["action"], "BUY")

    def test_skips_a_pullback_below_the_50d_average(self):
        df = downtrend_bars(n=80, daily_loss_pct=0.5)
        features = compute_features(df)
        signal = mean_reversion.generate_signal(features, df)
        self.assertEqual(signal["action"], "SKIP")
        self.assertIn("downtrend", signal["reason"])

    def test_skips_when_not_oversold(self):
        df = uptrend_bars(n=80, daily_gain_pct=0.2)
        features = compute_features(df)
        signal = mean_reversion.generate_signal(features, df)
        self.assertEqual(signal["action"], "SKIP")


class TestVwapReclaim(unittest.TestCase):
    def test_skips_with_no_intraday_data(self):
        signal = vwap_reclaim.generate_signal({}, None)
        self.assertEqual(signal["action"], "SKIP")

    def test_buys_a_reclaim_on_volume(self):
        # Below VWAP for a while, then a strong volume push back above it.
        closes = [100, 99, 98, 97, 98, 97, 96, 103]
        volumes = [1000] * 7 + [5000]
        from tests.stocks.helpers import make_bars
        df = make_bars(closes, volumes=volumes)
        signal = vwap_reclaim.generate_signal({}, df)
        self.assertEqual(signal["action"], "BUY")

    def test_skips_without_a_reclaim_pattern(self):
        df = flat_bars(n=20)
        signal = vwap_reclaim.generate_signal({}, df)
        self.assertEqual(signal["action"], "SKIP")


class TestEvaluateAll(unittest.TestCase):
    def test_returns_a_verdict_for_every_registered_strategy(self):
        df = uptrend_bars(n=80)
        features = compute_features(df)
        results = evaluate_all(features, df)
        self.assertEqual(set(results.keys()), {"momentum", "breakout", "mean_reversion", "vwap_reclaim"})

    def test_one_strategy_raising_does_not_break_the_others(self):
        import unittest.mock as mock
        with mock.patch("src.stocks.strategies.momentum.generate_signal", side_effect=RuntimeError("boom")):
            results = evaluate_all({}, None)
        self.assertEqual(results["momentum"]["action"], "SKIP")
        self.assertIn("breakout", results)


if __name__ == "__main__":
    unittest.main()
