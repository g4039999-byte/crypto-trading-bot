import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.snapshot as snapshot


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_file = Path(self._tmp_dir.name) / "snapshots.json"
        self._patcher = mock.patch.object(snapshot, "SNAPSHOT_FILE", tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp_dir.cleanup()

    def make_pair(self, price="1.23", liquidity=5000, volume=10000, buys=10, sells=5):
        return {
            "priceUsd": price,
            "liquidity": {"usd": liquidity},
            "volume": {"h24": volume},
            "txns": {"h24": {"buys": buys, "sells": sells}},
        }

    def test_load_snapshots_empty_when_file_missing(self):
        self.assertEqual(snapshot.load_snapshots("token-a"), [])

    def test_save_then_load_round_trip(self):
        snapshot.save_snapshot("token-a", self.make_pair())
        history = snapshot.load_snapshots("token-a")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["liquidity_usd"], 5000)

    def test_history_is_trimmed_to_limit(self):
        with mock.patch.object(snapshot, "SNAPSHOT_HISTORY_LIMIT", 3):
            for i in range(5):
                snapshot.save_snapshot("token-a", self.make_pair(price=str(i)))
            history = snapshot.load_snapshots("token-a")
            self.assertEqual(len(history), 3)

    def test_null_liquidity_does_not_raise(self):
        pair = {"priceUsd": "1", "liquidity": None, "volume": {"h24": 100}, "txns": None}
        snapshot.save_snapshot("token-a", pair)  # should not raise
        history = snapshot.load_snapshots("token-a")
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["liquidity_usd"])

    def test_missing_address_is_skipped(self):
        snapshot.save_snapshot("?", self.make_pair())
        snapshot.save_snapshot(None, self.make_pair())
        self.assertFalse(snapshot.SNAPSHOT_FILE.exists())

    def test_corrupt_file_recovers_instead_of_crashing(self):
        snapshot.SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        snapshot.SNAPSHOT_FILE.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(snapshot.load_snapshots("token-a"), [])
        snapshot.save_snapshot("token-a", self.make_pair())
        data = json.loads(snapshot.SNAPSHOT_FILE.read_text(encoding="utf-8"))
        self.assertIn("token-a", data)

    def test_known_addresses_empty_when_no_data(self):
        self.assertEqual(snapshot.known_addresses(), [])

    def test_known_addresses_most_recent_first(self):
        snapshot.save_snapshot("token-old", self.make_pair())
        snapshot.save_snapshot("token-new", self.make_pair())
        # token-old gets a second, more recent snapshot -- it should now
        # sort ahead of token-new.
        snapshot.save_snapshot("token-old", self.make_pair(price="2"))

        self.assertEqual(snapshot.known_addresses(), ["token-old", "token-new"])

    def test_known_addresses_respects_limit(self):
        for i in range(5):
            snapshot.save_snapshot(f"token-{i}", self.make_pair())
        self.assertEqual(len(snapshot.known_addresses(limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
