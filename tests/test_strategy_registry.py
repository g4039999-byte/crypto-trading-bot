"""scripts/strategy_registry.py: record/list/activate a named paper-
trading strategy preset. Isolated from the real data/strategy_versions.json
and .env -- these tests never touch either.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.strategy_registry as registry


class TestStrategyRegistry(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_registry = Path(self._tmp_dir.name) / "strategy_versions.json"
        tmp_env = Path(self._tmp_dir.name) / ".env"
        self._patches = [
            mock.patch.object(registry, "REGISTRY_FILE", tmp_registry),
            mock.patch.object(registry, "ENV_FILE", tmp_env),
        ]
        for p in self._patches:
            p.start()

        # A tiny, deterministic snapshot dataset instead of the real
        # (large, ever-changing) data/snapshots.json -- these tests
        # exercise the registry's own logic, not backtest accuracy
        # (that's scripts/backtest_paper_strategy.py's own job).
        # price_usd is a float here (unlike the real snapshots.json,
        # where it's a JSON string) because this patches _load_snapshots
        # itself, bypassing the string->float coercion that function
        # normally does when reading the real file.
        self._load_snapshots_patch = mock.patch.object(registry, "_load_snapshots", return_value={
            "addr-1": [
                {"timestamp": "2026-01-01T00:00:00+00:00", "price_usd": 1.0, "liquidity_usd": 20000,
                 "volume_24h": 60000, "buys_24h": 90, "sells_24h": 40},
                {"timestamp": "2026-01-01T00:20:00+00:00", "price_usd": 1.6, "liquidity_usd": 20000,
                 "volume_24h": 90000, "buys_24h": 200, "sells_24h": 60},
            ]
        })
        self._load_snapshots_patch.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._load_snapshots_patch.stop()
        self._tmp_dir.cleanup()

    def test_record_version_appends_a_backtest_result(self):
        result = registry.record_version("v2_active", rationale="test")
        self.assertIn("trades", result)
        versions = registry.list_versions()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["name"], "v2_active")
        self.assertEqual(versions[0]["rationale"], "test")
        self.assertIn("backtest", versions[0])

    def test_record_version_rejects_unknown_preset(self):
        with self.assertRaises(KeyError):
            registry.record_version("not_a_real_preset")

    def test_multiple_records_accumulate_in_order(self):
        registry.record_version("v1_legacy")
        registry.record_version("v2_active")
        versions = registry.list_versions()
        self.assertEqual([v["name"] for v in versions], ["v1_legacy", "v2_active"])

    def test_activate_writes_paper_env_overrides_and_creates_the_file(self):
        overrides = registry.activate_version("v1_legacy")
        self.assertIn("PAPER_MIN_SCORE", overrides)
        self.assertEqual(overrides["PAPER_MIN_SCORE"], "80")

        content = registry.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("PAPER_MIN_SCORE=80", content)
        self.assertIn("PAPER_ENTRY_TRENDS=STRONG,RISING", content)
        # v1_legacy has no liquidity-drawdown guard (None) -- must not
        # force an override for it.
        self.assertNotIn("PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT", content)
        # Never touches the shared, live-affecting constant.
        self.assertNotIn("MAX_HOLDING_MINUTES", content)

    def test_activate_preserves_unrelated_existing_env_lines(self):
        registry.ENV_FILE.write_text("SOME_OTHER_KEY=keep_me\nPAPER_MIN_SCORE=999\n", encoding="utf-8")
        registry.activate_version("v2_active")

        content = registry.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("SOME_OTHER_KEY=keep_me", content)
        self.assertIn(f"PAPER_MIN_SCORE={registry.PRESETS['v2_active'].min_score}", content)
        self.assertNotIn("PAPER_MIN_SCORE=999", content)


if __name__ == "__main__":
    unittest.main()
