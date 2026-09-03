import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.stocks.market_hours import ET, is_market_open, seconds_until_next_open


def _et(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=ET)


class TestIsMarketOpen(unittest.TestCase):
    def test_open_during_regular_session_on_a_weekday(self):
        self.assertTrue(is_market_open(_et(2026, 9, 2, 10, 0)))  # Wednesday

    def test_closed_before_open(self):
        self.assertFalse(is_market_open(_et(2026, 9, 2, 9, 0)))

    def test_closed_at_or_after_close(self):
        self.assertFalse(is_market_open(_et(2026, 9, 2, 16, 0)))
        self.assertFalse(is_market_open(_et(2026, 9, 2, 20, 0)))

    def test_closed_on_saturday(self):
        self.assertFalse(is_market_open(_et(2026, 9, 5, 10, 0)))  # Saturday

    def test_closed_on_sunday(self):
        self.assertFalse(is_market_open(_et(2026, 9, 6, 10, 0)))  # Sunday

    def test_closed_on_a_known_holiday(self):
        self.assertFalse(is_market_open(_et(2026, 12, 25, 10, 0)))  # Christmas, a Friday

    def test_open_right_at_the_opening_second(self):
        self.assertTrue(is_market_open(_et(2026, 9, 2, 9, 30)))

    def test_accepts_a_non_et_timezone_and_converts(self):
        utc_time = datetime(2026, 9, 2, 14, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 ET
        self.assertTrue(is_market_open(utc_time))

    def test_defaults_to_the_real_current_time_without_raising(self):
        is_market_open()  # just must not raise


class TestSecondsUntilNextOpen(unittest.TestCase):
    def test_zero_ish_when_already_open(self):
        self.assertEqual(seconds_until_next_open(_et(2026, 9, 2, 10, 0)), 0.0)

    def test_same_day_countdown_before_the_open(self):
        seconds = seconds_until_next_open(_et(2026, 9, 2, 8, 0))
        self.assertAlmostEqual(seconds, 90 * 60, delta=1)  # 08:00 -> 09:30 = 90 min

    def test_after_close_rolls_to_the_next_trading_day(self):
        seconds = seconds_until_next_open(_et(2026, 9, 2, 17, 0))  # Wed after close
        expected_open = _et(2026, 9, 3, 9, 30)
        self.assertAlmostEqual(seconds, (expected_open - _et(2026, 9, 2, 17, 0)).total_seconds(), delta=1)

    def test_friday_after_close_rolls_to_monday(self):
        seconds = seconds_until_next_open(_et(2026, 9, 4, 17, 0))  # Friday after close
        expected_open = _et(2026, 9, 7, 9, 30)  # Monday (Sep 7 2026 is Labor Day -> Tuesday the 8th)
        # Sep 7 2026 is itself a holiday (Labor Day) in the fixture list above,
        # so the next real open is Tuesday the 8th.
        expected_open = _et(2026, 9, 8, 9, 30)
        self.assertAlmostEqual(seconds, (expected_open - _et(2026, 9, 4, 17, 0)).total_seconds(), delta=1)

    def test_skips_a_holiday_that_falls_on_a_weekday(self):
        seconds = seconds_until_next_open(_et(2026, 12, 24, 17, 0))  # Thu after close
        # Dec 25 2026 is a holiday (Friday) -> next open is Monday Dec 28.
        expected_open = _et(2026, 12, 28, 9, 30)
        self.assertAlmostEqual(seconds, (expected_open - _et(2026, 12, 24, 17, 0)).total_seconds(), delta=1)


if __name__ == "__main__":
    unittest.main()
