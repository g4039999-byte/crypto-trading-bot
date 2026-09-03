"""End-to-end: a mocked X API response -> x_client -> x_signal_engine
-> x_correlation -> x_intelligence -> a real score bonus for a
radar-shaped token. Every other X test file mocks its module's direct
dependency; this one wires the real modules together (only the HTTP
call itself is mocked) to prove the full chain actually interoperates,
not just each piece in isolation.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import src.x_account_reputation as reputation
import src.x_client as x_client
import src.x_intelligence as xi
import src.x_signal_engine as engine


def _now_iso():
    # Real posts need real "now" timestamps, not a hardcoded date --
    # x_signal_engine's TTL logic compares against actual wall-clock
    # time, so a fixed past date would make every cluster look stale.
    return datetime.now(timezone.utc).isoformat()


def _fake_search_response(posts, users=None):
    resp = mock.Mock(status_code=200, headers={})
    resp.json.return_value = {"data": posts, "includes": {"users": users or []}, "meta": {"result_count": len(posts)}}
    resp.raise_for_status = mock.Mock()
    return resp


class TestFullXPipeline(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp = lambda name: Path(self._tmp_dir.name) / name  # noqa: E731
        self._patches = [
            mock.patch.object(x_client, "USAGE_FILE", tmp("x_usage.json")),
            mock.patch.object(x_client, "X_BEARER_TOKEN", "fake-token"),
            mock.patch.object(x_client, "X_ENABLED", True),
            mock.patch.object(engine, "STATE_FILE", tmp("x_signals.json")),
            mock.patch.object(engine, "X_MIN_INDEPENDENT_MENTIONS", 2),
            mock.patch.object(reputation, "STATE_FILE", tmp("x_reputation.json")),
            mock.patch.object(xi, "POLL_STATE_FILE", tmp("x_poll_state.json")),
            mock.patch.object(xi, "X_SEARCH_QUERIES", ("solana meme coin",)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_real_x_trend_correlates_to_a_token_and_produces_a_score_bonus(self):
        posts = [
            {"id": "1", "text": "new solana meme coin $PEPITO just launched, huge potential", "author_id": "alice",
             "created_at": _now_iso(), "public_metrics": {"like_count": 10, "retweet_count": 3, "reply_count": 1}},
            {"id": "2", "text": "everyone's talking about $PEPITO on solana today", "author_id": "bob",
             "created_at": _now_iso(), "public_metrics": {"like_count": 5, "retweet_count": 1, "reply_count": 0}},
            {"id": "3", "text": "$PEPITO confirmed real gem, buying more", "author_id": "carol",
             "created_at": _now_iso(), "public_metrics": {"like_count": 8, "retweet_count": 2, "reply_count": 0}},
        ]
        users = [{"id": "alice", "username": "alice"}, {"id": "bob", "username": "bob"}, {"id": "carol", "username": "carol"}]

        with mock.patch("requests.get", return_value=_fake_search_response(posts, users)):
            touched = xi.maybe_poll_and_update()
        self.assertEqual(touched, 1)  # PEPITO

        candidate_tokens = [{"symbol": "PEPITO", "address": "addr-pepito", "liquidity": 20000, "age": 30}]
        signal = xi.social_signal_for_token("addr-pepito", candidate_tokens)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["entity"], "PEPITO")
        self.assertEqual(signal["independent_mentions"], 3)
        self.assertGreater(signal["confidence"], 0)
        self.assertFalse(signal["is_possible_clone"])

        bonus = xi.score_bonus_for_signal(signal)
        self.assertGreater(bonus, 0)

    def test_clone_of_an_established_token_gets_no_bonus(self):
        # The real hype on X is about the established name, $PEPITO --
        # x_correlation.correlate() then separately notices a different,
        # much newer/less-liquid token whose symbol is suspiciously
        # similar (a classic O/1 typosquat) and flags THAT one, not the
        # real $PEPITO match.
        posts = [
            {"id": "1", "text": "new solana meme coin $PEPITO just launched, huge potential", "author_id": "alice",
             "created_at": _now_iso()},
            {"id": "2", "text": "$PEPITO is blowing up on solana right now", "author_id": "bob",
             "created_at": _now_iso()},
        ]
        with mock.patch("requests.get", return_value=_fake_search_response(posts)):
            xi.maybe_poll_and_update()

        candidate_tokens = [
            {"symbol": "PEPITO", "address": "addr-original", "liquidity": 200000, "age": 500},  # established
            {"symbol": "PEP1TO", "address": "addr-clone", "liquidity": 4000, "age": 3},           # brand new imitator
        ]
        original_signal = xi.social_signal_for_token("addr-original", candidate_tokens)
        clone_signal = xi.social_signal_for_token("addr-clone", candidate_tokens)

        self.assertIsNotNone(original_signal)
        self.assertFalse(original_signal["is_possible_clone"])
        self.assertGreater(xi.score_bonus_for_signal(original_signal), 0)

        self.assertIsNotNone(clone_signal)
        self.assertTrue(clone_signal["is_possible_clone"])
        self.assertEqual(xi.score_bonus_for_signal(clone_signal), 0)

    def test_x_disabled_produces_no_signal_at_all_and_no_state_written(self):
        with mock.patch.object(x_client, "X_BEARER_TOKEN", ""), mock.patch("requests.get") as mock_get:
            touched = xi.maybe_poll_and_update()

        self.assertEqual(touched, 0)
        mock_get.assert_not_called()
        self.assertEqual(xi.get_active_trends(), [])


if __name__ == "__main__":
    unittest.main()
