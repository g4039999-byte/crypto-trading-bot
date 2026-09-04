import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.stocks.live_ledger as live_ledger


class TestLiveLedger(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "live_positions.json"
        self._patch = mock.patch.object(live_ledger, "STATE_FILE", tmp_state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp_dir.cleanup()

    def test_load_state_with_no_file_returns_empty_state(self):
        state = live_ledger.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(state["closed_trades"], [])

    def test_has_open_position_false_initially(self):
        self.assertFalse(live_ledger.has_open_position("AAPL"))

    def test_record_open_position_persists_and_computes_atr_levels(self):
        position = live_ledger.record_open_position(
            "AAPL", 100.0, 0.25, 25.0, atr_at_entry=2.0,
            order_id="order-1", client_order_id="coid-1", strategy="breakout", entry_score=70,
            entry_reason="test", starting_capital_usd=200.0,
        )
        self.assertLess(position["stop_loss_price"], 100.0)
        self.assertGreater(position["take_profit_price"], 100.0)
        self.assertEqual(position["order_id"], "order-1")
        self.assertEqual(position["client_order_id"], "coid-1")

        self.assertTrue(live_ledger.has_open_position("AAPL"))
        state = live_ledger.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertEqual(state["trades_today"][live_ledger._today_key()], 1)
        self.assertEqual(state["peak_equity_usd"], 200.0)

    def test_update_mfe_mae_tracks_best_and_worst_and_last_price(self):
        live_ledger.record_open_position("AAPL", 100.0, 0.25, 25.0, atr_at_entry=2.0, order_id="o1", client_order_id="c1")
        live_ledger.update_mfe_mae("AAPL", 108.0)
        live_ledger.update_mfe_mae("AAPL", 97.0)
        state = live_ledger.load_state()
        pos = state["open_positions"][0]
        self.assertEqual(pos["mfe_price"], 108.0)
        self.assertEqual(pos["mae_price"], 97.0)
        self.assertEqual(pos["last_price"], 97.0)
        self.assertIn("last_price_at", pos)

    def test_set_trailing_stop_updates_only_the_matching_symbol(self):
        live_ledger.record_open_position("AAPL", 100.0, 0.25, 25.0, atr_at_entry=2.0, order_id="o1", client_order_id="c1")
        live_ledger.record_open_position("MSFT", 200.0, 0.1, 20.0, atr_at_entry=3.0, order_id="o2", client_order_id="c2")
        live_ledger.set_trailing_stop("AAPL", 103.5)
        state = live_ledger.load_state()
        aapl = next(p for p in state["open_positions"] if p["symbol"] == "AAPL")
        msft = next(p for p in state["open_positions"] if p["symbol"] == "MSFT")
        self.assertEqual(aapl["trailing_stop_price"], 103.5)
        self.assertIsNone(msft["trailing_stop_price"])

    def test_record_close_position_computes_pnl_and_removes_from_open(self):
        live_ledger.record_open_position("AAPL", 100.0, 0.25, 25.0, atr_at_entry=2.0, order_id="o1", client_order_id="c1", starting_capital_usd=200.0)
        result = live_ledger.record_close_position("AAPL", 108.0, "take_profit", order_id="o2", client_order_id="c2", starting_capital_usd=200.0)

        self.assertAlmostEqual(result["pnl_usd"], (108.0 - 100.0) * 0.25)
        self.assertTrue(result["position"]["was_correct"])
        state = live_ledger.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(state["closed_trades"]), 1)
        self.assertFalse(live_ledger.has_open_position("AAPL"))

    def test_record_close_position_with_nothing_open_returns_none(self):
        self.assertIsNone(live_ledger.record_close_position("NOPE", 100.0, "stop_loss", order_id="o1", client_order_id="c1"))

    def test_realized_pnl_usd_sums_closed_trades(self):
        live_ledger.record_open_position("AAPL", 100.0, 0.25, 25.0, atr_at_entry=2.0, order_id="o1", client_order_id="c1", starting_capital_usd=200.0)
        live_ledger.record_close_position("AAPL", 108.0, "take_profit", order_id="o2", client_order_id="c2", starting_capital_usd=200.0)
        self.assertAlmostEqual(live_ledger.realized_pnl_usd(), 2.0)

    def test_a_corrupt_state_file_is_preserved_and_treated_as_empty(self):
        live_ledger.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        live_ledger.STATE_FILE.write_text("{not valid json", encoding="utf-8")
        state = live_ledger.load_state()
        self.assertEqual(state["open_positions"], [])
        corrupt_files = list(live_ledger.STATE_FILE.parent.glob("*.corrupt.*.json"))
        self.assertEqual(len(corrupt_files), 1)


if __name__ == "__main__":
    unittest.main()
