import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from src.stocks import bar_cache, research_pipeline as rp
from src.stocks.backtester import BacktestTrade
from tests.stocks.helpers import breakout_bars, flat_bars, uptrend_bars


def _trade(entry_date, pnl_pct, fold_index=0, in_sample=True):
    return BacktestTrade(
        symbol="X", strategy="s", entry_date=entry_date, entry_price=100.0,
        pnl_pct=pnl_pct, fold_index=fold_index, in_sample=in_sample,
    )


class _CacheIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(bar_cache, "CACHE_DIR", Path(self._tmp_dir.name))
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        self._tmp_dir.cleanup()


class TestTagTradesWithRegime(unittest.TestCase):
    def test_tags_each_trade_with_the_asof_regime_row(self):
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        regime_series = pd.DataFrame(
            {"trend": ["BULLISH", "BULLISH", "SIDEWAYS", "SIDEWAYS", "BEARISH"],
             "volatility": ["LOW"] * 5, "risk_appetite": ["risk-on"] * 5},
            index=idx,
        )
        trades = [_trade(str(idx[1]), 5.0), _trade(str(idx[4]), -3.0)]
        rp.tag_trades_with_regime(trades, regime_series)
        self.assertEqual(trades[0].regime_trend, "BULLISH")
        self.assertEqual(trades[1].regime_trend, "BEARISH")

    def test_empty_regime_series_is_a_safe_no_op(self):
        trades = [_trade("2024-01-01", 1.0)]
        rp.tag_trades_with_regime(trades, pd.DataFrame())
        self.assertIsNone(trades[0].regime_trend)

    def test_no_trades_is_a_safe_no_op(self):
        idx = pd.date_range("2024-01-01", periods=2, freq="D")
        regime_series = pd.DataFrame({"trend": ["BULLISH"] * 2, "volatility": ["LOW"] * 2, "risk_appetite": ["risk-on"] * 2}, index=idx)
        self.assertEqual(rp.tag_trades_with_regime([], regime_series), [])


class TestFoldStabilityScore(unittest.TestCase):
    def test_all_folds_passing_scores_one(self):
        fold_metrics = {
            0: {"trade_count": 10, "expectancy_pct": 1.0, "profit_factor": 1.5},
            1: {"trade_count": 10, "expectancy_pct": 0.5, "profit_factor": 1.2},
        }
        self.assertEqual(rp._fold_stability_score(fold_metrics), 1.0)

    def test_half_failing_scores_half(self):
        fold_metrics = {
            0: {"trade_count": 10, "expectancy_pct": 1.0, "profit_factor": 1.5},
            1: {"trade_count": 10, "expectancy_pct": -1.0, "profit_factor": 0.8},
        }
        self.assertEqual(rp._fold_stability_score(fold_metrics), 0.5)

    def test_empty_folds_with_no_trades_score_zero(self):
        fold_metrics = {0: {"trade_count": 0, "expectancy_pct": None, "profit_factor": None}}
        self.assertEqual(rp._fold_stability_score(fold_metrics), 0.0)

    def test_infinite_profit_factor_counts_as_passing(self):
        fold_metrics = {0: {"trade_count": 5, "expectancy_pct": 2.0, "profit_factor": float("inf")}}
        self.assertEqual(rp._fold_stability_score(fold_metrics), 1.0)


class TestAnalyzeStrategy(unittest.TestCase):
    def test_splits_combined_in_sample_and_out_of_sample_correctly(self):
        trades = [_trade("d", 5.0, in_sample=True), _trade("d", -2.0, in_sample=False)]
        report = rp.analyze_strategy("breakout", trades, n_folds=1)
        self.assertEqual(report["combined"]["trade_count"], 2)
        self.assertEqual(report["in_sample"]["trade_count"], 1)
        self.assertEqual(report["out_of_sample"]["trade_count"], 1)

    def test_per_fold_metrics_cover_every_fold_index_present(self):
        trades = [_trade("d", 1.0, fold_index=0), _trade("d", 1.0, fold_index=2)]
        report = rp.analyze_strategy("breakout", trades, n_folds=3)
        self.assertIn("0", report["per_fold"])
        self.assertIn("2", report["per_fold"])

    def test_regime_buckets_below_the_minimum_sample_are_excluded(self):
        trades = [_trade("d", 1.0) for _ in range(3)]
        for t in trades:
            t.regime_trend, t.regime_volatility = "BULLISH", "LOW"
        report = rp.analyze_strategy("breakout", trades, n_folds=1)
        self.assertEqual(report["per_regime"], {})  # 3 < MIN_TRADES_PER_REGIME_BUCKET


class TestRankStrategies(unittest.TestCase):
    def test_significant_strategies_rank_above_insignificant_ones(self):
        strategies_report = {
            "tiny_sample": {
                "combined": {"trade_count": 2, "sharpe": 5.0},
                "out_of_sample": {"trade_count": 1, "profit_factor": 10.0, "expectancy_pct": 10.0},
                "fold_stability_score": 1.0,
            },
            "real_sample": {
                "combined": {"trade_count": 100, "sharpe": 0.3},
                "out_of_sample": {"trade_count": 30, "profit_factor": 1.3, "expectancy_pct": 0.5},
                "fold_stability_score": 0.8,
            },
        }
        baselines_report = {"buy_and_hold": {"sharpe": 0.2}}
        ranked = rp._rank_strategies(strategies_report, baselines_report)
        self.assertEqual(ranked[0]["strategy"], "real_sample")
        self.assertFalse(ranked[1]["statistically_significant"])


class TestCalmarLikeRatio(unittest.TestCase):
    def test_basic_ratio(self):
        self.assertAlmostEqual(rp._calmar_like_ratio(100.0, 20.0), 5.0)

    def test_zero_drawdown_with_a_positive_return_is_infinite(self):
        self.assertEqual(rp._calmar_like_ratio(50.0, 0.0), float("inf"))

    def test_zero_drawdown_with_no_return_is_zero(self):
        self.assertEqual(rp._calmar_like_ratio(0.0, 0.0), 0.0)

    def test_none_drawdown_is_treated_as_zero_drawdown(self):
        self.assertEqual(rp._calmar_like_ratio(50.0, None), float("inf"))

    def test_scale_invariant_across_very_different_sample_sizes(self):
        # The whole point: a strategy measured on 10x the trades (both
        # its return AND its drawdown scale up together under this
        # project's summed-return convention) must produce the SAME
        # ratio, not a worse one just because the sample got bigger.
        small_sample = rp._calmar_like_ratio(60.0, 10.0)
        large_sample = rp._calmar_like_ratio(600.0, 100.0)
        self.assertAlmostEqual(small_sample, large_sample)


class TestAssessLiveReadiness(unittest.TestCase):
    def _good_report(self):
        return {
            "combined": {"trade_count": 200, "max_drawdown_pct": 20.0, "total_return_pct": 100.0},
            "out_of_sample": {"trade_count": 50, "profit_factor": 1.8, "expectancy_pct": 1.2},
            "fold_stability_score": 0.8,
        }

    def test_a_strong_report_is_a_live_candidate(self):
        result = rp.assess_live_readiness(self._good_report())
        self.assertEqual(result["verdict"], "LIVE_CANDIDATE")
        self.assertTrue(all(c["pass"] for c in result["criteria"].values()))

    def test_too_few_trades_is_not_ready(self):
        report = self._good_report()
        report["combined"]["trade_count"] = 5
        result = rp.assess_live_readiness(report)
        self.assertEqual(result["verdict"], "NOT_READY")
        self.assertFalse(result["criteria"]["enough_combined_trades"]["pass"])

    def test_negative_out_of_sample_expectancy_is_not_ready(self):
        report = self._good_report()
        report["out_of_sample"]["expectancy_pct"] = -0.5
        result = rp.assess_live_readiness(report)
        self.assertEqual(result["verdict"], "NOT_READY")
        self.assertFalse(result["criteria"]["positive_out_of_sample_expectancy"]["pass"])

    def test_excessive_drawdown_relative_to_return_is_not_ready(self):
        # Same absolute drawdown as the "good" report, but the return
        # earned for enduring it is now tiny -- a poor risk/reward
        # trade-off even though max_drawdown_pct itself didn't change.
        report = self._good_report()
        report["combined"]["total_return_pct"] = 5.0  # only 0.25x its own drawdown
        result = rp.assess_live_readiness(report)
        self.assertEqual(result["verdict"], "NOT_READY")
        self.assertFalse(result["criteria"]["return_to_drawdown_ratio_above_threshold"]["pass"])

    def test_a_large_absolute_drawdown_is_fine_if_the_return_scales_with_it(self):
        # This is the exact real-world case this ratio-based check exists
        # for: an 11,000-trade, 10-year backtest naturally produces a
        # max_drawdown_pct over 100 in absolute terms purely from sample
        # volume (see _calmar_like_ratio()'s docstring) -- that alone
        # must not fail a strategy whose cumulative return comfortably
        # covers it.
        report = self._good_report()
        report["combined"]["max_drawdown_pct"] = 150.0
        report["combined"]["total_return_pct"] = 900.0  # 6x its own drawdown
        result = rp.assess_live_readiness(report)
        self.assertTrue(result["criteria"]["return_to_drawdown_ratio_above_threshold"]["pass"])

    def test_low_fold_stability_is_not_ready_even_with_great_aggregate_numbers(self):
        report = self._good_report()
        report["fold_stability_score"] = 0.1  # only won in one lucky fold
        result = rp.assess_live_readiness(report)
        self.assertEqual(result["verdict"], "NOT_READY")
        self.assertFalse(result["criteria"]["stable_across_walk_forward_folds"]["pass"])

    def test_never_touches_any_config_or_file(self):
        # Purely a function of its input -- no side effects at all.
        with mock.patch("builtins.open", side_effect=AssertionError("must not touch the filesystem")):
            rp.assess_live_readiness(self._good_report())


class TestParameterSensitivityCheck(_CacheIsolatedTestCase):
    def test_restores_original_module_attributes_even_after_a_failure(self):
        from src.stocks.strategies import breakout as breakout_module
        original_threshold = breakout_module.MIN_RELATIVE_VOLUME

        with mock.patch("src.stocks.research_pipeline.backtest_strategy", side_effect=RuntimeError("boom")):
            results = rp.parameter_sensitivity_check(
                "breakout", {"MIN_RELATIVE_VOLUME": [1.2, 2.0]}, ["AAPL"], 120, n_folds=2,
            )

        self.assertEqual(breakout_module.MIN_RELATIVE_VOLUME, original_threshold)  # restored, not left mutated
        self.assertEqual(len(results), 2)  # one result per combo, even though every backtest failed

    def test_ranks_by_stability_before_raw_profit_factor(self):
        def fake_backtest(strategy_name, symbols, lookback_days, n_folds=None):
            from src.stocks.strategies import breakout as breakout_module
            if breakout_module.MIN_RELATIVE_VOLUME == 1.2:
                # "unstable": huge aggregate PF, but only fold 0 has trades
                return [BacktestTrade(symbol="X", strategy="breakout", entry_date="d", entry_price=1.0, pnl_pct=50.0, fold_index=0)] * 5
            # "stable": modest PF, but present (and winning) across both folds
            return (
                [BacktestTrade(symbol="X", strategy="breakout", entry_date="d", entry_price=1.0, pnl_pct=2.0, fold_index=0)] * 5
                + [BacktestTrade(symbol="X", strategy="breakout", entry_date="d", entry_price=1.0, pnl_pct=2.0, fold_index=1)] * 5
            )

        with mock.patch("src.stocks.research_pipeline.backtest_strategy", side_effect=fake_backtest):
            results = rp.parameter_sensitivity_check(
                "breakout", {"MIN_RELATIVE_VOLUME": [1.2, 2.0]}, ["AAPL"], 120, n_folds=2,
            )

        self.assertEqual(results[0]["params"]["MIN_RELATIVE_VOLUME"], 2.0)  # stable one ranked first


class TestRunResearch(_CacheIsolatedTestCase):
    def _patch_all_providers(self, fake_batch):
        # research_pipeline.py orchestrates backtester.py + benchmarks.py,
        # each of which imports its own `get_provider` reference (`from
        # src.stocks.data_provider import get_provider`) -- all three
        # must be patched, or a baseline call silently falls through to
        # the real data provider (a real, if harmless, network call).
        return (
            mock.patch("src.stocks.research_pipeline.get_provider", **{"return_value.get_daily_bars_batch.side_effect": fake_batch}),
            mock.patch("src.stocks.backtester.get_provider", **{"return_value.get_daily_bars_batch.side_effect": fake_batch}),
            mock.patch("src.stocks.benchmarks.get_provider", **{"return_value.get_daily_bars_batch.side_effect": fake_batch}),
        )

    def test_full_pipeline_runs_end_to_end_on_synthetic_data(self):
        df = breakout_bars(n=150, breakout_pct=10.0, breakout_volume_mult=4.0)
        spy_df = flat_bars(n=150)

        def fake_batch(symbols, lookback_days):
            return {s: (spy_df if s == "SPY" else df) for s in symbols}

        p1, p2, p3 = self._patch_all_providers(fake_batch)
        with p1, p2, p3:
            report = rp.run_research(symbols=["UP"], lookback_days=150, n_folds=2)

        self.assertIn("strategies", report)
        self.assertIn("baselines", report)
        self.assertIn("ranking", report)
        self.assertIn("live_readiness", report)
        self.assertIn("survivorship_bias_disclosure", report)
        self.assertEqual(report["universe_size"], 1)
        self.assertGreaterEqual(report["total_trades_across_all_strategies"], 0)

    def test_never_calls_activate_strategy_or_record_version(self):
        df = flat_bars(n=150)

        def fake_batch(symbols, lookback_days):
            return {s: df for s in symbols}

        p1, p2, p3 = self._patch_all_providers(fake_batch)
        with p1, p2, p3, \
             mock.patch("src.stocks.strategy_registry.activate_strategy") as mock_activate, \
             mock.patch("src.stocks.strategy_registry.record_version") as mock_record:
            rp.run_research(symbols=["FLAT"], lookback_days=150, n_folds=2)
        mock_activate.assert_not_called()
        mock_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
