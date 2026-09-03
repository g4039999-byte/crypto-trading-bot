"""Mirrors tests/test_portfolio.py -- same rules, but for the isolated
paper-trading state file, confirming it behaves identically without
touching src/portfolio.py or data/positions.json.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.paper_portfolio as paper_portfolio


class TestPaperPortfolio(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_file = Path(self._tmp_dir.name) / "paper_positions.json"
        self._patches = [
            mock.patch.object(paper_portfolio, "STATE_FILE", tmp_file),
            mock.patch.object(paper_portfolio, "TOTAL_CAPITAL_USD", 24.0),
            mock.patch.object(paper_portfolio, "MAX_TRADE_USD", 5.0),
            mock.patch.object(paper_portfolio, "PAPER_MAX_OPEN_POSITIONS", 1),
            mock.patch.object(paper_portfolio, "MAX_DAILY_LOSS_PCT", 20.0),
            mock.patch.object(paper_portfolio, "MAX_CAPITAL_DEPLOYMENT_PCT", 80.0),
            mock.patch.object(paper_portfolio, "STOP_LOSS_PCT", 25.0),
            mock.patch.object(paper_portfolio, "TAKE_PROFIT_PCT", 50.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_uses_its_own_state_file_path(self):
        self.assertIn("paper_positions.json", str(paper_portfolio.STATE_FILE))
        self.assertNotIn("data/positions.json", str(paper_portfolio.STATE_FILE).replace("\\", "/"))

    def test_full_buy_then_sell_cycle(self):
        position = paper_portfolio.open_position("addr-1", "GOOD", entry_price_usd=1.0, size_usd=5.0)
        self.assertAlmostEqual(position["amount_tokens"], 5.0)

        allowed, _ = paper_portfolio.can_open_new_position()
        self.assertFalse(allowed)  # one slot, already used

        should_exit, reason = paper_portfolio.check_exit(position, current_price_usd=1.5)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "take_profit")

        result = paper_portfolio.close_position("addr-1", exit_price_usd=1.5, reason=reason)
        self.assertAlmostEqual(result["pnl_usd"], 2.5)  # (1.5-1.0)*5.0

        allowed_again, _ = paper_portfolio.can_open_new_position()
        self.assertTrue(allowed_again)

        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(state["closed_trades"]), 1)

    def test_reset_paper_state_wipes_everything(self):
        paper_portfolio.open_position("addr-1", "GOOD", entry_price_usd=1.0, size_usd=5.0)
        paper_portfolio.reset_paper_state()
        state = paper_portfolio.load_state()
        self.assertEqual(state, {"open_positions": [], "daily_pnl_usd": {}, "closed_trades": []})


if __name__ == "__main__":
    unittest.main()
