import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.live_trader as live_trader
import src.portfolio as portfolio
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


class TestLiveTraderNeverTouchesAWallet(unittest.TestCase):
    def test_module_does_not_import_wallet(self):
        # Regression guard: this module must decide and log only. Real
        # execution is a separate, far-more-gated step (src/wallet.py),
        # never called from here.
        self.assertNotIn("wallet", dir(live_trader))


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


class TestRunLiveCycleIntegration(unittest.TestCase):
    """Uses the real src.portfolio state (in a temp file) to check the
    full decision cycle wires together, with only the network-touching
    parts (kill switch gate, round-trip check) mocked.
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_positions_file = Path(self._tmp_dir.name) / "positions.json"
        tmp_log_file = Path(self._tmp_dir.name) / "trade_log.jsonl"
        self._patches = [
            mock.patch.object(portfolio, "STATE_FILE", tmp_positions_file),
            mock.patch("src.live_trader.trading_allowed", return_value=GateResult(True, [])),
            mock.patch("src.live_trader.round_trip_check", return_value={"sellable": True, "reason": None}),
            # log_decision writes to data/trade_log.jsonl by default -- redirect
            # it too, so this test suite never touches the real project data/.
            mock.patch("src.trade_logger.LOG_FILE", tmp_log_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_opens_one_position_from_a_qualifying_pair(self):
        pairs = [make_pair(score=95, trend="STRONG", address="addr-1", symbol="GOOD")]
        decisions = live_trader.run_live_cycle(pairs)

        self.assertEqual(decisions[-1]["action"], "BUY")
        state = portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["open_positions"][0]["symbol"], "GOOD")

    def test_second_pair_is_skipped_once_a_position_is_open(self):
        pairs = [
            make_pair(score=95, trend="STRONG", address="addr-1", symbol="FIRST"),
            make_pair(score=90, trend="STRONG", address="addr-2", symbol="SECOND"),
        ]
        live_trader.run_live_cycle(pairs)
        state = portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["open_positions"][0]["symbol"], "FIRST")

    def test_exit_closes_the_position_and_frees_the_slot(self):
        portfolio.open_position("addr-1", "GOOD", entry_price_usd=1.0, size_usd=5.0)
        decisions = live_trader.run_live_cycle([], current_prices={"addr-1": 0.5})  # below stop-loss

        self.assertEqual(decisions[0]["action"], "SELL")
        state = portfolio.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(state["closed_trades"]), 1)


if __name__ == "__main__":
    unittest.main()
