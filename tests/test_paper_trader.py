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
import src.snapshot as snapshot

SELLABLE = {"sellable": True, "reason": None, "round_trip_loss_pct": 2.0}


def make_pair(score=90, trend="STRONG", price_usd=1.0, address="addr-1", symbol="GOOD", age=20):
    return {
        "score": score, "trend": trend, "price_usd": price_usd, "address": address,
        "symbol": symbol, "liquidity": 20000, "volume": 60000, "age": age, "buys": 100, "sells": 50,
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
        tmp_snapshots = Path(self._tmp_dir.name) / "snapshots.json"
        self._patches = [
            mock.patch.object(paper_portfolio, "STATE_FILE", tmp_positions),
            mock.patch("src.paper_logger.LOG_FILE", tmp_log),
            mock.patch("src.paper_trader.round_trip_check", return_value=SELLABLE),
            # Isolates _liquidity_drawdown_pct()/_discovery_to_entry_seconds()'s
            # load_snapshots() calls from the real, machine-local
            # data/snapshots.json -- without this they'd silently read
            # whatever real history (if any) happens to exist for these
            # synthetic test addresses.
            mock.patch.object(snapshot, "SNAPSHOT_FILE", tmp_snapshots),
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
        self.assertAlmostEqual(position["stop_loss_price_usd"], 0.75)   # -25% (PAPER_STOP_LOSS_PCT)
        self.assertAlmostEqual(position["take_profit_price_usd"], 1.25)  # +25% (PAPER_TAKE_PROFIT_PCT)
        # Tracking fields requested for performance analysis: entry
        # score/trend/age and the reason are recorded on the position
        # itself, not just the separate paper_trade_log.jsonl line.
        self.assertEqual(position["entry_score"], 90)
        self.assertEqual(position["entry_trend"], "STRONG")
        self.assertEqual(position["entry_age_minutes"], 20)
        self.assertIn("passed", position["entry_reason"])
        # No prior snapshot exists for this synthetic token in this
        # isolated test, so "time since discovery" is unknown (None) --
        # a live discovery timestamp is exercised by
        # test_discovery_to_entry_seconds_is_recorded_when_history_exists.
        self.assertIsNone(position["discovery_to_entry_seconds"])

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

    def test_still_qualifying_pair_is_not_bought_twice_while_already_held(self):
        # Cycle 1: opens a position. Cycle 2: the exact same pair still
        # qualifies on every score/trend/risk check (nothing about it
        # changed) -- it must be skipped as "already held", not bought a
        # second time into a duplicate position on the same token.
        pair = make_pair(price_usd=1.00)
        paper_trader.run_paper_cycle([pair])
        decisions = paper_trader.run_paper_cycle([pair])

        self.assertEqual(decisions[-1]["action"], "SKIP")
        self.assertIn("already holding", decisions[-1]["reason"])
        state = paper_portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 1)

    def test_multiple_different_qualifying_pairs_open_multiple_positions_in_one_cycle(self):
        pairs = [
            make_pair(address="addr-1", symbol="ONE", price_usd=1.00),
            make_pair(address="addr-2", symbol="TWO", price_usd=2.00),
        ]
        decisions = paper_trader.run_paper_cycle(pairs)

        buys = [d for d in decisions if d["action"] == "BUY"]
        self.assertEqual(len(buys), 2)
        state = paper_portfolio.load_state()
        self.assertEqual(len(state["open_positions"]), 2)
        self.assertEqual({p["symbol"] for p in state["open_positions"]}, {"ONE", "TWO"})

    def test_unsellable_token_is_never_bought(self):
        honeypot_check = {"sellable": False, "reason": "no sell route -- possible honeypot", "round_trip_loss_pct": None}
        with mock.patch("src.paper_trader.round_trip_check", return_value=honeypot_check):
            decisions = paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])

        self.assertEqual(decisions[-1]["action"], "SKIP")
        self.assertIn("honeypot", decisions[-1]["reason"])
        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])

    def test_discovery_to_entry_seconds_is_recorded_when_history_exists(self):
        import json
        from datetime import datetime, timedelta, timezone

        first_seen = datetime.now(timezone.utc) - timedelta(seconds=600)
        snapshot.SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        snapshot.SNAPSHOT_FILE.write_text(json.dumps({
            "addr-1": [
                {"timestamp": first_seen.isoformat(), "price_usd": "0.9", "liquidity_usd": 20000,
                 "volume_24h": 60000, "buys_24h": 90, "sells_24h": 40},
            ]
        }), encoding="utf-8")

        decisions = paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])
        self.assertEqual(decisions[-1]["action"], "BUY")
        position = paper_portfolio.load_state()["open_positions"][0]
        self.assertIsNotNone(position["discovery_to_entry_seconds"])
        self.assertGreaterEqual(position["discovery_to_entry_seconds"], 599)

    def test_candidate_with_draining_liquidity_is_skipped(self):
        # This token's liquidity has already fallen well over half from
        # its recent peak (20000 -> 8000 = -60%), even though 8000 still
        # clears PAPER_MIN_LIQUIDITY_USD on its own -- the drawdown guard
        # must catch what the static floor alone would miss.
        import json

        snapshot.SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        snapshot.SNAPSHOT_FILE.write_text(json.dumps({
            "addr-1": [
                {"timestamp": "2026-01-01T00:00:00+00:00", "price_usd": "1.0", "liquidity_usd": 20000,
                 "volume_24h": 60000, "buys_24h": 90, "sells_24h": 40},
                {"timestamp": "2026-01-01T00:05:00+00:00", "price_usd": "0.7", "liquidity_usd": 8000,
                 "volume_24h": 60000, "buys_24h": 95, "sells_24h": 45},
            ]
        }), encoding="utf-8")

        draining_pair = make_pair(price_usd=0.70)
        draining_pair["liquidity"] = 8000
        decisions = paper_trader.run_paper_cycle([draining_pair])

        self.assertEqual(decisions[-1]["action"], "SKIP")
        self.assertIn("drained", decisions[-1]["reason"])
        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])

    def test_stop_loss_cooldown_blocks_immediate_reentry_after_a_loss(self):
        paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])  # cycle 1: buy

        fallen_pair = make_pair(price_usd=0.70, trend="WEAK")  # -30%, past the -25% stop
        paper_trader.run_paper_cycle([fallen_pair])  # cycle 2: stop-loss sells it
        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(state["closed_trades"][-1]["reason"], "stop_loss")

        # Cycle 3: the exact same token, now fully recovered and
        # qualifying again on every other check -- still must not be
        # re-bought while PAPER_STOP_LOSS_COOLDOWN_MINUTES hasn't passed.
        recovered_pair = make_pair(price_usd=1.00)
        decisions = paper_trader.run_paper_cycle([recovered_pair])

        self.assertEqual(decisions[-1]["action"], "SKIP")
        self.assertIn("cooldown", decisions[-1]["reason"])
        state = paper_portfolio.load_state()
        self.assertEqual(state["open_positions"], [])

    def test_stop_loss_cooldown_expires_after_the_configured_window(self):
        with mock.patch.object(paper_trader, "PAPER_STOP_LOSS_COOLDOWN_MINUTES", 0):
            paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])
            paper_trader.run_paper_cycle([make_pair(price_usd=0.70, trend="WEAK")])
            decisions = paper_trader.run_paper_cycle([make_pair(price_usd=1.00)])

        # Cooldown disabled (0) -- the same token is free to be
        # re-evaluated fresh immediately, exactly like before this
        # feature existed.
        self.assertEqual(decisions[-1]["action"], "BUY")


if __name__ == "__main__":
    unittest.main()
