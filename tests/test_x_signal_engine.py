"""src/x_signal_engine.py: entity extraction, trend clustering,
velocity, spam handling, and TTL-based pruning -- fully isolated from
the real data/x_signals.json.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.x_signal_engine as engine


def _now_minus(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def make_post(post_id, text, author_id="u1", minutes_ago=0):
    return {"id": post_id, "text": text, "author_id": author_id, "created_at": _now_minus(minutes_ago)}


class TestExtractEntities(unittest.TestCase):
    def test_extracts_cashtags(self):
        self.assertEqual(engine.extract_entities("just aped into $PEPITO, huge"), ["PEPITO"])

    def test_extracts_hashtags(self):
        self.assertEqual(engine.extract_entities("new #MOONCOIN just launched"), ["MOONCOIN"])

    def test_bare_ticker_requires_meme_context(self):
        # "PEPITO" alone with no crypto/meme context word is too noisy to trust.
        self.assertEqual(engine.extract_entities("PEPITO went to the store"), [])
        self.assertIn("PEPITO", engine.extract_entities("new solana meme coin PEPITO just launched"))

    def test_common_words_are_denylisted_even_in_context(self):
        result = engine.extract_entities("THE new solana coin is here, ALL aboard")
        self.assertNotIn("THE", result)
        self.assertNotIn("ALL", result)

    def test_empty_text_yields_no_entities(self):
        self.assertEqual(engine.extract_entities(""), [])
        self.assertEqual(engine.extract_entities(None), [])


class TestIsProbableSpam(unittest.TestCase):
    def test_flags_common_spam_phrasing(self):
        self.assertTrue(engine.is_probable_spam("FREE CLAIM airdrop now, DM me for guaranteed profit"))

    def test_does_not_flag_ordinary_text(self):
        self.assertFalse(engine.is_probable_spam("just bought some $PEPITO, looks like a solid new solana coin"))


class TestSignalStateIsolated(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_state = Path(self._tmp_dir.name) / "x_signals.json"
        self._patches = [
            mock.patch.object(engine, "STATE_FILE", tmp_state),
            mock.patch.object(engine, "X_MIN_INDEPENDENT_MENTIONS", 2),
            mock.patch.object(engine, "X_SIGNAL_TTL_MINUTES", 60.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_update_and_active_trends_roundtrip(self):
        posts = [
            make_post("1", "new solana meme coin $PEPITO is pumping", author_id="alice"),
            make_post("2", "$PEPITO looks like a real gem", author_id="bob"),
        ]
        touched = engine.update_signal_state(posts, query="test")
        self.assertEqual(touched, ["PEPITO"])

        trends = engine.active_trends()
        self.assertEqual(len(trends), 1)
        self.assertEqual(trends[0]["entity"], "PEPITO")
        self.assertEqual(trends[0]["independent_mentions"], 2)

    def test_below_min_independent_mentions_gets_zero_confidence(self):
        posts = [make_post("1", "new solana meme coin $LONER just launched", author_id="alice")]
        engine.update_signal_state(posts)
        trends = engine.active_trends()
        self.assertEqual(trends[0]["confidence"], 0.0)

    def test_same_author_repeating_does_not_inflate_independent_mentions(self):
        posts = [
            make_post("1", "new solana meme coin $SPAM is huge", author_id="spammer"),
            make_post("2", "new solana meme coin $SPAM is still huge", author_id="spammer"),
            make_post("3", "new solana meme coin $SPAM keeps going", author_id="spammer"),
        ]
        engine.update_signal_state(posts)
        trends = engine.active_trends()
        self.assertEqual(trends[0]["independent_mentions"], 1)

    def test_spam_flagged_posts_are_excluded_from_independent_mentions(self):
        posts = [
            # lowercase spam phrasing on purpose: the spam-marker regex is
            # case-insensitive, but bare-ticker extraction only matches
            # ALL-CAPS words -- this keeps the test scenario to exactly
            # one entity (FAKE) instead of also picking up "FREE"/"DM" etc.
            make_post("1", "new solana meme coin $FAKE -- free claim airdrop now, dm me guaranteed profit", author_id="a"),
            make_post("2", "new solana meme coin $FAKE looks interesting", author_id="b"),
        ]
        engine.update_signal_state(posts)
        trends = {t["entity"]: t for t in engine.active_trends()}
        self.assertEqual(trends["FAKE"]["independent_mentions"], 1)  # only the non-spam one counts

    def test_duplicate_post_id_is_not_double_counted(self):
        post = make_post("1", "new solana meme coin $DUP is here", author_id="a")
        engine.update_signal_state([post])
        engine.update_signal_state([post])
        trends = engine.active_trends()
        self.assertEqual(trends[0]["total_mentions"], 1)

    def test_stale_cluster_is_excluded_from_active_trends(self):
        with mock.patch.object(engine, "X_SIGNAL_TTL_MINUTES", 10.0):
            posts = [make_post("1", "new solana meme coin $OLDNEWS launched", minutes_ago=60)]
            engine.update_signal_state(posts)
            self.assertEqual(engine.active_trends(), [])

    def test_prune_stale_clusters_removes_only_old_ones(self):
        with mock.patch.object(engine, "X_SIGNAL_TTL_MINUTES", 10.0):
            engine.update_signal_state([make_post("1", "new solana meme coin $STALE here", minutes_ago=100)])
            engine.update_signal_state([make_post("2", "new solana meme coin $FRESH here", minutes_ago=0)])
            removed = engine.prune_stale_clusters()

        self.assertEqual(removed, 1)
        remaining = engine._load_state()
        self.assertIn("FRESH", remaining)
        self.assertNotIn("STALE", remaining)

    def test_reputation_lookup_affects_confidence(self):
        posts = [
            make_post("1", "new solana meme coin $TRUSTED is great", author_id="good1"),
            make_post("2", "new solana meme coin $TRUSTED confirmed", author_id="good2"),
        ]
        engine.update_signal_state(posts)

        low_rep = engine.active_trends(reputation_lookup=lambda _a: 0.2)
        high_rep = engine.active_trends(reputation_lookup=lambda _a: 2.0)
        self.assertLess(low_rep[0]["confidence"], high_rep[0]["confidence"])


if __name__ == "__main__":
    unittest.main()
