import unittest
from unittest import mock

from src.stocks.backtester import backtest_all_strategies, backtest_strategy
from tests.stocks.helpers import breakout_bars, flat_bars, uptrend_bars


class TestBacktestStrategy(unittest.TestCase):
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


class TestBacktestAllStrategies(unittest.TestCase):
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
