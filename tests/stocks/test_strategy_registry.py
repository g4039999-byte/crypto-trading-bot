import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stocks import strategy_registry as sr
from src.stocks.backtester import BacktestTrade


def _trade(pnl_pct, in_sample):
    return BacktestTrade(
        symbol="TEST", strategy="momentum", entry_date="2024-01-01", entry_price=100.0,
        exit_date="2024-01-05", exit_price=100.0 * (1 + pnl_pct / 100), reason="take_profit",
        pnl_pct=pnl_pct, confidence=0.5, in_sample=in_sample,
    )


class TestStrategyRegistry(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(sr, "REGISTRY_FILE", Path(self._tmp_dir.name) / "strategy_versions.json"),
            mock.patch.object(sr, "ACTIVE_STRATEGY_FILE", Path(self._tmp_dir.name) / "active_strategy.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_record_version_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            sr.record_version("not_a_strategy")

    def test_record_version_rejects_intraday_only_strategies(self):
        with self.assertRaises(KeyError):
            sr.record_version("vwap_reclaim")

    def test_record_version_appends_to_the_registry(self):
        trades = [_trade(5.0, True), _trade(-2.0, False)]
        with mock.patch.object(sr, "backtest_strategy", return_value=trades), \
             mock.patch.object(sr, "buy_and_hold", return_value=[10.0, 20.0]):
            entry = sr.record_version("momentum", rationale="test run")
        self.assertEqual(entry["strategy"], "momentum")
        self.assertEqual(entry["combined"]["trade_count"], 2)
        self.assertEqual(entry["out_of_sample"]["trade_count"], 1)
        self.assertEqual(sr.list_versions(), [entry])

    def test_get_active_strategy_defaults_to_none(self):
        self.assertIsNone(sr.get_active_strategy())

    def test_activate_and_read_back(self):
        sr.activate_strategy("breakout")
        self.assertEqual(sr.get_active_strategy(), "breakout")

    def test_activate_none_clears_it(self):
        sr.activate_strategy("breakout")
        sr.activate_strategy(None)
        self.assertIsNone(sr.get_active_strategy())

    def test_activate_unknown_strategy_raises(self):
        with self.assertRaises(KeyError):
            sr.activate_strategy("not_a_strategy")

    def test_corrupt_registry_file_degrades_to_empty_not_a_crash(self):
        sr.REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        sr.REGISTRY_FILE.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(sr.list_versions(), [])

    def test_corrupt_active_strategy_file_degrades_to_none(self):
        sr.ACTIVE_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
        sr.ACTIVE_STRATEGY_FILE.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(sr.get_active_strategy())


if __name__ == "__main__":
    unittest.main()
