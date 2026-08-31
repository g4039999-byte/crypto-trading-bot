import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.live_trader as live_trader
import src.portfolio as portfolio
import src.wallet as wallet
from src.kill_switch import GateResult
from src.risk import RiskAssessment


def make_pair(score=90, trend="STRONG", price_usd=1.0, address="addr-1", symbol="GOOD"):
    return {
        "score": score,
        "trend": trend,
        "price_usd": price_usd,
        "address": address,
        "symbol": symbol,
        "liquidity": 20000,
        "volume": 60000,
        "age": 10,
        "buys": 100,
        "sells": 50,
    }


class TestLiveTraderRealExecutionStaysGated(unittest.TestCase):
    """The one regression test that matters most in this file: even after
    wiring real execution into run_live_cycle(), the actual module-level
    EXECUTION_ENABLED_IN_CODE constant in src/wallet.py -- untouched by
    anything here -- must still block every real order.
    """

    def test_execution_enabled_in_code_is_still_false(self):
        self.assertFalse(wallet.EXECUTION_ENABLED_IN_CODE)

    def test_full_cycle_opens_nothing_for_real_even_with_every_other_gate_open(self):
        # Every gate EXCEPT the source-level EXECUTION_ENABLED_IN_CODE is
        # forced open here, on purpose -- proving that one gate alone is
        # still enough to stop a real trade.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp_positions_file = Path(tmp_dir.name) / "positions.json"
        tmp_log_file = Path(tmp_dir.name) / "trade_log.jsonl"

        with mock.patch.object(portfolio, "STATE_FILE", tmp_positions_file), mock.patch(
            "src.live_trader.trading_allowed", return_value=GateResult(True, [])
        ), mock.patch("src.live_trader.round_trip_check", return_value={"sellable": True, "reason": None}), mock.patch(
            "src.trade_logger.LOG_FILE", tmp_log_file
        ), mock.patch(
            "src.live_trader.get_sol_usd_price", return_value=150.0
        ), mock.patch(
            "src.live_trader.get_quote", return_value={"outAmount": "1000000", "inAmount": "1"}
        ):
            decisions = live_trader.run_live_cycle([make_pair(score=95, trend="STRONG")])

        self.assertEqual(decisions[-1]["action"], "BUY")  # the *decision* passed screening
        state = portfolio.load_state()
        self.assertEqual(state["open_positions"], [])  # but nothing was actually bought


class TestEvaluateEntry(unittest.TestCase):
    def setUp(self):
        self._log_patch = mock.patch("src.live_trader.log_decision")
        self.mock_log = self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()

    def test_blocked_when_kill_switch_denies(self):
        with mock.patch(
            "src.live_trader.trading_allowed",
            return_value=GateResult(allowed=False, reasons=["LIVE_TRADING is not set to true"]),
        ):
            decision = live_trader.evaluate_entry(make_pair())
        self.assertEqual(decision["action"], "BLOCKED")

    def test_skip_when_no_room_for_new_position(self):
        with mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])), mock.patch(
            "src.live_trader.load_state", return_value={}
        ), mock.patch("src.live_trader.can_open_new_position", return_value=(False, "already at the max")):
            decision = live_trader.evaluate_entry(make_pair())
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("max", decision["reason"])

    def test_skip_when_missing_price(self):
        with mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])), mock.patch(
            "src.live_trader.load_state", return_value={}
        ), mock.patch("src.live_trader.can_open_new_position", return_value=(True, None)):
            decision = live_trader.evaluate_entry(make_pair(price_usd=None))
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("price", decision["reason"])

    def test_skip_when_score_below_minimum(self):
        with mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])), mock.patch(
            "src.live_trader.load_state", return_value={}
        ), mock.patch("src.live_trader.can_open_new_position", return_value=(True, None)), mock.patch(
            "src.live_trader.MIN_LIVE_SCORE", 80
        ):
            decision = live_trader.evaluate_entry(make_pair(score=50))
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("score", decision["reason"])

    def test_skip_when_trend_not_acceptable(self):
        with mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])), mock.patch(
            "src.live_trader.load_state", return_value={}
        ), mock.patch("src.live_trader.can_open_new_position", return_value=(True, None)):
            decision = live_trader.evaluate_entry(make_pair(trend="WEAK"))
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("trend", decision["reason"])

    def test_skip_when_risk_screening_fails(self):
        with mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])), mock.patch(
            "src.live_trader.load_state", return_value={}
        ), mock.patch("src.live_trader.can_open_new_position", return_value=(True, None)), mock.patch(
            "src.live_trader.assess_token_safety",
            return_value=RiskAssessment(passed=False, reasons=["liquidity too low"]),
        ):
            decision = live_trader.evaluate_entry(make_pair(), probe_check={"sellable": True})
        self.assertEqual(decision["action"], "SKIP")
        self.assertIn("liquidity", decision["reason"])

    def test_buy_when_everything_passes(self):
        with mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])), mock.patch(
            "src.live_trader.load_state", return_value={}
        ), mock.patch("src.live_trader.can_open_new_position", return_value=(True, None)), mock.patch(
            "src.live_trader.assess_token_safety", return_value=RiskAssessment(passed=True, reasons=[])
        ), mock.patch("src.live_trader.compute_position_size_usd", return_value=5.0):
            decision = live_trader.evaluate_entry(make_pair(), probe_check={"sellable": True})
        self.assertEqual(decision["action"], "BUY")
        self.assertEqual(decision["size_usd"], 5.0)


class TestEvaluateExit(unittest.TestCase):
    def test_hold_within_band(self):
        position = {"symbol": "AAA", "token_address": "addr-1", "stop_loss_price_usd": 0.5, "take_profit_price_usd": 2.0}
        with mock.patch("src.live_trader.log_decision") as mock_log:
            decision = live_trader.evaluate_exit(position, current_price_usd=1.0)
        self.assertEqual(decision["action"], "HOLD")
        mock_log.assert_not_called()

    def test_sell_on_stop_loss(self):
        position = {"symbol": "AAA", "token_address": "addr-1", "stop_loss_price_usd": 0.5, "take_profit_price_usd": 2.0}
        with mock.patch("src.live_trader.log_decision") as mock_log:
            decision = live_trader.evaluate_exit(position, current_price_usd=0.4)
        self.assertEqual(decision["action"], "SELL")
        mock_log.assert_called_once()


class TestAttemptRealBuy(unittest.TestCase):
    """_attempt_real_buy() in isolation -- every branch of what can
    happen between "screening passed" and "money actually moved".
    """

    def setUp(self):
        self._log_patch = mock.patch("src.live_trader.log_decision")
        self.mock_log = self._log_patch.start()
        self.addCleanup(self._log_patch.stop)

    def test_no_price_means_nothing_executed(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=None):
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertFalse(result["executed"])
        self.assertIn("SOL/USD price", result["reason"])

    def test_no_quote_means_nothing_executed(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=150.0), mock.patch(
            "src.live_trader.get_quote", return_value=None
        ):
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertFalse(result["executed"])
        self.assertIn("quote", result["reason"])

    def test_execution_disabled_is_reported_but_does_not_raise(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=150.0), mock.patch(
            "src.live_trader.get_quote", return_value={"outAmount": "1"}
        ):
            # Real gate, real (untouched) EXECUTION_ENABLED_IN_CODE=False.
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertFalse(result["executed"])
        self.assertIn("disabled", result["reason"])

    def test_wallet_not_configured_is_caught(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=150.0), mock.patch(
            "src.live_trader.get_quote", return_value={"outAmount": "1"}
        ), mock.patch.object(
            wallet, "build_and_send_swap", side_effect=wallet.WalletNotConfigured("no key")
        ):
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertFalse(result["executed"])
        self.assertIn("wallet not ready", result["reason"])

    def test_swap_execution_error_is_caught(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=150.0), mock.patch(
            "src.live_trader.get_quote", return_value={"outAmount": "1"}
        ), mock.patch.object(
            wallet, "build_and_send_swap", side_effect=wallet.SwapExecutionError("jupiter down")
        ):
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertFalse(result["executed"])
        self.assertIn("could not be built/sent", result["reason"])

    def test_unconfirmed_swap_is_not_treated_as_executed(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=150.0), mock.patch(
            "src.live_trader.get_quote", return_value={"outAmount": "1"}
        ), mock.patch.object(
            wallet, "build_and_send_swap", return_value={"confirmed": False, "signature": "Sig1", "timed_out": True}
        ):
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertFalse(result["executed"])
        self.assertEqual(result["signature"], "Sig1")

    def test_confirmed_swap_is_executed(self):
        with mock.patch("src.live_trader.get_sol_usd_price", return_value=150.0), mock.patch(
            "src.live_trader.get_quote", return_value={"outAmount": "1"}
        ), mock.patch.object(
            wallet, "build_and_send_swap", return_value={"confirmed": True, "signature": "Sig1"}
        ):
            result = live_trader._attempt_real_buy(make_pair(), 5.0)
        self.assertTrue(result["executed"])
        self.assertEqual(result["signature"], "Sig1")


class TestAttemptRealSell(unittest.TestCase):
    def setUp(self):
        self._log_patch = mock.patch("src.live_trader.log_decision")
        self.mock_log = self._log_patch.start()
        self.addCleanup(self._log_patch.stop)

    def _position(self):
        return {"symbol": "GOOD", "token_address": "addr-1"}

    def test_zero_balance_means_nothing_to_sell(self):
        with mock.patch.object(wallet, "get_public_key_str", return_value="Pub1"), mock.patch.object(
            wallet, "get_spl_token_balance_raw", return_value=0
        ):
            result = live_trader._attempt_real_sell(self._position(), 1.0)
        self.assertFalse(result["executed"])
        self.assertIn("zero", result["reason"])

    def test_confirmed_sell_is_executed(self):
        with mock.patch.object(wallet, "get_public_key_str", return_value="Pub1"), mock.patch.object(
            wallet, "get_spl_token_balance_raw", return_value=1_000_000
        ), mock.patch("src.live_trader.get_quote", return_value={"outAmount": "1"}), mock.patch.object(
            wallet, "build_and_send_swap", return_value={"confirmed": True, "signature": "Sig2"}
        ):
            result = live_trader._attempt_real_sell(self._position(), 1.0)
        self.assertTrue(result["executed"])
        self.assertEqual(result["signature"], "Sig2")

    def test_execution_disabled_is_reported_but_does_not_raise(self):
        with mock.patch.object(wallet, "get_public_key_str", return_value="Pub1"), mock.patch.object(
            wallet, "get_spl_token_balance_raw", return_value=1_000_000
        ), mock.patch("src.live_trader.get_quote", return_value={"outAmount": "1"}):
            result = live_trader._attempt_real_sell(self._position(), 1.0)
        self.assertFalse(result["executed"])
        self.assertIn("disabled", result["reason"])


class TestRunLiveCycleOrchestration(unittest.TestCase):
    """Uses the real src.portfolio state (in a temp file) to check the
    cycle wires together correctly. _attempt_real_buy/_attempt_real_sell
    are mocked here (they have their own dedicated tests above) so these
    tests focus purely on the orchestration: which pair gets bought, that
    a second candidate is skipped once a slot is full, and that a closed
    position actually frees that slot -- all conditioned on whether the
    (mocked) real execution reports "executed".
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_positions_file = Path(self._tmp_dir.name) / "positions.json"
        tmp_log_file = Path(self._tmp_dir.name) / "trade_log.jsonl"
        self._patches = [
            mock.patch.object(portfolio, "STATE_FILE", tmp_positions_file),
            mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])),
            mock.patch("src.live_trader.round_trip_check", return_value={"sellable": True, "reason": None}),
            mock.patch("src.trade_logger.LOG_FILE", tmp_log_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_opens_one_position_when_execution_succeeds(self):
        pairs = [make_pair(score=95, trend="STRONG", address="addr-1", symbol="GOOD")]
        with mock.patch(
            "src.live_trader._attempt_real_buy", return_value={"executed": True, "signature": "Sig1"}
        ):
            decisions = live_trader.run_live_cycle(pairs)

        self.assertEqual(decisions[-1]["action"], "BUY")
        state = portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["open_positions"][0]["symbol"], "GOOD")

    def test_opens_nothing_when_execution_is_not_confirmed(self):
        pairs = [make_pair(score=95, trend="STRONG", address="addr-1", symbol="GOOD")]
        with mock.patch(
            "src.live_trader._attempt_real_buy", return_value={"executed": False, "reason": "disabled"}
        ):
            live_trader.run_live_cycle(pairs)

        state = portfolio.load_state()
        self.assertEqual(state["open_positions"], [])

    def test_second_pair_is_skipped_once_a_position_is_open(self):
        pairs = [
            make_pair(score=95, trend="STRONG", address="addr-1", symbol="FIRST"),
            make_pair(score=90, trend="STRONG", address="addr-2", symbol="SECOND"),
        ]
        with mock.patch(
            "src.live_trader._attempt_real_buy", return_value={"executed": True, "signature": "Sig1"}
        ):
            live_trader.run_live_cycle(pairs)
        state = portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["open_positions"][0]["symbol"], "FIRST")

    def test_exit_closes_the_position_when_execution_succeeds(self):
        portfolio.open_position("addr-1", "GOOD", entry_price_usd=1.0, size_usd=5.0)
        with mock.patch(
            "src.live_trader._attempt_real_sell", return_value={"executed": True, "signature": "Sig2"}
        ):
            decisions = live_trader.run_live_cycle([], current_prices={"addr-1": 0.5})  # below stop-loss

        self.assertEqual(decisions[0]["action"], "SELL")
        state = portfolio.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(state["closed_trades"]), 1)

    def test_exit_signal_leaves_position_open_when_sell_is_not_executed(self):
        portfolio.open_position("addr-1", "GOOD", entry_price_usd=1.0, size_usd=5.0)
        with mock.patch(
            "src.live_trader._attempt_real_sell", return_value={"executed": False, "reason": "disabled"}
        ):
            decisions = live_trader.run_live_cycle([], current_prices={"addr-1": 0.5})

        self.assertEqual(decisions[0]["action"], "SELL")  # the decision still fires
        state = portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)  # but bookkeeping reflects reality: still held


if __name__ == "__main__":
    unittest.main()
