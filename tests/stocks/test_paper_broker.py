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
        aapl = pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        pb.open_position("MSFT", 200.0, 500.0, atr_at_entry=4.0)
        aapl_take_profit = aapl["take_profit_price"]  # actual level, not a hardcoded guess -- see the config change this replaces
        msft_price_well_below_its_own_levels = 205.0  # inside MSFT's own stop/take-profit band regardless of the ATR multipliers configured

        results = pb.evaluate_exit_for_open_positions({"AAPL": aapl_take_profit, "MSFT": msft_price_well_below_its_own_levels})

        self.assertEqual(len(results), 1)  # only AAPL's take-profit triggers
        state = pb.load_state()
        remaining_symbols = {p["symbol"] for p in state["open_positions"]}
        self.assertEqual(remaining_symbols, {"MSFT"})
        msft = next(p for p in state["open_positions"] if p["symbol"] == "MSFT")
        self.assertEqual(msft["mfe_price"], msft_price_well_below_its_own_levels)

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

    def test_save_state_is_atomic_no_partial_file_survives_a_write_failure(self):
        # Simulate a crash mid-write (json.dump raising partway through) --
        # the real state file on disk must be completely untouched, and no
        # stray temp file should be left behind either.
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        original_bytes = pb.STATE_FILE.read_bytes()

        with mock.patch("json.dump", side_effect=RuntimeError("disk full mid-write")):
            with self.assertRaises(RuntimeError):
                pb.save_state(pb.load_state())

        self.assertEqual(pb.STATE_FILE.read_bytes(), original_bytes)  # untouched, not truncated/corrupted
        leftover_tmp_files = list(pb.STATE_FILE.parent.glob(".paper_positions_*.tmp"))
        self.assertEqual(leftover_tmp_files, [])

    def test_load_state_after_restart_sees_a_position_opened_before_the_crash(self):
        # Simulates the exact restart-safety property item 22 asks for:
        # a fresh load_state() call (as a freshly-restarted process would
        # make) must see the position an earlier process already committed.
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        reloaded = pb.load_state()  # a brand new read, not the in-memory value
        self.assertEqual(len(reloaded["open_positions"]), 1)
        self.assertEqual(reloaded["open_positions"][0]["symbol"], "AAPL")

    def test_a_corrupt_state_file_is_preserved_for_forensics_not_silently_dropped(self):
        pb.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        pb.STATE_FILE.write_text("{not valid json at all", encoding="utf-8")

        state = pb.load_state()

        self.assertEqual(state["open_positions"], [])  # safe fallback
        self.assertFalse(pb.STATE_FILE.exists())  # moved aside, not left as garbage at the real path
        preserved = list(pb.STATE_FILE.parent.glob("paper_positions.corrupt.*.json"))
        self.assertEqual(len(preserved), 1)

    def test_reopening_an_already_held_symbol_is_prevented_by_the_engine_not_the_broker(self):
        # paper_broker itself has no built-in "reject a duplicate symbol"
        # guard -- that check lives in src.stocks.engine.evaluate_entry
        # (see tests/stocks/test_engine.py's already-holding test) and
        # depends entirely on this module's state being restart-safe,
        # which the tests above verify directly.
        pb.open_position("AAPL", 100.0, 500.0, atr_at_entry=2.0)
        state_after_restart = pb.load_state()
        already_held = any(p["symbol"] == "AAPL" for p in state_after_restart["open_positions"])
        self.assertTrue(already_held)


if __name__ == "__main__":
    unittest.main()
