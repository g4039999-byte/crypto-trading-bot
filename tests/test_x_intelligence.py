"""src/x_intelligence.py: the orchestration + resilience boundary
between X and the rest of the project. These tests are the ones that
matter most for "X being down must never take down Radar/Paper
Trading" -- every scenario here asserts the function returns a safe
default (0, [], None) instead of raising, even when a dependency
explodes.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.x_intelligence as xi


class TestMaybePollAndUpdate(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_poll_state = Path(self._tmp_dir.name) / "x_poll_state.json"
        self._patch = mock.patch.object(xi, "POLL_STATE_FILE", tmp_poll_state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp_dir.cleanup()

    def test_noop_and_zero_network_calls_when_not_configured(self):
        with mock.patch.object(xi, "is_configured", return_value=False), \
                mock.patch.object(xi, "search_recent") as mock_search:
            result = xi.maybe_poll_and_update()

        self.assertEqual(result, 0)
        mock_search.assert_not_called()

    def test_polls_every_configured_query_when_configured(self):
        with mock.patch.object(xi, "is_configured", return_value=True), \
                mock.patch.object(xi, "X_SEARCH_QUERIES", ("query one", "query two")), \
                mock.patch.object(xi, "search_recent", return_value=[{"id": "1", "text": "$PEPITO gem", "author_id": "a"}]) as mock_search, \
                mock.patch.object(xi, "update_signal_state", return_value=["PEPITO"]), \
                mock.patch.object(xi, "prune_stale_clusters", return_value=0):
            result = xi.maybe_poll_and_update()

        self.assertEqual(mock_search.call_count, 2)
        self.assertEqual(result, 1)

    def test_skips_polling_again_before_the_interval_elapses(self):
        with mock.patch.object(xi, "is_configured", return_value=True), \
                mock.patch.object(xi, "search_recent", return_value=[]) as mock_search, \
                mock.patch.object(xi, "update_signal_state", return_value=[]), \
                mock.patch.object(xi, "prune_stale_clusters", return_value=0):
            xi.maybe_poll_and_update()
            mock_search.reset_mock()
            result = xi.maybe_poll_and_update()  # immediately again

        mock_search.assert_not_called()
        self.assertEqual(result, 0)

    def test_one_query_raising_does_not_stop_the_others(self):
        with mock.patch.object(xi, "is_configured", return_value=True), \
                mock.patch.object(xi, "X_SEARCH_QUERIES", ("bad query", "good query")), \
                mock.patch.object(xi, "search_recent", side_effect=[RuntimeError("boom"), [{"id": "1", "text": "$OK gem", "author_id": "a"}]]), \
                mock.patch.object(xi, "update_signal_state", return_value=["OK"]) as mock_update, \
                mock.patch.object(xi, "prune_stale_clusters", return_value=0):
            result = xi.maybe_poll_and_update()

        mock_update.assert_called_once()
        self.assertEqual(result, 1)

    def test_a_totally_unexpected_exception_still_returns_zero_not_raise(self):
        with mock.patch.object(xi, "is_configured", side_effect=RuntimeError("completely broken")):
            result = xi.maybe_poll_and_update()
        self.assertEqual(result, 0)


class TestGetActiveTrends(unittest.TestCase):
    def test_returns_empty_list_when_active_trends_raises(self):
        with mock.patch.object(xi, "active_trends", side_effect=RuntimeError("boom")):
            self.assertEqual(xi.get_active_trends(), [])

    def test_passes_the_reputation_lookup_through(self):
        with mock.patch.object(xi, "active_trends", return_value=[{"entity": "X"}]) as mock_active:
            xi.get_active_trends()
        mock_active.assert_called_once()
        self.assertIn("reputation_lookup", mock_active.call_args.kwargs)


class TestSocialSignalForToken(unittest.TestCase):
    def test_returns_none_with_no_active_trends(self):
        with mock.patch.object(xi, "get_active_trends", return_value=[]):
            self.assertIsNone(xi.social_signal_for_token("addr-1", []))

    def test_returns_none_and_does_not_raise_when_correlation_explodes(self):
        with mock.patch.object(xi, "social_score_for_token", side_effect=RuntimeError("boom")):
            result = xi.social_signal_for_token("addr-1", [], trend_summaries=[{"entity": "X"}])
        self.assertIsNone(result)

    def test_returns_the_correlated_signal_on_success(self):
        expected = {"entity": "PEPITO", "confidence": 0.7, "is_possible_clone": False}
        with mock.patch.object(xi, "social_score_for_token", return_value=expected):
            result = xi.social_signal_for_token("addr-1", [], trend_summaries=[{"entity": "PEPITO"}])
        self.assertEqual(result, expected)


class TestScoreBonusForSignal(unittest.TestCase):
    def test_no_signal_means_zero_bonus(self):
        self.assertEqual(xi.score_bonus_for_signal(None), 0)

    def test_possible_clone_means_zero_bonus_regardless_of_confidence(self):
        signal = {"confidence": 1.0, "is_possible_clone": True}
        self.assertEqual(xi.score_bonus_for_signal(signal), 0)

    def test_bonus_scales_with_confidence_up_to_the_configured_max(self):
        with mock.patch.object(xi, "X_SCORE_MAX_BONUS", 10):
            full = xi.score_bonus_for_signal({"confidence": 1.0, "is_possible_clone": False})
            half = xi.score_bonus_for_signal({"confidence": 0.5, "is_possible_clone": False})
        self.assertEqual(full, 10)
        self.assertEqual(half, 5)


if __name__ == "__main__":
    unittest.main()
