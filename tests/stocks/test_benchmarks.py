import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stocks import bar_cache
from src.stocks.benchmarks import (
    buy_and_hold,
    simple_breakout_baseline,
    simple_momentum_baseline,
    simple_volume_baseline,
)
from tests.stocks.helpers import flat_bars, uptrend_bars


class _CacheIsolatedTestCase(unittest.TestCase):
    """Same reasoning as tests/stocks/test_backtester.py's version --
    benchmarks.py now routes through the real disk-backed bar_cache too.
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(bar_cache, "CACHE_DIR", Path(self._tmp_dir.name))
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        self._tmp_dir.cleanup()


class TestBuyAndHold(_CacheIsolatedTestCase):
    def test_one_pnl_per_symbol_matching_first_to_last_close(self):
        df = uptrend_bars(n=80, daily_gain_pct=1.0)
        expected = (float(df["close"].iloc[-1]) - float(df["close"].iloc[0])) / float(df["close"].iloc[0]) * 100
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"UP": df}
            pnls = buy_and_hold(["UP"])
        self.assertEqual(len(pnls), 1)
        self.assertAlmostEqual(pnls[0], expected)

    def test_symbols_with_too_little_data_are_skipped(self):
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"EMPTY": flat_bars(n=1)}
            pnls = buy_and_hold(["EMPTY"])
        self.assertEqual(pnls, [])


class TestSimpleMomentumBaseline(_CacheIsolatedTestCase):
    def test_never_raises_on_a_flat_market(self):
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"FLAT": flat_bars(n=100)}
            pnls = simple_momentum_baseline(["FLAT"])
        self.assertIsInstance(pnls, list)

    def test_produces_trades_on_a_dip_then_a_cross_back_above_sma50(self):
        # simple_momentum_baseline only fires on an actual crossing event
        # (yesterday <= sma50, today > sma50) -- a series that is *already*
        # above its SMA50 from bar one (like a pure uptrend_bars()) never
        # crosses, it's simply always above. Build a decline (drags price
        # and the average down together) followed by a sharp rally so price
        # pulls decisively back above its own (lagging) 50d average.
        from tests.stocks.helpers import downtrend_bars, make_bars

        down = downtrend_bars(n=60, daily_loss_pct=1.0)
        last_close = float(down["close"].iloc[-1])
        rally_closes = [last_close * (1.03 ** i) for i in range(1, 40)]
        rally = make_bars(rally_closes, start=down.index[-1] + (down.index[1] - down.index[0]))
        import pandas as pd

        df = pd.concat([down, rally])
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"UP": df}
            pnls = simple_momentum_baseline(["UP"], hold_days=5)
        self.assertGreater(len(pnls), 0)


class TestSimpleVolumeBaseline(_CacheIsolatedTestCase):
    def test_never_raises_with_no_volume_spikes(self):
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"FLAT": flat_bars(n=100)}
            pnls = simple_volume_baseline(["FLAT"])
        self.assertEqual(pnls, [])

    def test_a_volume_spike_produces_a_trade(self):
        df = flat_bars(n=100)
        df.iloc[60, df.columns.get_loc("volume")] = 10_000_000  # far above the 20d average
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"SPIKE": df}
            pnls = simple_volume_baseline(["SPIKE"], hold_days=5, rvol_threshold=2.0)
        self.assertEqual(len(pnls), 1)


class TestSimpleBreakoutBaseline(_CacheIsolatedTestCase):
    def test_never_raises_on_a_flat_market(self):
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"FLAT": flat_bars(n=100)}
            pnls = simple_breakout_baseline(["FLAT"])
        self.assertIsInstance(pnls, list)

    def test_a_new_high_with_no_volume_confirmation_still_produces_a_trade(self):
        from tests.stocks.helpers import make_bars

        # A flat run, then a push to a new high with room left afterward
        # to actually hold hold_days bars -- breakout_bars() puts its
        # spike on the very last bar, leaving no room to hold, so this
        # builds the scenario directly instead. No volume spike at all
        # (constant volume throughout), which would fail
        # src.stocks.strategies.breakout's own volume filter but must
        # still trigger this naive, volume-blind baseline.
        closes = [100.0] * 60 + [112.0] + [113.0] * 20
        df = make_bars(closes)
        with mock.patch("src.stocks.benchmarks.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"BRK": df}
            pnls = simple_breakout_baseline(["BRK"], hold_days=5)
        self.assertEqual(len(pnls), 1)


if __name__ == "__main__":
    unittest.main()
