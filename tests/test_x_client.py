"""src/x_client.py: configuration gating, retries/rate-limit backoff,
and the daily read-budget guard -- all without ever touching the real
X API or the real data/x_usage.json.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.x_client as x_client


class TestIsConfigured(unittest.TestCase):
    def test_false_when_no_bearer_token(self):
        with mock.patch.object(x_client, "X_BEARER_TOKEN", ""), \
                mock.patch.object(x_client, "X_ENABLED", True):
            self.assertFalse(x_client.is_configured())

    def test_false_when_explicitly_disabled_even_with_a_token(self):
        with mock.patch.object(x_client, "X_BEARER_TOKEN", "fake-token"), \
                mock.patch.object(x_client, "X_ENABLED", False):
            self.assertFalse(x_client.is_configured())

    def test_true_when_token_present_and_enabled(self):
        with mock.patch.object(x_client, "X_BEARER_TOKEN", "fake-token"), \
                mock.patch.object(x_client, "X_ENABLED", True):
            self.assertTrue(x_client.is_configured())


class TestSearchRecentGating(unittest.TestCase):
    def test_returns_empty_list_without_any_network_call_when_unconfigured(self):
        with mock.patch.object(x_client, "X_BEARER_TOKEN", ""), \
                mock.patch("requests.get") as mock_get:
            result = x_client.search_recent("solana meme coin")

        self.assertEqual(result, [])
        mock_get.assert_not_called()


class TestSearchRecentWithBudget(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_usage = Path(self._tmp_dir.name) / "x_usage.json"
        self._patches = [
            mock.patch.object(x_client, "USAGE_FILE", tmp_usage),
            mock.patch.object(x_client, "X_BEARER_TOKEN", "fake-token"),
            mock.patch.object(x_client, "X_ENABLED", True),
            mock.patch.object(x_client, "X_MAX_READS_PER_DAY", 50),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def _fake_response(self, posts, users=None, status_code=200):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.headers = {}
        resp.json.return_value = {
            "data": posts,
            "includes": {"users": users or []},
            "meta": {"result_count": len(posts)},
        }
        resp.raise_for_status = mock.Mock()
        return resp

    def test_parses_posts_and_joins_author_info(self):
        posts = [{"id": "1", "text": "check $PEPITO", "author_id": "u1", "created_at": "2026-01-01T00:00:00Z",
                   "public_metrics": {"like_count": 5, "retweet_count": 2, "reply_count": 1}}]
        users = [{"id": "u1", "username": "alice"}]
        with mock.patch("requests.get", return_value=self._fake_response(posts, users)):
            result = x_client.search_recent("solana meme coin")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["author_username"], "alice")
        self.assertEqual(result[0]["like_count"], 5)

    def test_records_reads_against_the_daily_budget(self):
        posts = [{"id": str(i), "text": "x", "author_id": "u", "created_at": "2026-01-01T00:00:00Z"} for i in range(5)]
        with mock.patch("requests.get", return_value=self._fake_response(posts)):
            x_client.search_recent("solana meme coin", max_results=10)

        self.assertEqual(x_client.reads_used_today(), 5)

    def test_skips_the_call_once_the_daily_budget_is_exhausted(self):
        with mock.patch.object(x_client, "_budget_remaining", return_value=0), \
                mock.patch("requests.get") as mock_get:
            result = x_client.search_recent("solana meme coin")

        self.assertEqual(result, [])
        mock_get.assert_not_called()

    def test_returns_empty_list_never_raises_on_malformed_response(self):
        bad_resp = mock.Mock(status_code=200, headers={})
        bad_resp.json.side_effect = ValueError("bad json")
        bad_resp.raise_for_status = mock.Mock()
        with mock.patch("requests.get", return_value=bad_resp), mock.patch("time.sleep"):
            result = x_client.search_recent("solana meme coin")
        self.assertEqual(result, [])

    def test_401_fails_fast_without_retrying(self):
        resp = mock.Mock(status_code=401, headers={})
        with mock.patch("requests.get", return_value=resp) as mock_get:
            result = x_client.search_recent("solana meme coin")

        self.assertEqual(result, [])
        mock_get.assert_called_once()

    def test_429_backs_off_using_the_reset_header_then_succeeds(self):
        import time
        rate_limited = mock.Mock(status_code=429, headers={"x-rate-limit-reset": str(int(time.time()) + 1)})
        success = self._fake_response([])
        with mock.patch("requests.get", side_effect=[rate_limited, success]), \
                mock.patch("time.sleep") as mock_sleep:
            result = x_client.search_recent("solana meme coin")

        self.assertEqual(result, [])
        mock_sleep.assert_called_once()
        waited = mock_sleep.call_args[0][0]
        self.assertLessEqual(waited, x_client.X_RATE_LIMIT_MAX_WAIT_SECONDS)

    def test_rate_limit_wait_is_capped_even_with_a_far_future_reset_header(self):
        resp = mock.Mock(status_code=429, headers={"x-rate-limit-reset": "9999999999"})
        wait = x_client._rate_limit_wait_seconds(resp)
        self.assertLessEqual(wait, x_client.X_RATE_LIMIT_MAX_WAIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
