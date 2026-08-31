import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.kill_switch as kill_switch


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._stop_file = str(Path(self._tmp_dir.name) / "STOP_TRADING")
        self._patches = [
            mock.patch.object(kill_switch, "LIVE_TRADING", True),
            mock.patch.object(kill_switch, "CONFIRM_LIVE_TRADING", kill_switch.REQUIRED_CONFIRM_PHRASE),
            mock.patch.object(kill_switch, "KILL_SWITCH_FILE", self._stop_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()

    def test_blocked_by_default_when_live_trading_false(self):
        with mock.patch.object(kill_switch, "LIVE_TRADING", False):
            result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)
        self.assertTrue(any("LIVE_TRADING" in r for r in result.reasons))

    def test_blocked_when_confirm_phrase_missing(self):
        with mock.patch.object(kill_switch, "CONFIRM_LIVE_TRADING", ""):
            result = kill_switch.trading_allowed()
        self.assertFalse(result.allowed)

    def test_allowed_when_every_gate_passes(self):
        result = kill_switch.trading_allowed()
        self.assertTrue(result.allowed)
        self.assertEqual(result.reasons, [])

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


if __name__ == "__main__":
    unittest.main()
