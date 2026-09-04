import unittest
from unittest import mock

import src.stocks.live_risk as live_risk


class TestCanOpenNewLivePosition(unittest.TestCase):
    def _empty_state(self):
        return {"open_positions": [], "closed_trades": [], "daily_pnl_usd": {}, "trades_today": {}, "peak_equity_usd": 200.0}

    def test_allowed_from_a_clean_empty_state(self):
        allowed, reason = live_risk.can_open_new_live_position(self._empty_state())
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_blocked_at_max_open_positions(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_OPEN_POSITIONS", 1):
            state = self._empty_state()
            state["open_positions"] = [{"symbol": "AAPL", "size_usd": 25.0}]
            allowed, reason = live_risk.can_open_new_live_position(state)
        self.assertFalse(allowed)
        self.assertIn("max", reason)

    def test_blocked_by_overtrading_guard(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_TRADES_PER_DAY", 2):
            state = self._empty_state()
            state["trades_today"] = {live_risk._today_key(): 2}
            allowed, reason = live_risk.can_open_new_live_position(state)
        self.assertFalse(allowed)
        self.assertIn("overtrading", reason)

    def test_blocked_by_daily_loss_cap(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_DAILY_LOSS_PCT", 3.0):
            state = self._empty_state()
            state["daily_pnl_usd"] = {live_risk._today_key(): -6.5}  # 3.25% of 200 -- over the 3% cap
            allowed, reason = live_risk.can_open_new_live_position(state)
        self.assertFalse(allowed)
        self.assertIn("daily loss cap", reason)

    def test_not_blocked_by_daily_loss_cap_when_still_under_it(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_DAILY_LOSS_PCT", 3.0):
            state = self._empty_state()
            state["daily_pnl_usd"] = {live_risk._today_key(): -1.0}
            allowed, reason = live_risk.can_open_new_live_position(state)
        self.assertTrue(allowed)

    def test_blocked_by_drawdown_circuit_breaker(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_DRAWDOWN_PCT", 10.0):
            state = self._empty_state()
            state["peak_equity_usd"] = 220.0
            state["closed_trades"] = [{"pnl_usd": -25.0}]  # equity now 175, drawdown from 220 peak = ~20.5%
            allowed, reason = live_risk.can_open_new_live_position(state)
        self.assertFalse(allowed)
        self.assertIn("circuit breaker", reason)


class TestComputeLivePositionSizeUsd(unittest.TestCase):
    def _empty_state(self):
        return {"open_positions": []}

    def test_capped_at_max_position_usd(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_POSITION_USD", 25.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT", 100.0):
            size = live_risk.compute_live_position_size_usd(self._empty_state())
        self.assertEqual(size, 25.0)

    def test_never_exceeds_remaining_deployment_room(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_POSITION_USD", 100.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT", 50.0):
            state = {"open_positions": [{"size_usd": 90.0}]}  # only $10 of the $100 deployment cap left
            size = live_risk.compute_live_position_size_usd(state)
        self.assertEqual(size, 10.0)

    def test_regime_multiplier_scales_size_down(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_POSITION_USD", 25.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT", 100.0):
            size = live_risk.compute_live_position_size_usd(self._empty_state(), regime_multiplier=0.5)
        self.assertEqual(size, 12.5)

    def test_never_exceeds_real_buying_power_when_supplied(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_POSITION_USD", 25.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT", 100.0):
            size = live_risk.compute_live_position_size_usd(self._empty_state(), buying_power_usd=7.0)
        self.assertEqual(size, 7.0)

    def test_zero_or_negative_buying_power_yields_zero_size(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_POSITION_USD", 25.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT", 100.0):
            size = live_risk.compute_live_position_size_usd(self._empty_state(), buying_power_usd=-3.0)
        self.assertEqual(size, 0.0)

    def test_never_negative(self):
        with mock.patch.object(live_risk, "STOCKS_LIVE_MAX_POSITION_USD", 100.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_STARTING_CAPITAL_USD", 200.0), \
             mock.patch.object(live_risk, "STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT", 10.0):
            state = {"open_positions": [{"size_usd": 100.0}]}  # already over the $20 deployment cap
            size = live_risk.compute_live_position_size_usd(state)
        self.assertEqual(size, 0.0)


if __name__ == "__main__":
    unittest.main()
