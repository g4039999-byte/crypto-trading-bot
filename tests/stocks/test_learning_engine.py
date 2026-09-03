import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stocks import learning_engine as le


def _metrics(trade_count=25, pf=1.5, expectancy=1.0, dd=20.0):
    return {
        "trade_count": trade_count, "win_rate_pct": 55.0, "total_return_pct": 10.0,
        "avg_win_pct": 3.0, "avg_loss_pct": -2.0, "profit_factor": pf,
        "expectancy_pct": expectancy, "sharpe": 0.2, "sortino": 0.3, "max_drawdown_pct": dd,
    }


class TestLearningEngine(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(le, "LEARNING_STATE_FILE", Path(self._tmp_dir.name) / "learning_state.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    # --- gating: TIME ALONE must be sufficient to run the historical-
    # backtest-driven search (item 13's fix) -- new-trade count can only
    # make a cycle run EARLIER, never required to make one run at all. ---

    def _prime_last_run_at(self, trade_count=0):
        """Run one cycle now (so state.last_run_at is set to 'now') --
        used to set up "checked too recently" scenarios for the tests
        below. Fully offline: no real backtest/network call.
        """
        with mock.patch.object(le, "load_state", return_value={"closed_trades": [{"pnl_pct": 1.0}] * trade_count}), \
             mock.patch.object(le, "get_active_strategy", return_value="breakout"), \
             mock.patch.object(le, "backtest_strategy", return_value=[]):
            le.run_learning_cycle(force=False)

    def test_a_fresh_install_with_zero_live_trades_still_runs_on_the_first_call(self):
        # No prior last_run_at at all -- must NOT be blocked by having
        # zero closed paper trades. This is the exact bug item 13 reports.
        with mock.patch.object(le, "load_state", return_value={"closed_trades": []}), \
             mock.patch.object(le, "get_active_strategy", return_value="breakout"), \
             mock.patch.object(le, "backtest_strategy", return_value=[]) as mock_backtest:
            state = le.run_learning_cycle(force=False)
        mock_backtest.assert_called()  # the backtest step actually ran
        self.assertNotEqual(state["last_action"], "skipped_insufficient_data")

    def test_enough_time_passed_runs_even_with_zero_new_trades(self):
        self._prime_last_run_at(trade_count=0)
        with mock.patch.object(le, "load_state", return_value={"closed_trades": []}), \
             mock.patch.object(le, "_seconds_since", return_value=99999999), \
             mock.patch.object(le, "get_active_strategy", return_value="breakout"), \
             mock.patch.object(le, "backtest_strategy", return_value=[]) as mock_backtest:
            state = le.run_learning_cycle(force=False)
        mock_backtest.assert_called()
        self.assertNotEqual(state["last_action"], "skipped_insufficient_data")

    def test_enough_new_trades_runs_even_though_checked_recently(self):
        self._prime_last_run_at(trade_count=0)
        with mock.patch.object(le, "load_state", return_value={"closed_trades": [{"pnl_pct": 1.0}] * 30}), \
             mock.patch.object(le, "get_active_strategy", return_value="breakout"), \
             mock.patch.object(le, "backtest_strategy", return_value=[]) as mock_backtest:
            state = le.run_learning_cycle(force=False)  # 30 new trades, but almost no time has passed
        mock_backtest.assert_called()
        self.assertNotEqual(state["last_action"], "skipped_insufficient_data")

    def test_skips_only_when_neither_enough_time_nor_enough_new_trades(self):
        self._prime_last_run_at(trade_count=0)
        with mock.patch.object(le, "load_state", return_value={"closed_trades": [{"pnl_pct": 1.0}] * 3}), \
             mock.patch.object(le, "backtest_strategy") as mock_backtest:
            state = le.run_learning_cycle(force=False)  # too little time AND too few new trades
        mock_backtest.assert_not_called()
        self.assertEqual(state["last_action"], "skipped_insufficient_data")

    def test_force_bypasses_both_gates(self):
        with mock.patch.object(le, "load_state", return_value={"closed_trades": []}), \
             mock.patch.object(le, "get_active_strategy", return_value=None), \
             mock.patch.object(le, "backtest_strategy", return_value=[]):
            state = le.run_learning_cycle(force=True)
        self.assertNotEqual(state["last_action"], None)

    # --- dominance rule ---

    def test_dominates_requires_the_significance_floor(self):
        candidate = _metrics(trade_count=5, pf=5.0, expectancy=5.0)  # huge edge, but too few trades
        active = _metrics(trade_count=100, pf=1.0, expectancy=0.1)
        self.assertFalse(le._dominates(candidate, active))

    def test_dominates_requires_a_real_pf_margin_not_a_nominal_tick(self):
        active = _metrics(pf=1.5)
        barely_better = _metrics(pf=1.55)  # smaller than STOCKS_LEARNING_MIN_PF_IMPROVEMENT
        self.assertFalse(le._dominates(barely_better, active))

    def test_dominates_rejects_a_candidate_with_worse_expectancy(self):
        active = _metrics(pf=1.5, expectancy=1.0)
        candidate = _metrics(pf=2.0, expectancy=-1.0)  # great PF, terrible expectancy
        self.assertFalse(le._dominates(candidate, active))

    def test_dominates_rejects_a_candidate_with_dramatically_worse_drawdown(self):
        active = _metrics(pf=1.5, dd=10.0)
        candidate = _metrics(pf=2.0, dd=50.0)  # 5x the drawdown
        self.assertFalse(le._dominates(candidate, active))

    def test_dominates_accepts_a_genuinely_better_candidate(self):
        active = _metrics(pf=1.2, expectancy=0.5, dd=30.0)
        candidate = _metrics(pf=1.8, expectancy=1.0, dd=25.0)
        self.assertTrue(le._dominates(candidate, active))

    # --- rollback ---

    def test_rolls_back_when_active_strategys_own_live_trades_turn_negative(self):
        losing_trades = [{"strategy": "momentum", "pnl_pct": -2.0}] * 25
        with mock.patch.object(le, "load_state", return_value={"closed_trades": losing_trades}), \
             mock.patch.object(le, "get_active_strategy", return_value="momentum"), \
             mock.patch.object(le, "get_previous_strategy", return_value="breakout"), \
             mock.patch.object(le, "activate_strategy") as mock_activate:
            state = le.run_learning_cycle(force=True)
        mock_activate.assert_called_once_with("breakout")
        self.assertEqual(state["last_action"], "rolled_back")

    def test_does_not_roll_back_below_the_minimum_live_sample(self):
        losing_trades = [{"strategy": "momentum", "pnl_pct": -2.0}] * 3  # below STOCKS_LEARNING_ROLLBACK_MIN_TRADES
        with mock.patch.object(le, "load_state", return_value={"closed_trades": losing_trades}), \
             mock.patch.object(le, "get_active_strategy", return_value="momentum"), \
             mock.patch.object(le, "activate_strategy") as mock_activate, \
             mock.patch.object(le, "backtest_strategy", return_value=[]):
            le.run_learning_cycle(force=True)
        mock_activate.assert_not_called()

    def test_does_not_roll_back_when_there_is_no_previous_strategy(self):
        losing_trades = [{"strategy": "momentum", "pnl_pct": -2.0}] * 25
        with mock.patch.object(le, "load_state", return_value={"closed_trades": losing_trades}), \
             mock.patch.object(le, "get_active_strategy", return_value="momentum"), \
             mock.patch.object(le, "get_previous_strategy", return_value=None), \
             mock.patch.object(le, "activate_strategy") as mock_activate, \
             mock.patch.object(le, "backtest_strategy", return_value=[]):
            le.run_learning_cycle(force=True)
        mock_activate.assert_not_called()

    # --- adoption ---

    def test_adopts_a_dominating_candidate_and_records_a_new_version(self):
        import src.stocks.backtester as backtester_module

        winning_trades = [backtester_module.BacktestTrade(
            symbol="X", strategy="breakout", entry_date="d", entry_price=1.0,
            pnl_pct=2.0, in_sample=False,
        ) for _ in range(25)]
        losing_active_trades = [backtester_module.BacktestTrade(
            symbol="X", strategy="momentum", entry_date="d", entry_price=1.0,
            pnl_pct=-0.5, in_sample=False,
        ) for _ in range(25)]

        def fake_backtest(name, symbols, lookback_days):
            return winning_trades if name == "breakout" else losing_active_trades

        with mock.patch.object(le, "load_state", return_value={"closed_trades": []}), \
             mock.patch.object(le, "get_active_strategy", return_value="momentum"), \
             mock.patch.object(le, "backtest_strategy", side_effect=fake_backtest), \
             mock.patch.object(le, "record_version") as mock_record_version, \
             mock.patch.object(le, "activate_strategy") as mock_activate:
            state = le.run_learning_cycle(force=True)

        mock_record_version.assert_called_once()
        mock_activate.assert_called_once_with("breakout")
        self.assertEqual(state["last_action"], "adopted")

    def test_no_change_when_nothing_beats_the_active_strategy(self):
        import src.stocks.backtester as backtester_module

        decent_trades = [backtester_module.BacktestTrade(
            symbol="X", strategy="s", entry_date="d", entry_price=1.0, pnl_pct=1.0, in_sample=False,
        ) for _ in range(25)]

        with mock.patch.object(le, "load_state", return_value={"closed_trades": []}), \
             mock.patch.object(le, "get_active_strategy", return_value="breakout"), \
             mock.patch.object(le, "backtest_strategy", return_value=decent_trades), \
             mock.patch.object(le, "record_version") as mock_record_version, \
             mock.patch.object(le, "activate_strategy") as mock_activate:
            state = le.run_learning_cycle(force=True)

        mock_record_version.assert_not_called()
        mock_activate.assert_not_called()
        self.assertEqual(state["last_action"], "no_change")

    def test_a_backtest_failure_for_one_strategy_does_not_crash_the_cycle(self):
        with mock.patch.object(le, "load_state", return_value={"closed_trades": []}), \
             mock.patch.object(le, "get_active_strategy", return_value="breakout"), \
             mock.patch.object(le, "backtest_strategy", side_effect=RuntimeError("data down")):
            state = le.run_learning_cycle(force=True)
        self.assertEqual(state["last_action"], "skipped_insufficient_data")

    def test_corrupt_learning_state_file_degrades_to_defaults(self):
        le.LEARNING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        le.LEARNING_STATE_FILE.write_text("{not valid json", encoding="utf-8")
        state = le._load_state()
        self.assertEqual(state["last_action"], None)


if __name__ == "__main__":
    unittest.main()
