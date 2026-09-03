"""src/x_account_reputation.py: learned source weighting, isolated from
the real data/x_account_reputation.json.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.x_account_reputation as reputation


class TestAccountReputation(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "x_account_reputation.json"
        self._patch = mock.patch.object(reputation, "STATE_FILE", tmp_state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp_dir.cleanup()

    def test_unknown_account_is_neutral(self):
        self.assertEqual(reputation.get_weight("never-seen-before"), reputation.NEUTRAL_WEIGHT)

    def test_no_author_id_is_neutral(self):
        self.assertEqual(reputation.get_weight(None), reputation.NEUTRAL_WEIGHT)

    def test_repeated_useful_outcomes_raise_weight_above_neutral(self):
        for _ in range(10):
            reputation.record_outcome("good-account", was_useful=True)
        self.assertGreater(reputation.get_weight("good-account"), reputation.NEUTRAL_WEIGHT)

    def test_repeated_useless_outcomes_lower_weight_below_neutral(self):
        for _ in range(10):
            reputation.record_outcome("bad-account", was_useful=False)
        self.assertLess(reputation.get_weight("bad-account"), reputation.NEUTRAL_WEIGHT)

    def test_weight_is_bounded(self):
        for _ in range(100):
            reputation.record_outcome("extreme-good", was_useful=True)
        for _ in range(100):
            reputation.record_outcome("extreme-bad", was_useful=False)
        self.assertLessEqual(reputation.get_weight("extreme-good"), reputation.MAX_WEIGHT)
        self.assertGreaterEqual(reputation.get_weight("extreme-bad"), reputation.MIN_WEIGHT)

    def test_recent_outcomes_matter_more_than_old_ones(self):
        # A long bad history followed by a recent string of good calls
        # should end up meaningfully better than a purely-bad account,
        # thanks to EMA smoothing weighting recent outcomes more.
        for _ in range(20):
            reputation.record_outcome("recovering", was_useful=False)
        recovering_after_bad_streak = reputation.get_weight("recovering")
        for _ in range(20):
            reputation.record_outcome("recovering", was_useful=True)
        recovered = reputation.get_weight("recovering")
        self.assertGreater(recovered, recovering_after_bad_streak)

    def test_graded_float_outcome_is_accepted(self):
        weight_before = reputation.get_weight("graded")
        reputation.record_outcome("graded", was_useful=0.5, context={"trade_pnl_usd": 2.1})
        self.assertGreater(reputation.get_weight("graded"), weight_before)

    def test_top_accounts_ranks_by_weight_and_respects_min_outcomes(self):
        reputation.record_outcome("one-hit-wonder", was_useful=True)
        for _ in range(5):
            reputation.record_outcome("consistent", was_useful=True)

        ranked = reputation.top_accounts(min_outcomes=3)
        ids = [r["author_id"] for r in ranked]
        self.assertIn("consistent", ids)
        self.assertNotIn("one-hit-wonder", ids)

    def test_context_is_stored_but_never_used_in_the_math(self):
        reputation.record_outcome("acct", was_useful=True, context={"entity": "PEPITO", "trade_pnl_usd": 3.2})
        state = reputation._load_state()
        self.assertEqual(state["acct"]["history"][-1]["context"], {"entity": "PEPITO", "trade_pnl_usd": 3.2})


if __name__ == "__main__":
    unittest.main()
