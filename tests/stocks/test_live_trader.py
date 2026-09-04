"""Tests for src.stocks.live_trader -- the orchestration layer. Every
network-facing call is mocked via src.stocks.live_broker's functions;
nothing here ever reaches Alpaca, real or paper. See this module's own
docstring: it is never imported by the continuously-running engine/
webapp process, so these tests exercise it the same way a human
deliberately invoking it from a separate script would.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.stocks.kill_switch as kill_switch
import src.stocks.live_broker as live_broker
import src.stocks.live_ledger as live_ledger
import src.stocks.live_logger as live_logger
import src.stocks.live_trader as live_trader

_OPEN_GATE = kill_switch.GateResult(allowed=True, reasons=[])
_CLOSED_GATE = kill_switch.GateResult(allowed=False, reasons=["STOCKS_LIVE_TRADING is not set to true"])


class _IsolatedLiveLogFile(unittest.TestCase):
    """Every test in this file goes through live_trader's decision path,
    which logs via src.stocks.live_logger -- redirect that to a temp
    file so no test run ever appends to the real
    data/stocks/live_trade_log.jsonl (see engine.py/webapp's LAST_CYCLE_
    FILE test-isolation fix earlier this session for why this matters).
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._log_patch = mock.patch.object(live_logger, "LOG_FILE", Path(self._tmp_dir.name) / "live_trade_log.jsonl")
        self._log_patch.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._log_patch.stop()
        self._tmp_dir.cleanup()


class TestEvaluateLiveEntry(_IsolatedLiveLogFile):
    def setUp(self):
        super().setUp()
        self._patches = [
            mock.patch.object(live_ledger, "has_open_position", return_value=False),
            mock.patch.object(live_ledger, "load_state", return_value={"open_positions": []}),
            mock.patch.object(live_broker, "list_live_open_orders", return_value=[]),
            mock.patch.object(live_broker, "get_live_account", return_value={"buying_power": "100.00"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def test_blocked_when_trading_gate_is_closed(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_CLOSED_GATE):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0)
        self.assertEqual(decision["action"], "BLOCKED")

    def test_skip_when_a_live_position_is_already_open_for_the_symbol(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch.object(live_ledger, "has_open_position", return_value=True):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("already open", decision["reason"])

    def test_blocked_when_alpaca_already_has_an_open_order_for_the_symbol(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch.object(live_broker, "list_live_open_orders", return_value=[{"id": "o1"}]):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0)
        self.assertEqual(decision["action"], "BLOCKED")
        self.assertIn("open real order", decision["reason"])

    def test_skip_when_live_risk_gating_declines(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch("src.stocks.live_trader.live_risk.can_open_new_live_position", return_value=(False, "already at the live max")):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("already at the live max", decision["reason"])

    def test_skip_when_price_or_atr_is_unusable(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE):
            decision = live_trader.evaluate_live_entry("AAPL", 0, 2.0)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("price/ATR", decision["reason"])

    def test_blocked_when_account_balance_cannot_be_read(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch.object(live_broker, "get_live_account", return_value=None):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0)
        self.assertEqual(decision["action"], "BLOCKED")
        self.assertIn("buying power", decision["reason"])

    def test_skip_when_buying_power_is_below_the_safety_buffer(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch.object(live_broker, "get_live_account", return_value={"buying_power": "2.00"}), \
             mock.patch("src.stocks.live_trader.STOCKS_LIVE_MIN_BUYING_POWER_BUFFER_USD", 5.0):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0)
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("buying power", decision["reason"])

    def test_buy_decision_when_everything_passes(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch("src.stocks.live_trader.live_risk.can_open_new_live_position", return_value=(True, None)), \
             mock.patch("src.stocks.live_trader.live_risk.compute_live_position_size_usd", return_value=25.0):
            decision = live_trader.evaluate_live_entry("AAPL", 100.0, 2.0, strategy="breakout", score=70)
        self.assertEqual(decision["action"], "BUY")
        self.assertEqual(decision["size_usd"], 25.0)
        self.assertEqual(decision["strategy"], "breakout")


class TestAttemptLiveBuy(_IsolatedLiveLogFile):
    def test_disabled_gate_is_reported_and_nothing_is_recorded(self):
        with mock.patch.object(live_broker, "submit_live_order", side_effect=live_broker.LiveTradingDisabled("nope")), \
             mock.patch.object(live_ledger, "record_open_position") as mocked_record:
            result = live_trader.attempt_live_buy("AAPL", 100.0, {"size_usd": 25.0, "atr": 2.0})
        self.assertFalse(result["executed"])
        mocked_record.assert_not_called()

    def test_rejected_order_is_not_executed(self):
        with mock.patch.object(live_broker, "submit_live_order", side_effect=live_broker.LiveOrderRejected("bad request")), \
             mock.patch.object(live_ledger, "record_open_position") as mocked_record:
            result = live_trader.attempt_live_buy("AAPL", 100.0, {"size_usd": 25.0, "atr": 2.0})
        self.assertFalse(result["executed"])
        mocked_record.assert_not_called()

    def test_ambiguous_outcome_is_surfaced_and_nothing_is_recorded_locally(self):
        with mock.patch.object(live_broker, "submit_live_order", side_effect=live_broker.LiveOrderAmbiguous("network blip")), \
             mock.patch.object(live_ledger, "record_open_position") as mocked_record:
            result = live_trader.attempt_live_buy("AAPL", 100.0, {"size_usd": 25.0, "atr": 2.0})
        self.assertFalse(result["executed"])
        self.assertTrue(result.get("ambiguous"))
        mocked_record.assert_not_called()

    def test_order_that_never_fills_is_not_recorded(self):
        with mock.patch.object(live_broker, "submit_live_order", return_value={"id": "order-1"}), \
             mock.patch.object(live_broker, "poll_order_fill", return_value={"filled": False, "status": "canceled", "timed_out": False}), \
             mock.patch.object(live_ledger, "record_open_position") as mocked_record:
            result = live_trader.attempt_live_buy("AAPL", 100.0, {"size_usd": 25.0, "atr": 2.0})
        self.assertFalse(result["executed"])
        mocked_record.assert_not_called()

    def test_confirmed_fill_is_recorded_in_the_ledger(self):
        with mock.patch.object(live_broker, "submit_live_order", return_value={"id": "order-1"}), \
             mock.patch.object(live_broker, "poll_order_fill", return_value={"filled": True, "filled_qty": "0.25", "filled_avg_price": "101.0"}), \
             mock.patch.object(live_ledger, "record_open_position", return_value={"symbol": "AAPL"}) as mocked_record:
            result = live_trader.attempt_live_buy("AAPL", 100.0, {"size_usd": 25.0, "atr": 2.0, "strategy": "breakout", "score": 70, "reason": "test"})
        self.assertTrue(result["executed"])
        mocked_record.assert_called_once()
        _, kwargs = mocked_record.call_args
        self.assertEqual(mocked_record.call_args.args[0], "AAPL")
        self.assertEqual(kwargs["order_id"], "order-1")

    def test_zero_size_never_reaches_the_broker(self):
        with mock.patch.object(live_broker, "submit_live_order") as mocked_submit:
            result = live_trader.attempt_live_buy("AAPL", 100.0, {"size_usd": 0.0, "atr": 2.0})
        self.assertFalse(result["executed"])
        mocked_submit.assert_not_called()


class TestEvaluateLiveExit(_IsolatedLiveLogFile):
    def _open_position(self, **overrides):
        from datetime import datetime, timezone
        base = {
            "symbol": "AAPL", "entry_price": 100.0, "shares": 0.25, "atr_at_entry": 2.0,
            "stop_loss_price": 97.0, "take_profit_price": 108.0, "trailing_stop_price": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        base.update(overrides)
        return base

    def test_hold_when_price_is_between_stop_and_target(self):
        decision, trailing = live_trader.evaluate_live_exit(self._open_position(), 101.0)
        self.assertEqual(decision["action"], "HOLD")

    def test_sell_on_stop_loss(self):
        decision, _ = live_trader.evaluate_live_exit(self._open_position(), 96.0)
        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["reason"], "stop_loss")

    def test_sell_on_take_profit(self):
        decision, _ = live_trader.evaluate_live_exit(self._open_position(), 109.0)
        self.assertEqual(decision["action"], "SELL")
        self.assertEqual(decision["reason"], "take_profit")


class TestAttemptLiveSell(_IsolatedLiveLogFile):
    def _position(self):
        return {"symbol": "AAPL", "shares": 0.25, "entry_price": 100.0}

    def test_blocked_when_gate_closed(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_CLOSED_GATE), \
             mock.patch.object(live_broker, "submit_live_order") as mocked_submit:
            result = live_trader.attempt_live_sell(self._position(), 108.0, "take_profit")
        self.assertFalse(result["executed"])
        mocked_submit.assert_not_called()

    def test_confirmed_fill_is_recorded_as_closed(self):
        with mock.patch.object(kill_switch, "trading_allowed", return_value=_OPEN_GATE), \
             mock.patch.object(live_broker, "submit_live_order", return_value={"id": "order-2"}), \
             mock.patch.object(live_broker, "poll_order_fill", return_value={"filled": True, "filled_qty": "0.25", "filled_avg_price": "108.0"}), \
             mock.patch.object(live_ledger, "record_close_position", return_value={"pnl_usd": 2.0}) as mocked_close:
            result = live_trader.attempt_live_sell(self._position(), 108.0, "take_profit")
        self.assertTrue(result["executed"])
        mocked_close.assert_called_once()


class TestEmergencyStop(_IsolatedLiveLogFile):
    def test_engages_kill_switch_and_attempts_cancel(self):
        with mock.patch.object(kill_switch, "engage_kill_switch") as mocked_engage, \
             mock.patch.object(live_broker, "cancel_all_live_orders", return_value=True) as mocked_cancel:
            live_trader.emergency_stop("test emergency")
        mocked_engage.assert_called_once_with("test emergency")
        mocked_cancel.assert_called_once()

    def test_safe_when_execution_was_never_enabled(self):
        with mock.patch.object(kill_switch, "engage_kill_switch"), \
             mock.patch.object(live_broker, "cancel_all_live_orders", side_effect=live_broker.LiveTradingDisabled("nope")):
            live_trader.emergency_stop()  # must not raise

    def test_safe_when_cancel_raises_unexpectedly(self):
        with mock.patch.object(kill_switch, "engage_kill_switch") as mocked_engage, \
             mock.patch.object(live_broker, "cancel_all_live_orders", side_effect=RuntimeError("boom")):
            live_trader.emergency_stop()  # must not raise
        mocked_engage.assert_called_once()


if __name__ == "__main__":
    unittest.main()
