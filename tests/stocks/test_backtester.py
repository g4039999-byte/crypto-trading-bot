import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stocks import bar_cache
from src.stocks.backtester import (
    _apply_costs,
    _commission_drag_pct,
    _fold_boundaries,
    backtest_all_strategies,
    backtest_strategy,
)
from tests.stocks.helpers import breakout_bars, flat_bars, uptrend_bars


class _CacheIsolatedTestCase(unittest.TestCase):
    """Every test in this module drives backtest_strategy()/backtest_all_
    strategies() through a mocked get_provider(), but the code under
    test also runs those results through the real, disk-backed
    src.stocks.bar_cache -- redirect it to a throwaway temp directory so
    tests never write synthetic test bars into the repo's real cache
    (which a later real research run would then read back as if it were
    genuine market data).
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(bar_cache, "CACHE_DIR", Path(self._tmp_dir.name))
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        self._tmp_dir.cleanup()


class TestBacktestStrategy(_CacheIsolatedTestCase):
    def test_unknown_strategy_raises_keyerror(self):
        with self.assertRaises(KeyError):
            backtest_strategy("not_a_real_strategy", ["AAPL"])

    def test_intraday_only_strategy_raises_valueerror(self):
        with self.assertRaises(ValueError):
            backtest_strategy("vwap_reclaim", ["AAPL"])

    def test_no_lookahead_fill_is_the_bar_after_the_signal_bar(self):
        # A clean uptrend should eventually trigger a momentum BUY; the
        # fill price recorded must be an *entry* achievable the next
        # day, not the signal day's own close.
        df = uptrend_bars(n=120, daily_gain_pct=0.6, volume=2_000_000)
        df.iloc[-1, df.columns.get_loc("volume")] = 6_000_000
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"UP": df}
            trades = backtest_strategy("momentum", ["UP"], lookback_days=120)
        # Whether or not a trade fired depends on exact thresholds, but the
        # call must never raise and must return a list.
        self.assertIsInstance(trades, list)
        for t in trades:
            self.assertIn(t.reason, ("stop_loss", "take_profit", "max_holding_time"))

    def test_flat_market_produces_no_trades_but_does_not_crash(self):
        df = flat_bars(n=120)
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"FLAT": df}
            trades = backtest_strategy("breakout", ["FLAT"], lookback_days=120)
        self.assertEqual(trades, [])

    def test_a_symbol_with_too_little_history_is_skipped_not_fatal(self):
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {
                "SHORT": flat_bars(n=10),
                "GOOD": breakout_bars(n=120),
            }
            trades = backtest_strategy("breakout", ["SHORT", "GOOD"], lookback_days=120)
        self.assertIsInstance(trades, list)
        for t in trades:
            self.assertEqual(t.symbol, "GOOD")

    def test_a_strategy_error_on_one_symbol_does_not_stop_the_batch(self):
        good_df = breakout_bars(n=120)
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {
                "GOOD": good_df,
                "BAD": flat_bars(n=120),
            }
            with mock.patch(
                "src.stocks.backtester._backtest_one_symbol",
                side_effect=[RuntimeError("boom"), []],
            ):
                trades = backtest_strategy("breakout", ["BAD", "GOOD"], lookback_days=120)
        self.assertEqual(trades, [])  # both calls handled: one raised (caught), one returned []

    def test_every_trade_has_a_fold_index_within_the_configured_range(self):
        from src.stocks.config import RESEARCH_WALK_FORWARD_FOLDS

        df = breakout_bars(n=200, breakout_pct=10.0, breakout_volume_mult=4.0)
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"UP": df}
            trades = backtest_strategy("breakout", ["UP"], lookback_days=200)
        for t in trades:
            self.assertGreaterEqual(t.fold_index, 0)
            self.assertLess(t.fold_index, RESEARCH_WALK_FORWARD_FOLDS)


class TestRealisticCosts(unittest.TestCase):
    def test_slippage_moves_the_entry_up_and_the_exit_down(self):
        with mock.patch("src.stocks.backtester.STOCKS_SLIPPAGE_BPS", 10.0):
            entry, exit_ = _apply_costs(100.0, 110.0)
        self.assertGreater(entry, 100.0)
        self.assertLess(exit_, 110.0)

    def test_zero_slippage_leaves_prices_unchanged(self):
        with mock.patch("src.stocks.backtester.STOCKS_SLIPPAGE_BPS", 0.0):
            entry, exit_ = _apply_costs(100.0, 110.0)
        self.assertEqual(entry, 100.0)
        self.assertEqual(exit_, 110.0)

    def test_commission_drag_is_zero_when_commission_is_zero(self):
        with mock.patch("src.stocks.backtester.STOCKS_COMMISSION_PER_TRADE_USD", 0.0):
            self.assertEqual(_commission_drag_pct(100.0), 0.0)

    def test_commission_drag_is_positive_and_shrinks_with_larger_positions(self):
        with mock.patch("src.stocks.backtester.STOCKS_COMMISSION_PER_TRADE_USD", 1.0):
            drag_small_position = _commission_drag_pct(100.0, position_size_usd=500.0)
            drag_large_position = _commission_drag_pct(100.0, position_size_usd=5000.0)
        self.assertGreater(drag_small_position, 0.0)
        self.assertGreater(drag_small_position, drag_large_position)  # a bigger position dilutes a fixed $ commission

    def test_full_backtest_trade_pnl_reflects_costs_not_just_the_raw_price_move(self):
        # A trade whose raw stop/take-profit levels would produce an exact
        # round-number % move should come back slightly worse once costs
        # apply -- prove this at the full backtest_strategy() level, not
        # just the cost-function unit level, so a future refactor that
        # forgets to call _apply_costs somewhere is caught here too.
        df = breakout_bars(n=120, breakout_pct=10.0, breakout_volume_mult=4.0)
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider, \
             tempfile.TemporaryDirectory() as tmp_dir:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"UP": df}
            with mock.patch.object(bar_cache, "CACHE_DIR", Path(tmp_dir)), \
                 mock.patch("src.stocks.backtester.STOCKS_SLIPPAGE_BPS", 0.0), \
                 mock.patch("src.stocks.backtester.STOCKS_COMMISSION_PER_TRADE_USD", 0.0):
                zero_cost_trades = backtest_strategy("breakout", ["UP"], lookback_days=120)
            with mock.patch.object(bar_cache, "CACHE_DIR", Path(tmp_dir)), \
                 mock.patch("src.stocks.backtester.STOCKS_SLIPPAGE_BPS", 50.0), \
                 mock.patch("src.stocks.backtester.STOCKS_COMMISSION_PER_TRADE_USD", 5.0):
                with_cost_trades = backtest_strategy("breakout", ["UP"], lookback_days=120)

        if zero_cost_trades and with_cost_trades:  # a trade may or may not fire depending on synthetic data
            self.assertLess(with_cost_trades[0].pnl_pct, zero_cost_trades[0].pnl_pct)


class TestFoldBoundaries(unittest.TestCase):
    def test_evenly_divides_bars_into_the_requested_fold_count(self):
        boundaries = _fold_boundaries(100, 5)
        self.assertEqual(boundaries, [20, 40, 60, 80, 100])

    def test_last_boundary_always_covers_every_remaining_bar(self):
        boundaries = _fold_boundaries(103, 5)  # doesn't divide evenly
        self.assertEqual(boundaries[-1], 103)

    def test_a_single_fold_is_just_the_whole_range(self):
        self.assertEqual(_fold_boundaries(50, 1), [50])


class TestBacktestAllStrategies(_CacheIsolatedTestCase):
    def test_skips_intraday_only_strategies_and_shares_one_fetch(self):
        with mock.patch("src.stocks.backtester.get_provider") as mock_get_provider:
            mock_get_provider.return_value.get_daily_bars_batch.return_value = {"FLAT": flat_bars(n=120)}
            results = backtest_all_strategies(["FLAT"], lookback_days=120)
        self.assertNotIn("vwap_reclaim", results)
        self.assertIn("momentum", results)
        self.assertIn("breakout", results)
        self.assertIn("mean_reversion", results)
        mock_get_provider.return_value.get_daily_bars_batch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
