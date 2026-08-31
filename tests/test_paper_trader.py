"""The full paper buy-then-sell cycle, exercised through
run_paper_cycle() exactly as radar.py's --paper flag calls it -- this is
the automated stand-in for "test a complete buy and sell cycle without
real money".
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.paper_portfolio as paper_portfolio
import src.paper_trader as paper_trader

SELLABLE = {"sellable": True, "reason": None, "round_trip_loss_pct": 2.0}


def make_pair(score=90, trend="STRONG", price_usd=1.0, address="addr-1", symbol="GOOD"):
    return {
        "score": score, "trend": trend, "price_usd": price_usd, "address": address,
        "symbol": symbol, "liquidity": 20000, "volume": 60000, "age": 10, "buys": 100, "sells": 50,
    }


class TestPaperTraderNeverImportsWallet(unittest.TestCase):
    def test_module_does_not_import_wallet_or_kill_switch(self):
        self.assertNotIn("wallet", dir(paper_trader))
        self.assertNotIn("trading_allowed", dir(paper_trader))


class TestFullBuyThenSellCycle(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_positions = Path(self._tmp_dir.name) / "paper_positions.json"
        tmp_log = Path(self._tmp_dir.name) / "paper_trade_log.jsonl"
        self._patches = [
            mock.patch.object(paper_portfolio, "STATE_FILE", tmp_positions),
            mock.patch("src.paper_logger.LOG_FILE", tmp_log),
            mock.patch("src.paper_trader.round_trip_check", return_value=SELLABLE),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_cycle_1_opens_a_paper_position(self):
        decisions = paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])

        self.assertEqual(decisions[-1]["action"], "BUY")
        state = paper_portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)
        position = state["open_positions"][0]
        self.assertEqual(position["symbol"], "GOOD")
        self.assertAlmostEqual(position["entry_price_usd"], 1.00)
        self.assertAlmostEqual(position["stop_loss_price_usd"], 0.75)   # -25%
        self.assertAlmostEqual(position["take_profit_price_usd"], 1.50)  # +50%

    def test_cycle_2_take_profit_closes_the_position_with_positive_pnl(self):
        paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])  # cycle 1: buy

        # Cycle 2: price rallied past the take-profit level. Trend has
        # cooled to NEUTRAL (no longer STRONG/RISING), so this is a pure
        # exit check -- it must not also qualify as a fresh re-buy of
        # the same token in the same cycle.
        risen_pair = make_pair(price_usd=1.60, symbol="GOOD", address="addr-1", trend="NEUTRAL")
        decisions = paper_trader.run_paper_cycle([risen_pair])

        sell_decisions = [d for d in decisions if d["action"] == "SELL"]
        self.assertEqual(len(sell_decisions), 1)
        self.assertEqual(sell_decisions[0]["reason"], "take_profit")

        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(state["closed_trades"]), 1)
        closed = state["closed_trades"][0]
        self.assertGreater(closed["pnl_usd"], 0)
        self.assertEqual(closed["reason"], "take_profit")

    def test_cycle_2_stop_loss_closes_the_position_with_negative_pnl(self):
        paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])  # cycle 1: buy

        fallen_pair = make_pair(price_usd=0.70, symbol="GOOD", address="addr-1", trend="WEAK")
        decisions = paper_trader.run_paper_cycle([fallen_pair])

        sell_decisions = [d for d in decisions if d["action"] == "SELL"]
        self.assertEqual(len(sell_decisions), 1)
        self.assertEqual(sell_decisions[0]["reason"], "stop_loss")

        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])
        closed = state["closed_trades"][0]
        self.assertLess(closed["pnl_usd"], 0)

    def test_full_cycle_is_logged_to_the_paper_log_only(self):
        paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])
        paper_trader.run_paper_cycle([make_pair(price_usd=1.60, address="addr-1", trend="NEUTRAL")])

        import src.paper_logger as paper_logger

        log_lines = paper_logger.LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
        actions = [__import__("json").loads(line)["action"] for line in log_lines]
        self.assertIn("BUY", actions)
        self.assertIn("SELL", actions)
        # Every entry is clearly tagged as simulated.
        for line in log_lines:
            self.assertEqual(__import__("json").loads(line)["mode"], "PAPER")

    def test_low_score_pair_is_skipped_not_bought(self):
        decisions = paper_trader.run_paper_cycle([make_pair(score=10, price_usd=1.00)])
        self.assertEqual(decisions[-1]["action"], "SKIP")
        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])

    def test_unsellable_token_is_never_bought(self):
        honeypot_check = {"sellable": False, "reason": "no sell route -- possible honeypot", "round_trip_loss_pct": None}
        with mock.patch("src.paper_trader.round_trip_check", return_value=honeypot_check):
            decisions = paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])

        self.assertEqual(decisions[-1]["action"], "SKIP")
        self.assertIn("honeypot", decisions[-1]["reason"])
        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])


if __name__ == "__main__":
    unittest.main()
