"""Resilience tests: a data-provider outage, X being unavailable, or
Alpaca being unconfigured must never crash a cycle -- mirrors
tests/test_radar_integration.py's resilience tests on the crypto side.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stocks import engine
from src.stocks.features import compute_features
from tests.stocks.helpers import uptrend_bars


class _EngineTestIsolation(unittest.TestCase):
    """Shared setup for every test in this module that calls a real
    run_cycle()/run_forever() -- isolates every file src.stocks.engine
    can write (paper state, paper log, AND last_cycle.json/the
    opportunities snapshot) to a throwaway temp directory. Without the
    last_cycle.json patch specifically, a real run_cycle() call in a
    test overwrites the REAL, live production file the dashboard reads
    -- this bit once (see git history): a test using synthetic "HIGH"/
    "LOW" symbols briefly showed up as a live "opportunity" on the
    actual running dashboard.
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "paper_positions.json"
        tmp_log = Path(self._tmp_dir.name) / "paper_trade_log.jsonl"
        tmp_last_cycle = Path(self._tmp_dir.name) / "last_cycle.json"
        self._patches = [
            mock.patch("src.stocks.paper_broker.STATE_FILE", tmp_state),
            mock.patch("src.stocks.paper_logger.LOG_FILE", tmp_log),
            mock.patch("src.stocks.engine.LAST_CYCLE_FILE", tmp_last_cycle),
            mock.patch("src.stocks.paper_broker.alpaca_client.submit_paper_order", return_value=None),
            # These tests exercise cycle logic, not market-hours gating,
            # health bookkeeping, or the (real-network-calling) learning
            # loop -- keep run_forever() free of all three side effects
            # here so results don't depend on the wall-clock time the
            # test suite happens to run at.
            mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", False),
            mock.patch("src.stocks.engine.health.record_success"),
            mock.patch("src.stocks.engine.health.record_failure", return_value=0),
            mock.patch("src.stocks.engine.health.record_start"),
            mock.patch("src.stocks.engine._maybe_run_learning_cycle"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()


class TestEngineResilience(_EngineTestIsolation):
    def test_x_social_signal_lookup_failure_falls_back_to_market_only(self):
        with mock.patch("src.x_intelligence.social_signal_for_token", side_effect=RuntimeError("X is down")):
            signal, bonus = engine._x_social_signal("AAPL", {"volume": 1_000_000})
        self.assertIsNone(signal)
        self.assertEqual(bonus, 0)

    def test_run_cycle_survives_regime_detection_raising_entirely(self):
        with mock.patch("src.stocks.engine.current_regime", side_effect=RuntimeError("SPY data unavailable")), \
             mock.patch("src.stocks.engine.scan_universe", return_value={}):
            summary = engine.run_cycle()
        self.assertEqual(summary["regime"]["trend"], "SIDEWAYS")

    def test_run_cycle_survives_universe_scan_raising_entirely(self):
        with mock.patch("src.stocks.engine.current_regime", return_value={"trend": "BULLISH", "risk_appetite": "risk-on", "volatility": "LOW"}), \
             mock.patch("src.stocks.engine.scan_universe", side_effect=RuntimeError("data provider down")):
            summary = engine.run_cycle()
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["buys"], 0)

    def test_run_cycle_survives_x_intelligence_raising_entirely_for_every_candidate(self):
        df = uptrend_bars(n=80, daily_gain_pct=0.6, volume=2_000_000)
        df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000
        features = compute_features(df)
        candidates = {"UP": {"features": features, "df": df}}

        with mock.patch("src.stocks.engine.current_regime", return_value={"trend": "BULLISH", "risk_appetite": "risk-on", "volatility": "LOW"}), \
             mock.patch("src.stocks.engine.scan_universe", return_value=candidates), \
             mock.patch("src.x_intelligence.social_signal_for_token", side_effect=RuntimeError("X down")):
            summary = engine.run_cycle()

        self.assertEqual(summary["candidates"], 1)  # ran to completion despite X failing

    def test_run_cycle_survives_exit_monitoring_failure(self):
        with mock.patch("src.stocks.engine.current_regime", return_value={"trend": "BULLISH", "risk_appetite": "risk-on", "volatility": "LOW"}), \
             mock.patch("src.stocks.engine.scan_universe", return_value={}), \
             mock.patch("src.stocks.engine.evaluate_exit_for_open_positions", side_effect=RuntimeError("boom")):
            summary = engine.run_cycle()
        self.assertIn("sells", summary)

    def test_run_cycle_survives_the_active_strategy_lookup_failing(self):
        df = uptrend_bars(n=80, daily_gain_pct=0.6, volume=2_000_000)
        df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000
        features = compute_features(df)
        candidates = {"UP": {"features": features, "df": df}}

        with mock.patch("src.stocks.engine.current_regime", return_value={"trend": "BULLISH", "risk_appetite": "risk-on", "volatility": "LOW"}), \
             mock.patch("src.stocks.engine.scan_universe", return_value=candidates), \
             mock.patch("src.stocks.strategy_registry.get_active_strategy", side_effect=RuntimeError("registry file corrupt")):
            summary = engine.run_cycle()
        self.assertEqual(summary["candidates"], 1)

    def test_run_forever_survives_a_cycle_raising_and_continues_to_the_next(self):
        call_count = {"n": 0}

        def fake_cycle():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first cycle blew up")
            return {"scanned": 0}

        with mock.patch("src.stocks.engine.run_cycle", side_effect=fake_cycle), mock.patch("time.sleep"):
            engine.run_forever(interval_seconds=0, max_iterations=2)
        self.assertEqual(call_count["n"], 2)  # kept going after the first cycle's exception

    def test_a_symbol_scanned_twice_in_one_cycle_only_ever_buys_once(self):
        # run_cycle() ranks candidates from a dict keyed by symbol, so a
        # symbol can never literally appear twice in one real cycle --
        # this proves the guard that matters (state is re-read after
        # every BUY, per run_cycle's own comment) actually prevents a
        # double-buy even if evaluate_entry were called twice in a row
        # for the same symbol within one cycle.
        from src.stocks.paper_broker import load_state

        df = uptrend_bars(n=80, daily_gain_pct=0.6, volume=2_000_000)
        df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000
        features = compute_features(df)
        candidate = {"features": features, "df": df}
        regime = {"trend": "BULLISH", "risk_appetite": "risk-on"}

        with mock.patch("src.stocks.strategy_registry.get_active_strategy", return_value=None):
            first = engine.evaluate_entry("DUPE", candidate, regime, load_state())
            if first["action"] == "BUY":
                engine.open_position(
                    "DUPE", features["price"], first["size_usd"], first["atr"],
                    strategy=first["strategy"], entry_score=first["score"],
                )
            second = engine.evaluate_entry("DUPE", candidate, regime, load_state())

        if first["action"] == "BUY":
            self.assertEqual(second["action"], "SKIP")
            self.assertIn("already holding", second["reason"])
        state = load_state()
        self.assertLessEqual(len([p for p in state["open_positions"] if p["symbol"] == "DUPE"]), 1)

    def test_never_holds_a_duplicate_position_in_the_same_symbol(self):
        from src.stocks.paper_broker import open_position
        open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        from src.stocks.paper_broker import load_state
        state = load_state()

        df = uptrend_bars(n=80, daily_gain_pct=0.6, volume=2_000_000)
        features = compute_features(df)
        decision = engine.evaluate_entry("AAPL", {"features": features, "df": df}, {"trend": "BULLISH", "risk_appetite": "risk-on"}, state)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("already holding", decision["reason"])


class TestRunForeverResilienceLayers(unittest.TestCase):
    """The three layers run_forever() adds on top of a plain run_cycle():
    market-hours gating, health-tracked exponential backoff, and a
    best-effort periodic learning check.
    """

    def setUp(self):
        # record_start() writes to the real health_status.json (used for
        # the dashboard's uptime figure) -- every test here calls a real
        # run_forever(), so this must be mocked in every one of them,
        # same "never let a test touch the live production state file"
        # discipline as _EngineTestIsolation's last_cycle.json patch above.
        patcher = mock.patch("src.stocks.engine.health.record_start")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_skips_a_cycle_and_does_not_call_run_cycle_while_market_is_closed(self):
        # max_iterations counts real cycles, not closed-market skips --
        # is_market_open() returns False once (one skip, one sleep) then
        # True, so exactly one real cycle runs before max_iterations=1 stops it.
        with mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", True), \
             mock.patch("src.stocks.engine.is_market_open", side_effect=[False, True]), \
             mock.patch("src.stocks.engine.seconds_until_next_open", return_value=5.0), \
             mock.patch("src.stocks.engine.run_cycle") as mock_run_cycle, \
             mock.patch("src.stocks.engine.health.record_success"), \
             mock.patch("src.stocks.engine._maybe_run_learning_cycle"), \
             mock.patch("time.sleep") as mock_sleep:
            engine.run_forever(interval_seconds=0, max_iterations=1)

        mock_run_cycle.assert_called_once()
        mock_sleep.assert_any_call(5.0)  # slept through the closed-market wait

    def test_runs_immediately_when_market_hours_gating_is_disabled(self):
        with mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", False), \
             mock.patch("src.stocks.engine.is_market_open") as mock_is_open, \
             mock.patch("src.stocks.engine.run_cycle", return_value={"scanned": 0}) as mock_run_cycle, \
             mock.patch("src.stocks.engine.health.record_success"), \
             mock.patch("src.stocks.engine._maybe_run_learning_cycle"), \
             mock.patch("time.sleep"):
            engine.run_forever(interval_seconds=0, max_iterations=1)

        mock_run_cycle.assert_called_once()
        mock_is_open.assert_not_called()  # gating fully bypassed, never even checked

    def test_a_cycle_that_raises_records_a_health_failure_and_backs_off(self):
        # max_iterations=2 so the loop reaches its sleep() call between
        # iterations -- run_forever() correctly skips sleeping after the
        # very last iteration, so max_iterations=1 would never sleep at all.
        with mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", False), \
             mock.patch("src.stocks.engine.run_cycle", side_effect=RuntimeError("provider down")), \
             mock.patch("src.stocks.engine.health.record_failure", return_value=42.0) as mock_record_failure, \
             mock.patch("src.stocks.engine._maybe_run_learning_cycle"), \
             mock.patch("time.sleep") as mock_sleep:
            engine.run_forever(interval_seconds=999, max_iterations=2)

        self.assertEqual(mock_record_failure.call_count, 2)
        mock_sleep.assert_called_once_with(42.0)  # backoff delay used, not the normal interval

    def test_a_successful_cycle_records_health_success_and_sleeps_the_normal_interval(self):
        with mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", False), \
             mock.patch("src.stocks.engine.run_cycle", return_value={"scanned": 3, "buys": 1}), \
             mock.patch("src.stocks.engine.health.record_success") as mock_record_success, \
             mock.patch("src.stocks.engine._maybe_run_learning_cycle"), \
             mock.patch("time.sleep") as mock_sleep:
            engine.run_forever(interval_seconds=17, max_iterations=2)

        self.assertEqual(mock_record_success.call_count, 2)
        mock_sleep.assert_called_once_with(17)

    def test_a_learning_cycle_failure_never_stops_the_trading_loop(self):
        with mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", False), \
             mock.patch("src.stocks.engine.run_cycle", return_value={"scanned": 0}), \
             mock.patch("src.stocks.engine.health.record_success"), \
             mock.patch("src.stocks.learning_engine.maybe_run_learning_cycle", side_effect=RuntimeError("learning blew up")), \
             mock.patch("time.sleep"):
            engine.run_forever(interval_seconds=0, max_iterations=2)  # must complete both iterations, not raise


class TestOpportunitySnapshot(_EngineTestIsolation):
    """The dashboard's Opportunity Scanner reads run_cycle()'s
    "opportunities" list -- built purely from what evaluate_entry()
    already computed for the real decision, never influencing it.
    """

    def test_a_buy_decision_produces_a_full_snapshot_with_levels(self):
        candidate = {"features": {"price": 100.0, "atr": 2.0, "pct_change_1d": 1.5, "volume": 5_000_000, "relative_volume": 2.0, "atr_pct": 2.0}}
        decision = {"action": "BUY", "score": 70, "strategy": "breakout", "reason": "score 70>=55", "size_usd": 1500.0, "atr": 2.0}

        snapshot = engine._opportunity_snapshot("AAPL", candidate, decision)

        self.assertEqual(snapshot["symbol"], "AAPL")
        self.assertEqual(snapshot["score"], 70)
        self.assertEqual(snapshot["action"], "BUY")
        self.assertLess(snapshot["stop_loss"], snapshot["price"])
        self.assertGreater(snapshot["take_profit"], snapshot["price"])
        self.assertGreater(snapshot["risk_reward"], 0)

    def test_a_skip_decision_with_a_score_still_shows_illustrative_levels(self):
        candidate = {"features": {"price": 50.0, "atr": 1.0, "pct_change_1d": -0.5, "volume": 1_000_000, "relative_volume": 0.8, "atr_pct": 2.0}}
        decision = {"action": "SKIP", "score": 30, "reason": "score 30 below minimum 55"}

        snapshot = engine._opportunity_snapshot("MSFT", candidate, decision)

        self.assertEqual(snapshot["action"], "SKIP")
        self.assertEqual(snapshot["score"], 30)
        self.assertIsNotNone(snapshot["stop_loss"])  # illustrative, not tied to a real position

    def test_a_skip_decision_with_no_score_and_no_atr_never_raises(self):
        candidate = {"features": {"price": None, "atr": None}}
        decision = {"action": "SKIP", "reason": "already holding an open position in this symbol"}

        snapshot = engine._opportunity_snapshot("SNOW", candidate, decision)

        self.assertIsNone(snapshot["score"])
        self.assertIsNone(snapshot["stop_loss"])
        self.assertIsNone(snapshot["risk_reward"])

    def test_run_cycle_populates_opportunities_ranked_by_score(self):
        low_df = uptrend_bars(n=80, daily_gain_pct=0.1)
        high_df = uptrend_bars(n=80, daily_gain_pct=0.6, volume=2_000_000)
        high_df.iloc[-1, high_df.columns.get_loc("volume")] = 5_000_000
        candidates = {
            "LOW": {"features": compute_features(low_df), "df": low_df},
            "HIGH": {"features": compute_features(high_df), "df": high_df},
        }

        with mock.patch("src.stocks.engine.current_regime", return_value={"trend": "BULLISH", "risk_appetite": "risk-on", "volatility": "LOW"}), \
             mock.patch("src.stocks.engine.scan_universe", return_value=candidates), \
             mock.patch("src.x_intelligence.social_signal_for_token", return_value=None):
            summary = engine.run_cycle()

        self.assertIn("opportunities", summary)
        self.assertEqual(len(summary["opportunities"]), 2)
        scores = [o["score"] for o in summary["opportunities"] if o["score"] is not None]
        self.assertEqual(scores, sorted(scores, reverse=True))  # ranked best-first

    def test_opportunities_list_is_capped_and_excluded_from_health_summary(self):
        many_candidates = {}
        for i in range(30):
            df = uptrend_bars(n=80, daily_gain_pct=0.1 + i * 0.01)
            many_candidates[f"SYM{i}"] = {"features": compute_features(df), "df": df}

        with mock.patch("src.stocks.engine.current_regime", return_value={"trend": "SIDEWAYS", "risk_appetite": "risk-on", "volatility": "LOW"}), \
             mock.patch("src.stocks.engine.scan_universe", return_value=many_candidates), \
             mock.patch("src.x_intelligence.social_signal_for_token", return_value=None):
            summary = engine.run_cycle()

        self.assertLessEqual(len(summary["opportunities"]), 25)

        with mock.patch("src.stocks.engine.STOCKS_RESPECT_MARKET_HOURS", False), \
             mock.patch("src.stocks.engine.run_cycle", return_value=summary), \
             mock.patch("src.stocks.engine.health.record_success") as mock_record_success, \
             mock.patch("src.stocks.engine._maybe_run_learning_cycle"), \
             mock.patch("time.sleep"):
            engine.run_forever(interval_seconds=0, max_iterations=1)

        recorded_summary = mock_record_success.call_args.kwargs["summary"]
        self.assertNotIn("opportunities", recorded_summary)  # health snapshot stays small


if __name__ == "__main__":
    unittest.main()
