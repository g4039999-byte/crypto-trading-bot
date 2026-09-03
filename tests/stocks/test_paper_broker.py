import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.stocks.paper_broker as pb


class TestPaperBroker(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "paper_positions.json"
        tmp_log = Path(self._tmp_dir.name) / "paper_trade_log.jsonl"
        self._patches = [
            mock.patch.object(pb, "STATE_FILE", tmp_state),
            mock.patch("src.stocks.paper_logger.LOG_FILE", tmp_log),
            mock.patch.object(pb.alpaca_client, "submit_paper_order", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_open_position_computes_shares_and_atr_based_levels(self):
        position = pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0, strategy="momentum", entry_score=70)
        self.assertAlmostEqual(position["shares"], 5.0)
        self.assertLess(position["stop_loss_price"], 100.0)
        self.assertGreater(position["take_profit_price"], 100.0)
        state = pb.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["trades_today"][pb._today_key()], 1)

    def test_close_position_computes_pnl_mfe_mae_and_was_correct(self):
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        pb.update_mfe_mae("AAPL", 108.0)
        pb.update_mfe_mae("AAPL", 97.0)
        result = pb.close_position("AAPL", 105.0, "take_profit")

        self.assertAlmostEqual(result["pnl_usd"], 25.0)  # (105-100)*5 shares
        closed = result["position"]
        self.assertTrue(closed["was_correct"])
        self.assertAlmostEqual(closed["mfe_pct"], 8.0)
        self.assertAlmostEqual(closed["mae_pct"], -3.0)

        state = pb.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(state["closed_trades"]), 1)

    def test_stop_loss_close_marks_was_correct_false(self):
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        result = pb.close_position("AAPL", 95.0, "stop_loss")
        self.assertFalse(result["position"]["was_correct"])

    def test_close_position_with_nothing_open_returns_none(self):
        self.assertIsNone(pb.close_position("NOPE", 100.0, "stop_loss"))

    def test_evaluate_exit_closes_triggered_positions_and_updates_mfe_for_others(self):
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)  # stop ~97, take ~106
        pb.open_position("MSFT", 200.0, 500.0, atr_at_entry=4.0)  # stop ~194, take ~212

        results = pb.evaluate_exit_for_open_positions({"AAPL": 107.0, "MSFT": 205.0})

        self.assertEqual(len(results), 1)  # only AAPL's take-profit triggers
        state = pb.load_state()
        remaining_symbols = {p["symbol"] for p in state["open_positions"]}
        self.assertEqual(remaining_symbols, {"MSFT"})
        msft = next(p for p in state["open_positions"] if p["symbol"] == "MSFT")
        self.assertEqual(msft["mfe_price"], 205.0)

    def test_evaluate_exit_skips_symbols_with_no_price_this_cycle(self):
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        results = pb.evaluate_exit_for_open_positions({})  # no price for AAPL
        self.assertEqual(results, [])
        state = pb.load_state()
        self.assertEqual(len(state["open_positions"]), 1)

    def test_reset_paper_state_wipes_everything(self):
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        pb.reset_paper_state()
        state = pb.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(state["closed_trades"], [])

    def test_alpaca_mirror_failure_never_blocks_the_local_ledger(self):
        with mock.patch.object(pb.alpaca_client, "submit_paper_order", side_effect=RuntimeError("boom")):
            position = pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
            result = pb.close_position("AAPL", 105.0, "take_profit")
        self.assertIsNotNone(position)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
