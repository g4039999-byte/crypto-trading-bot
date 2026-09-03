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


class TestEngineResilience(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "paper_positions.json"
        tmp_log = Path(self._tmp_dir.name) / "paper_trade_log.jsonl"
        self._patches = [
            mock.patch("src.stocks.paper_broker.STATE_FILE", tmp_state),
            mock.patch("src.stocks.paper_logger.LOG_FILE", tmp_log),
            mock.patch("src.stocks.paper_broker.alpaca_client.submit_paper_order", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

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


if __name__ == "__main__":
    unittest.main()
