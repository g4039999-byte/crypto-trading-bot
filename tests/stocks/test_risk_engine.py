import unittest
from unittest import mock

from src.stocks import risk_engine as re


def make_state(open_positions=None, closed_trades=None, daily_pnl_usd=None,
                trades_today=None, peak_equity_usd=None):
    return {
        "open_positions": open_positions or [],
        "closed_trades": closed_trades or [],
        "daily_pnl_usd": daily_pnl_usd or {},
        "trades_today": trades_today or {},
        "peak_equity_usd": peak_equity_usd if peak_equity_usd is not None else re.STOCKS_STARTING_CAPITAL_USD,
    }


class TestCanOpenNewPosition(unittest.TestCase):
    def test_allows_when_state_is_empty(self):
        allowed, reason = re.can_open_new_position(make_state())
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_blocks_at_max_open_positions(self):
        with mock.patch.object(re, "STOCKS_MAX_OPEN_POSITIONS", 2):
            state = make_state(open_positions=[{"size_usd": 100}, {"size_usd": 100}])
            allowed, reason = re.can_open_new_position(state)
        self.assertFalse(allowed)
        self.assertIn("max", reason)

    def test_blocks_at_max_trades_per_day(self):
        with mock.patch.object(re, "STOCKS_MAX_TRADES_PER_DAY", 3):
            state = make_state(trades_today={re._today_key(): 3})
            allowed, reason = re.can_open_new_position(state)
        self.assertFalse(allowed)
        self.assertIn("overtrading", reason)

    def test_blocks_at_daily_loss_cap(self):
        with mock.patch.object(re, "STOCKS_STARTING_CAPITAL_USD", 10000), \
                mock.patch.object(re, "STOCKS_MAX_DAILY_LOSS_PCT", 3.0):
            state = make_state(daily_pnl_usd={re._today_key(): -350})  # -3.5% > 3% cap
            allowed, reason = re.can_open_new_position(state)
        self.assertFalse(allowed)
        self.assertIn("daily loss cap", reason)

    def test_circuit_breaker_blocks_on_drawdown(self):
        with mock.patch.object(re, "STOCKS_STARTING_CAPITAL_USD", 10000), \
                mock.patch.object(re, "STOCKS_MAX_DRAWDOWN_PCT", 10.0):
            state = make_state(peak_equity_usd=10000, closed_trades=[{"pnl_usd": -1500}])  # equity 8500, dd=15%
            allowed, reason = re.can_open_new_position(state)
        self.assertFalse(allowed)
        self.assertIn("circuit breaker", reason)


class TestPositionSizing(unittest.TestCase):
    def test_caps_at_max_position_usd(self):
        with mock.patch.object(re, "STOCKS_MAX_POSITION_USD", 1500), \
                mock.patch.object(re, "STOCKS_STARTING_CAPITAL_USD", 10000), \
                mock.patch.object(re, "STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT", 80):
            size = re.compute_position_size_usd(make_state())
        self.assertEqual(size, 1500)

    def test_shrinks_to_remaining_room_under_deployment_cap(self):
        with mock.patch.object(re, "STOCKS_MAX_POSITION_USD", 1500), \
                mock.patch.object(re, "STOCKS_STARTING_CAPITAL_USD", 10000), \
                mock.patch.object(re, "STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT", 80):
            state = make_state(open_positions=[{"size_usd": 7500}])  # only 500 of the 8000 cap left
            size = re.compute_position_size_usd(state)
        self.assertEqual(size, 500)

    def test_regime_multiplier_scales_size_down(self):
        with mock.patch.object(re, "STOCKS_MAX_POSITION_USD", 1000), \
                mock.patch.object(re, "STOCKS_STARTING_CAPITAL_USD", 10000), \
                mock.patch.object(re, "STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT", 80):
            size = re.compute_position_size_usd(make_state(), regime_multiplier=0.5)
        self.assertEqual(size, 500)


class TestExitPricesAndChecks(unittest.TestCase):
    def test_stop_and_take_profit_scale_with_atr(self):
        stop = re.stop_loss_price(100.0, atr_value=2.0)
        take = re.take_profit_price(100.0, atr_value=2.0)
        self.assertLess(stop, 100.0)
        self.assertGreater(take, 100.0)

    def test_check_exit_stop_loss(self):
        position = {"stop_loss_price": 95.0, "take_profit_price": 110.0}
        should_exit, reason = re.check_exit(position, current_price=94.0, held_days=1)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "stop_loss")

    def test_check_exit_take_profit(self):
        position = {"stop_loss_price": 95.0, "take_profit_price": 110.0}
        should_exit, reason = re.check_exit(position, current_price=111.0, held_days=1)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "take_profit")

    def test_check_exit_trailing_stop_takes_priority_over_take_profit_target(self):
        position = {"stop_loss_price": 90.0, "take_profit_price": 120.0, "trailing_stop_price": 105.0}
        should_exit, reason = re.check_exit(position, current_price=104.0, held_days=1)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "trailing_stop")

    def test_check_exit_max_holding_time(self):
        with mock.patch.object(re, "STOCKS_MAX_HOLDING_DAYS", 5.0):
            position = {"stop_loss_price": 50.0, "take_profit_price": 200.0}
            should_exit, reason = re.check_exit(position, current_price=100.0, held_days=6)
        self.assertTrue(should_exit)
        self.assertEqual(reason, "max_holding_time")

    def test_check_exit_holds_when_nothing_triggers(self):
        position = {"stop_loss_price": 90.0, "take_profit_price": 120.0}
        should_exit, reason = re.check_exit(position, current_price=100.0, held_days=1)
        self.assertFalse(should_exit)
        self.assertIsNone(reason)

    def test_trailing_stop_does_not_arm_before_the_threshold(self):
        position = {"entry_price": 100.0, "atr_at_entry": 2.0, "trailing_stop_price": None}
        with mock.patch.object(re, "STOCKS_TRAILING_ARM_ATR_MULT", 1.5):
            result = re.update_trailing_stop(position, current_price=102.0)  # only 1 ATR above entry
        self.assertIsNone(result)

    def test_trailing_stop_arms_and_only_ever_moves_up(self):
        position = {"entry_price": 100.0, "atr_at_entry": 2.0, "trailing_stop_price": None}
        with mock.patch.object(re, "STOCKS_TRAILING_ARM_ATR_MULT", 1.5), \
                mock.patch.object(re, "STOCKS_TRAILING_STOP_ATR_MULT", 2.0):
            first = re.update_trailing_stop(position, current_price=105.0)  # armed
            position["trailing_stop_price"] = first
            second = re.update_trailing_stop(position, current_price=103.0)  # pulled back -- must not lower the stop
        self.assertIsNotNone(first)
        self.assertEqual(second, first)


if __name__ == "__main__":
    unittest.main()
