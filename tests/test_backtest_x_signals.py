"""scripts/backtest_x_signals.py: the synthetic-scenario replay that
validates src.x_account_reputation's learning mechanism actually
rewards consistently-useful accounts and penalizes noise/spam, without
depending on (nonexistent) real historical X data.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.backtest_x_signals as backtest_x
import src.x_account_reputation as reputation


class TestBacktestXSignals(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "x_account_reputation.json"
        self._patch = mock.patch.object(reputation, "STATE_FILE", tmp_state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp_dir.cleanup()

    def test_run_executes_without_raising_and_prints_a_summary(self):
        backtest_x.run()  # smoke test: must not raise

    def test_consistent_account_ends_more_trusted_than_spam_account(self):
        backtest_x.run()
        consistent = reputation.get_weight("consistently_early_and_right")
        spam = reputation.get_weight("spam_account")
        self.assertGreater(consistent, spam)
        self.assertGreater(consistent, reputation.NEUTRAL_WEIGHT)
        self.assertLess(spam, reputation.NEUTRAL_WEIGHT)

    def test_mixed_net_positive_lands_between_neutral_and_the_consistent_account(self):
        backtest_x.run()
        consistent = reputation.get_weight("consistently_early_and_right")
        mixed = reputation.get_weight("mixed_but_net_positive")
        self.assertGreater(mixed, reputation.NEUTRAL_WEIGHT)
        self.assertLess(mixed, consistent)

    def test_brand_new_account_with_no_history_stays_neutral(self):
        backtest_x.run()
        self.assertEqual(reputation.get_weight("brand_new_no_history"), reputation.NEUTRAL_WEIGHT)

    def test_a_recent_negative_streak_outweighs_an_older_equal_positive_one(self):
        # used_to_be_good_now_stale has 8 True *then* 8 False (equal
        # counts, net zero over its full history) -- EMA smoothing
        # weights the recent, consecutive negative run heavily, so it
        # should end up clearly below neutral and well below an account
        # with a consistently positive record, not roughly tied with it
        # just because the raw True/False counts partially cancel out.
        backtest_x.run()
        stale = reputation.get_weight("used_to_be_good_now_stale")
        consistent = reputation.get_weight("consistently_early_and_right")
        self.assertLess(stale, consistent)
        self.assertLess(stale, reputation.NEUTRAL_WEIGHT)


if __name__ == "__main__":
    unittest.main()
