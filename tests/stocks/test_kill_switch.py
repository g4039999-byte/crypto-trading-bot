import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.stocks.kill_switch as kill_switch


class TestStocksKillSwitch(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._stop_file = str(Path(self._tmp_dir.name) / "STOP_LIVE_TRADING")
        self._patches = [
            mock.patch.object(kill_switch, "STOCKS_LIVE_TRADING", True),
            mock.patch.object(kill_switch, "STOCKS_CONFIRM_LIVE_TRADING", kill_switch.STOCKS_REQUIRED_CONFIRM_PHRASE),
            mock.patch.object(kill_switch, "STOCKS_KILL_SWITCH_FILE", self._stop_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_blocked_by_default_when_live_trading_false(self):
        with mock.patch.object(kill_switch, "STOCKS_LIVE_TRADING", False):
            result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)
        self.assertTrue(any("STOCKS_LIVE_TRADING" in r for r in result.reasons))

    def test_blocked_when_confirm_phrase_missing(self):
        with mock.patch.object(kill_switch, "STOCKS_CONFIRM_LIVE_TRADING", ""):
            result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)

    def test_blocked_when_confirm_phrase_is_close_but_not_exact(self):
        with mock.patch.object(kill_switch, "STOCKS_CONFIRM_LIVE_TRADING", kill_switch.STOCKS_REQUIRED_CONFIRM_PHRASE + " "):
            result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)

    def test_allowed_when_every_gate_passes(self):
        result = kill_switch.trading_allowed()
        self.assertTrue(result.allowed)
        self.assertEqual(result.reasons, [])

    def test_both_reasons_reported_when_both_gates_closed(self):
        with mock.patch.object(kill_switch, "STOCKS_LIVE_TRADING", False), \
             mock.patch.object(kill_switch, "STOCKS_CONFIRM_LIVE_TRADING", ""):
            result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)
        self.assertEqual(len(result.reasons), 2)

    def test_engage_kill_switch_blocks_immediately(self):
        self.assertTrue(kill_switch.trading_allowed().allowed)
        kill_switch.engage_kill_switch("test stop")
        result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)
        self.assertTrue(any("kill-switch file" in r for r in result.reasons))

    def test_release_kill_switch_restores_access(self):
        kill_switch.engage_kill_switch("test stop")
        self.assertFalse(kill_switch.trading_allowed().allowed)
        kill_switch.release_kill_switch()
        self.assertTrue(kill_switch.trading_allowed().allowed)

    def test_release_kill_switch_is_safe_when_no_file_exists(self):
        kill_switch.release_kill_switch()  # should not raise
        self.assertTrue(kill_switch.trading_allowed().allowed)


class TestStocksKillSwitchRealDefaults(unittest.TestCase):
    """No patching at all -- asserts the values this module actually
    holds in this environment/checkout are the safe ones, i.e. what the
    real running system is in right now.
    """

    def test_live_trading_defaults_to_false(self):
        self.assertFalse(kill_switch.STOCKS_LIVE_TRADING)

    def test_confirm_phrase_is_not_preset_to_the_required_value(self):
        self.assertNotEqual(kill_switch.STOCKS_CONFIRM_LIVE_TRADING, kill_switch.STOCKS_REQUIRED_CONFIRM_PHRASE)

    def test_trading_allowed_is_false_with_real_unpatched_config(self):
        result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)


if __name__ == "__main__":
    unittest.main()
