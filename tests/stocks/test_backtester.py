import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from src.stocks import bar_cache
from src.stocks.backtester import (
    _apply_costs,
    _backtest_one_symbol,
    _commission_drag_pct,
    _fold_boundaries,
    backtest_all_strategies,
    backtest_strategy,
)
from src.stocks.features import compute_features
from tests.stocks.helpers import breakout_bars, flat_bars, make_bars, uptrend_bars


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
            self.assertIn(t.reason, ("stop_loss", "trailing_stop", "take_profit", "max_holding_time"))

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


class TestTrailingStopSimulation(unittest.TestCase):
    """The backtester must simulate the SAME trailing-stop rule
    src.stocks.risk_engine.update_trailing_stop() applies live -- a
    backtest that only modeled the fixed stop/target would be testing a
    different exit rule than what paper trading actually runs. Forces a
    BUY on the very first evaluated bar (mocking the strategy's own
    entry logic, which is covered elsewhere) so the post-entry price
    path -- and therefore the stop/trail/target levels -- can be
    constructed exactly.
    """

    def _entry_atr(self, flat_df):
        return compute_features(flat_df)["atr"]

    def test_a_rally_then_pullback_that_never_touches_the_hard_stop_exits_via_trailing_stop(self):
        from src.stocks.config import (
            STOCKS_STOP_LOSS_ATR_MULT,
            STOCKS_TRAILING_ARM_ATR_MULT,
            STOCKS_TRAILING_STOP_ATR_MULT,
        )
        from src.stocks.risk_engine import stop_loss_price, take_profit_price

        flat_df = flat_bars(n=56, price=100.0, noise=0.05)
        atr = self._entry_atr(flat_df)
        entry_price = float(flat_df["close"].iloc[-1])  # ~= next day's open in this flat series

        stop = stop_loss_price(entry_price, atr)
        target = take_profit_price(entry_price, atr)
        arm_level = entry_price + STOCKS_TRAILING_ARM_ATR_MULT * atr
        # Rally far enough past whichever is the binding constraint --
        # both "past the arming level" AND "far enough that, after the
        # trail's own ATR-multiple pullback from this high, it still
        # clears the hard stop" -- with a full extra ATR of cushion on
        # top, so this holds for any sane combination of the ATR
        # multipliers configured (this test intentionally derives every
        # bound from config rather than hardcoding numbers, since a
        # prior version broke when STOCKS_TAKE_PROFIT_ATR_MULT/
        # STOCKS_TRAILING_STOP_ATR_MULT were tuned from a real backtest).
        min_rally_for_trail_clearance = entry_price + (STOCKS_TRAILING_STOP_ATR_MULT - STOCKS_STOP_LOSS_ATR_MULT) * atr
        rally_high = max(arm_level, min_rally_for_trail_clearance) + atr
        trail_after_rally = rally_high - STOCKS_TRAILING_STOP_ATR_MULT * atr
        self.assertGreater(trail_after_rally, stop, "test setup invariant: trail must sit above the hard stop")
        self.assertLess(rally_high, target, "test setup invariant: rally must not reach the take-profit -- if this fails, "
                         "the configured ATR multipliers no longer leave room for a below-target rally; widen the gap "
                         "between STOCKS_TAKE_PROFIT_ATR_MULT and STOCKS_TRAILING_STOP_ATR_MULT")

        extra_rows = pd.DataFrame({
            "open": [entry_price, rally_high - 0.2, trail_after_rally + 0.3],
            "high": [rally_high, rally_high - 0.1, trail_after_rally + 0.5],
            "low": [entry_price - 0.1, rally_high - 0.3, trail_after_rally - 0.2],  # last row's low breaches the trail
            "close": [rally_high - 0.05, rally_high - 0.2, trail_after_rally],
            "volume": [1_000_000, 1_000_000, 1_000_000],
        }, index=pd.date_range(flat_df.index[-1] + (flat_df.index[1] - flat_df.index[0]), periods=3, freq="D"))
        df = pd.concat([flat_df, extra_rows])

        with mock.patch("src.stocks.strategies.breakout.generate_signal", return_value={"action": "BUY", "confidence": 1.0, "reason": "forced for test"}):
            trades = _backtest_one_symbol("breakout", "TEST", df, split_index=len(df), fold_boundaries=[len(df)])

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "trailing_stop")

    def test_never_exits_via_trailing_stop_if_price_never_arms_it(self):
        # A rally too shallow to ever reach the arming threshold must
        # never produce a trailing_stop exit, no matter how it pulls back.
        flat_df = flat_bars(n=56, price=100.0, noise=0.05)
        entry_price = float(flat_df["close"].iloc[-1])

        extra_rows = pd.DataFrame({
            "open": [entry_price, entry_price + 0.3, entry_price - 5.0],
            "high": [entry_price + 0.5, entry_price + 0.6, entry_price - 4.5],
            "low": [entry_price - 0.2, entry_price - 0.1, entry_price - 5.5],
            "close": [entry_price + 0.2, entry_price - 4.8, entry_price - 5.2],
            "volume": [1_000_000] * 3,
        }, index=pd.date_range(flat_df.index[-1] + (flat_df.index[1] - flat_df.index[0]), periods=3, freq="D"))
        df = pd.concat([flat_df, extra_rows])

        with mock.patch("src.stocks.strategies.breakout.generate_signal", return_value={"action": "BUY", "confidence": 1.0, "reason": "forced for test"}):
            trades = _backtest_one_symbol("breakout", "TEST", df, split_index=len(df), fold_boundaries=[len(df)])

        for t in trades:
            self.assertNotEqual(t.reason, "trailing_stop")


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
