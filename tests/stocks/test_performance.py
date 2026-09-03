import unittest

from src.stocks.performance import compute_metrics


class TestComputeMetrics(unittest.TestCase):
    def test_empty_list_returns_safe_defaults_not_a_crash(self):
        metrics = compute_metrics([])
        self.assertEqual(metrics["trade_count"], 0)
        self.assertIsNone(metrics["win_rate_pct"])

    def test_win_rate_and_pnl_split(self):
        metrics = compute_metrics([5.0, -2.0, 3.0, -1.0])
        self.assertEqual(metrics["trade_count"], 4)
        self.assertEqual(metrics["win_rate_pct"], 50.0)
        self.assertAlmostEqual(metrics["avg_win_pct"], 4.0)
        self.assertAlmostEqual(metrics["avg_loss_pct"], -1.5)

    def test_total_return_is_a_sum_not_a_compounded_product(self):
        # This is the exact bug this module documents fixing: naive
        # compounding of many *parallel* (not sequential) trades
        # produces an absurd exponential number -- see the module
        # docstring. A plain sum of 47 similarly-sized independent bets
        # must stay a sane, bounded-looking figure.
        pnl_pcts = [150.0] * 47  # e.g. every symbol in a bull-market buy&hold backtest
        metrics = compute_metrics(pnl_pcts)
        self.assertAlmostEqual(metrics["total_return_pct"], 150.0 * 47)
        self.assertLess(metrics["total_return_pct"], 1e6)  # sane order of magnitude, not 10^16

    def test_profit_factor_is_gross_profit_over_gross_loss(self):
        metrics = compute_metrics([10.0, 10.0, -5.0])
        self.assertAlmostEqual(metrics["profit_factor"], 20.0 / 5.0)

    def test_profit_factor_is_infinite_string_with_no_losses(self):
        metrics = compute_metrics([5.0, 3.0])
        self.assertEqual(metrics["profit_factor"], float("inf"))

    def test_sharpe_and_sortino_need_at_least_two_trades(self):
        self.assertIsNone(compute_metrics([5.0])["sharpe"])
        self.assertIsNotNone(compute_metrics([5.0, -2.0, 3.0])["sharpe"])

    def test_max_drawdown_on_the_cumulative_sum_curve(self):
        # +10, +10, -25, +5 -- cumulative: 10, 20, -5, 0 -- peak 20, trough -5 -> dd = 25
        metrics = compute_metrics([10.0, 10.0, -25.0, 5.0])
        self.assertAlmostEqual(metrics["max_drawdown_pct"], 25.0)

    def test_a_strategy_that_beats_a_baseline_is_detectable_by_every_metric(self):
        strong = compute_metrics([8.0, 7.0, -3.0, 6.0, -2.0])
        weak = compute_metrics([1.0, -1.0, -0.5, -1.0, 0.5])
        self.assertGreater(strong["win_rate_pct"], weak["win_rate_pct"])
        self.assertGreater(strong["expectancy_pct"], weak["expectancy_pct"])
        self.assertGreater(strong["profit_factor"], weak["profit_factor"])


if __name__ == "__main__":
    unittest.main()
