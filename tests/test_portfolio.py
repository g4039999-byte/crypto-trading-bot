import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.portfolio as portfolio


class TestPortfolio(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_file = Path(self._tmp_dir.name) / "positions.json"
        self._patches = [
            mock.patch.object(portfolio, "STATE_FILE", tmp_file),
            mock.patch.object(portfolio, "TOTAL_CAPITAL_USD", 24.0),
            mock.patch.object(portfolio, "MAX_TRADE_USD", 5.0),
            mock.patch.object(portfolio, "MAX_OPEN_POSITIONS", 1),
            mock.patch.object(portfolio, "MAX_DAILY_LOSS_PCT", 20.0),
            mock.patch.object(portfolio, "MAX_CAPITAL_DEPLOYMENT_PCT", 80.0),
            mock.patch.object(portfolio, "STOP_LOSS_PCT", 25.0),
            mock.patch.object(portfolio, "TAKE_PROFIT_PCT", 50.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_position_size_never_exceeds_max_trade_usd(self):
        self.assertEqual(portfolio.compute_position_size_usd(), 5.0)

    def test_position_size_never_exceeds_deployment_cap(self):
        with mock.patch.object(portfolio, "MAX_TRADE_USD", 100.0):
            # 80% of $24 = $19.20 -- still far below a $100 per-trade cap.
            self.assertEqual(portfolio.compute_position_size_usd(), 19.2)

    def test_cannot_open_second_position_when_max_reached(self):
        portfolio.open_position("addr-1", "AAA", entry_price_usd=1.0, size_usd=5.0)
        allowed, reason = portfolio.can_open_new_position()
        self.assertFalse(allowed)
        self.assertIn("max", reason)

    def test_no_room_left_once_one_position_uses_the_deployment_cap(self):
        portfolio.open_position("addr-1", "AAA", entry_price_usd=1.0, size_usd=5.0)
        # Raise MAX_TRADE_USD out of the way so the deployment cap (not
        # the per-trade cap) is what actually binds here.
        with mock.patch.object(portfolio, "MAX_OPEN_POSITIONS", 2), mock.patch.object(
            portfolio, "MAX_TRADE_USD", 100.0
        ):
            # deployment cap is $19.20 total; $5 is already committed.
            size = portfolio.compute_position_size_usd()
        self.assertAlmostEqual(size, 14.2, places=2)

    def test_stop_loss_and_take_profit_prices_are_set_on_open(self):
        position = portfolio.open_position("addr-1", "AAA", entry_price_usd=2.0, size_usd=5.0)
        self.assertAlmostEqual(position["stop_loss_price_usd"], 1.5)
        self.assertAlmostEqual(position["take_profit_price_usd"], 3.0)
        self.assertAlmostEqual(position["amount_tokens"], 2.5)

    def test_check_exit_triggers_stop_loss(self):
        position = portfolio.open_position("addr-1", "AAA", entry_price_usd=2.0, size_usd=5.0)
        should_exit, reason = portfolio.check_exit(position, current_price_usd=1.4)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "stop_loss")

    def test_check_exit_triggers_take_profit(self):
        position = portfolio.open_position("addr-1", "AAA", entry_price_usd=2.0, size_usd=5.0)
        should_exit, reason = portfolio.check_exit(position, current_price_usd=3.5)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "take_profit")

    def test_check_exit_holds_in_between(self):
        position = portfolio.open_position("addr-1", "AAA", entry_price_usd=2.0, size_usd=5.0)
        should_exit, reason = portfolio.check_exit(position, current_price_usd=2.1)
        self.assertFalse(should_exit)
        self.assertIsNone(reason)

    def test_close_position_records_pnl_and_frees_the_slot(self):
        portfolio.open_position("addr-1", "AAA", entry_price_usd=2.0, size_usd=5.0)
        result = portfolio.close_position("addr-1", exit_price_usd=1.4, reason="stop_loss")
        self.assertAlmostEqual(result["pnl_usd"], -1.5)  # (1.4-2.0)*2.5

        allowed, _ = portfolio.can_open_new_position()
        self.assertTrue(allowed)

    def test_daily_loss_cap_blocks_new_positions(self):
        portfolio.open_position("addr-1", "AAA", entry_price_usd=2.0, size_usd=5.0)
        # Lose $5 (well over 20% of $24 = $4.80).
        portfolio.close_position("addr-1", exit_price_usd=0.0, reason="stop_loss")

        allowed, reason = portfolio.can_open_new_position()
        self.assertFalse(allowed)
        self.assertIn("daily loss cap", reason)

    def test_corrupt_state_file_recovers_instead_of_crashing(self):
        portfolio.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        portfolio.STATE_FILE.write_text("{not valid json", encoding="utf-8")
        state = portfolio.load_state()
        self.assertEqual(state["open_positions"], [])


if __name__ == "__main__":
    unittest.main()
