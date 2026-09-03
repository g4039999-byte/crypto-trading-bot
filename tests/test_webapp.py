"""Tests for webapp/app.py: the local, non-technical control panel.

Covers: (1) it never touches real execution modules or live trading,
(2) Start always launches paper mode, never live, (3) Start/Stop/
Emergency actually control a process rather than being decorative,
(4) the status payload is built correctly from paper-trading state,
(5) the routes wire all of the above together, (6) starting a second
instance on an already-used port fails loudly instead of leaving a
silent, half-alive duplicate process (the actual root cause of the
"Start button hangs forever" incident this module's port guard fixes),
and (7) the launcher .bat script and the dashboard's own JS do their
matching halves of that same fix.
"""

import inspect
import unittest
from pathlib import Path
from unittest import mock

from webapp import app as webapp_module

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestIsolationFromLiveExecution(unittest.TestCase):
    def test_webapp_source_never_imports_wallet_risk_or_live_trader(self):
        forbidden_imports = (
            "import src.wallet", "from src.wallet", "from src import wallet",
            "import src.risk", "from src.risk", "from src import risk",
            "import src.live_trader", "from src.live_trader", "from src import live_trader",
        )
        source = inspect.getsource(webapp_module)
        for forbidden in forbidden_imports:
            self.assertNotIn(forbidden, source, f"{forbidden!r} must never appear in webapp/app.py")

    def test_start_radar_always_uses_the_paper_flag(self):
        source = inspect.getsource(webapp_module.start_radar)
        self.assertIn('"--paper"', source)
        self.assertNotIn('"--live"', source)


class TestBuildStatusPaperOnly(unittest.TestCase):
    def setUp(self):
        patcher_pid = mock.patch.object(webapp_module, "is_radar_running", return_value=False)
        patcher_kill = mock.patch("webapp.app.KILL_SWITCH_FILE", "definitely/does/not/exist")
        self.addCleanup(patcher_pid.stop)
        self.addCleanup(patcher_kill.stop)
        patcher_pid.start()
        patcher_kill.start()

    def test_empty_state_produces_zeroed_but_valid_payload(self):
        with mock.patch.object(webapp_module, "load_paper_state", return_value={
            "open_positions": [], "daily_pnl_usd": {}, "closed_trades": [],
        }), mock.patch.object(webapp_module, "list_all", return_value=[]), \
                mock.patch.object(webapp_module, "active_signals", return_value=[]):
            status = webapp_module.build_status()

        self.assertEqual(status["system_state"], "STOPPED")
        self.assertEqual(status["open_positions"], [])
        self.assertEqual(status["recent_trades"], [])
        self.assertEqual(status["radar"]["top_opportunities"], [])
        self.assertEqual(status["news"], [])
        self.assertEqual(status["balance"]["total_pnl_usd"], 0.0)

    def test_balance_reflects_realized_pnl_and_deployed_capital(self):
        paper_state = {
            "open_positions": [{"symbol": "FOO", "size_usd": 5.0, "entry_price_usd": 0.01, "opened_at": "t"}],
            "daily_pnl_usd": {webapp_module._today_key(): 2.5},
            "closed_trades": [
                {"symbol": "BAR", "pnl_usd": 2.5, "reason": "take_profit", "closed_at": "2026-01-01T00:00:00+00:00"},
            ],
        }
        with mock.patch.object(webapp_module, "load_paper_state", return_value=paper_state), \
                mock.patch.object(webapp_module, "list_all", return_value=[]), \
                mock.patch.object(webapp_module, "active_signals", return_value=[]):
            status = webapp_module.build_status()

        self.assertEqual(status["balance"]["total_pnl_usd"], 2.5)
        self.assertEqual(status["balance"]["today_pnl_usd"], 2.5)
        self.assertEqual(status["balance"]["deployed_usd"], 5.0)
        self.assertEqual(len(status["open_positions"]), 1)
        self.assertEqual(status["recent_trades"][0]["symbol"], "BAR")
        self.assertTrue(status["recent_trades"][0]["is_win"])

    def test_opportunities_are_ranked_by_latest_score_descending(self):
        watchlist = [
            {"symbol": "LOW", "address": "addr1", "status": "WATCHING",
             "history": [{"score": 40, "trend": "NEUTRAL"}], "last_updated_at": "t1"},
            {"symbol": "HIGH", "address": "addr2", "status": "QUALIFIED",
             "history": [{"score": 90, "trend": "STRONG"}], "last_updated_at": "t2"},
        ]
        with mock.patch.object(webapp_module, "load_paper_state", return_value={
            "open_positions": [], "daily_pnl_usd": {}, "closed_trades": [],
        }), mock.patch.object(webapp_module, "list_all", return_value=watchlist), \
                mock.patch.object(webapp_module, "active_signals", return_value=[]):
            status = webapp_module.build_status()

        top = status["radar"]["top_opportunities"]
        self.assertEqual(top[0]["symbol"], "HIGH")
        self.assertEqual(top[1]["symbol"], "LOW")


class TestProcessControlIsReal(unittest.TestCase):
    """These use a fake Popen so no real process is ever spawned by the
    test suite, but assert the real control functions (not a stand-in)
    are exercised end to end.
    """

    def setUp(self):
        webapp_module._managed_process = None
        self.addCleanup(setattr, webapp_module, "_managed_process", None)
        patcher = mock.patch.object(webapp_module, "_clear_pid")
        self.addCleanup(patcher.stop)
        patcher.start()
        patcher2 = mock.patch.object(webapp_module, "_write_pid")
        self.addCleanup(patcher2.stop)
        patcher2.start()

    def test_start_radar_launches_the_paper_radar_module(self):
        fake_proc = mock.Mock(pid=4242)
        fake_proc.poll.return_value = None

        with mock.patch.object(webapp_module, "is_radar_running", return_value=False), \
                mock.patch.object(webapp_module, "release_kill_switch") as mock_release, \
                mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            ok, message = webapp_module.start_radar()

        self.assertTrue(ok)
        mock_release.assert_called_once()
        args, kwargs = mock_popen.call_args
        command = args[0]
        self.assertIn("src.radar", command)
        self.assertIn("--loop", command)
        self.assertIn("--paper", command)
        self.assertNotIn("--live", command)
        self.assertIs(webapp_module._managed_process, fake_proc)

    def test_start_radar_refuses_when_already_running(self):
        with mock.patch.object(webapp_module, "is_radar_running", return_value=True), \
                mock.patch("subprocess.Popen") as mock_popen:
            ok, message = webapp_module.start_radar()

        self.assertFalse(ok)
        mock_popen.assert_not_called()

    def test_stop_radar_kills_the_tracked_process(self):
        webapp_module._managed_process = mock.Mock(pid=777)
        webapp_module._managed_process.wait.return_value = None

        with mock.patch.object(webapp_module, "_pid_is_alive", return_value=True), \
                mock.patch.object(webapp_module, "_kill_pid") as mock_kill:
            ok, message = webapp_module.stop_radar()

        self.assertTrue(ok)
        mock_kill.assert_called_once_with(777)
        self.assertIsNone(webapp_module._managed_process)

    def test_stop_radar_when_nothing_is_running_is_a_no_op(self):
        with mock.patch.object(webapp_module, "_pid_is_alive", return_value=False):
            ok, message = webapp_module.stop_radar()

        self.assertFalse(ok)

    def test_emergency_stop_kills_process_and_engages_kill_switch(self):
        with mock.patch.object(webapp_module, "stop_radar") as mock_stop, \
                mock.patch.object(webapp_module, "engage_kill_switch") as mock_engage:
            ok, message = webapp_module.emergency_stop()

        self.assertTrue(ok)
        mock_stop.assert_called_once()
        mock_engage.assert_called_once()


class TestRoutes(unittest.TestCase):
    def setUp(self):
        webapp_module.app.testing = True
        self.client = webapp_module.app.test_client()

    def test_index_serves_the_dashboard_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("لوحة تحكم", response.get_data(as_text=True))

    def test_status_endpoint_returns_expected_top_level_keys(self):
        with mock.patch.object(webapp_module, "build_status", return_value={"ok": True}):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_start_endpoint_calls_start_radar_and_returns_fresh_status(self):
        with mock.patch.object(webapp_module, "start_radar", return_value=(True, "بدأ")) as mock_start, \
                mock.patch.object(webapp_module, "build_status", return_value={"system_state": "RUNNING"}):
            response = self.client.post("/api/start")

        mock_start.assert_called_once()
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"]["system_state"], "RUNNING")

    def test_stop_endpoint_calls_stop_radar(self):
        with mock.patch.object(webapp_module, "stop_radar", return_value=(True, "توقف")) as mock_stop, \
                mock.patch.object(webapp_module, "build_status", return_value={}):
            response = self.client.post("/api/stop")

        mock_stop.assert_called_once()
        self.assertTrue(response.get_json()["ok"])

    def test_emergency_endpoint_calls_emergency_stop(self):
        with mock.patch.object(webapp_module, "emergency_stop", return_value=(True, "طوارئ")) as mock_emergency, \
                mock.patch.object(webapp_module, "build_status", return_value={}):
            response = self.client.post("/api/emergency")

        mock_emergency.assert_called_once()
        self.assertTrue(response.get_json()["ok"])


class TestPortGuard(unittest.TestCase):
    """Covers the fix for the incident where two instances of the web
    panel ended up bound to the same port: one held the real listening
    socket, the other sat alive but unreachable, and any request routed
    to it (including a Start click) hung forever with no error. The fix
    is a pre-flight bind check, made with our own plain socket
    (deliberately without SO_REUSEADDR) so a second start fails loudly
    and immediately instead of leaving a silent duplicate process.
    """

    def test_port_is_free_returns_true_and_closes_the_probe_socket(self):
        fake_socket = mock.Mock()
        with mock.patch.object(webapp_module.socket, "socket", return_value=fake_socket):
            self.assertTrue(webapp_module._port_is_free("127.0.0.1", 5000))
        fake_socket.bind.assert_called_once_with(("127.0.0.1", 5000))
        fake_socket.close.assert_called_once()

    def test_port_is_free_returns_false_when_bind_raises(self):
        fake_socket = mock.Mock()
        fake_socket.bind.side_effect = OSError("address already in use")
        with mock.patch.object(webapp_module.socket, "socket", return_value=fake_socket):
            self.assertFalse(webapp_module._port_is_free("127.0.0.1", 5000))
        fake_socket.close.assert_called_once()  # closed even on failure -- no leaked probe socket

    def test_main_refuses_to_start_a_second_instance_and_never_calls_app_run(self):
        with mock.patch.object(webapp_module, "_port_is_free", return_value=False), \
                mock.patch.object(webapp_module, "setup_logging"), \
                mock.patch.object(webapp_module.app, "run") as mock_run:
            with self.assertRaises(SystemExit) as ctx:
                webapp_module.main()

        self.assertEqual(ctx.exception.code, 1)
        mock_run.assert_not_called()

    def test_main_starts_the_server_when_the_port_is_free(self):
        with mock.patch.object(webapp_module, "_port_is_free", return_value=True), \
                mock.patch.object(webapp_module, "setup_logging"), \
                mock.patch.object(webapp_module.app, "run") as mock_run:
            webapp_module.main()

        mock_run.assert_called_once_with(host=webapp_module.HOST, port=webapp_module.PORT, debug=False, threaded=True)


class TestDashboardHasARequestTimeout(unittest.TestCase):
    """The JS itself can't run in this Python test suite, so this checks
    the served page's source for the specific mechanism (AbortController
    wired into every fetch call) that keeps a Start/Stop/Emergency click
    -- or the periodic status poll -- from hanging forever if the server
    never responds, same as the isolation tests above check source text
    rather than executing it.
    """

    def setUp(self):
        self.html = (PROJECT_ROOT / "webapp" / "templates" / "index.html").read_text(encoding="utf-8")

    def test_uses_abort_controller_with_a_timeout(self):
        self.assertIn("AbortController", self.html)
        self.assertIn("controller.signal", self.html)
        self.assertIn("ACTION_TIMEOUT_MS", self.html)
        self.assertIn("STATUS_TIMEOUT_MS", self.html)

    def test_every_fetch_call_goes_through_the_timeout_wrapper(self):
        # The only raw fetch(...) call allowed in the whole page is the
        # timeout wrapper's own internal one (identified by it passing
        # the abort signal through) -- every other network request
        # (button actions, periodic status refresh) must call
        # fetchWithTimeout(...) instead, so none of them can hang
        # indefinitely waiting on a server that never responds.
        raw_fetch_lines = [line for line in self.html.splitlines() if "fetch(" in line and "fetchWithTimeout(" not in line]
        self.assertEqual(len(raw_fetch_lines), 1, f"expected exactly one raw fetch() call (the wrapper's own): {raw_fetch_lines!r}")
        self.assertIn("controller.signal", raw_fetch_lines[0])

    def test_timeout_error_produces_a_clear_arabic_message_not_a_silent_hang(self):
        self.assertIn("AbortError", self.html)
        self.assertIn("انتهت مهلة", self.html)


class TestLauncherScriptChecksThePortFirst(unittest.TestCase):
    """The .bat launcher must not blindly start a second server process
    on top of one that's already running -- it should detect the port
    is taken and just (re)open the browser instead.
    """

    def setUp(self):
        self.bat = (PROJECT_ROOT / "تشغيل_الواجهة.bat").read_text(encoding="utf-8")

    def test_checks_port_5000_before_launching_python(self):
        self.assertIn("netstat", self.bat)
        self.assertIn(":5000", self.bat)
        self.assertIn("LISTENING", self.bat)

    def test_has_a_branch_that_skips_launching_python_when_already_running(self):
        # The "already running" branch must exit before reaching the
        # line that starts a second `python -m webapp.app`.
        already_running_idx = self.bat.find("الواجهة تعمل بالفعل")
        exit_idx = self.bat.find("exit /b 0")
        launch_idx = self.bat.find("-m webapp.app")
        self.assertNotEqual(already_running_idx, -1, "no 'already running' message found")
        self.assertNotEqual(exit_idx, -1, "no early exit found for the already-running case")
        self.assertTrue(already_running_idx < exit_idx < launch_idx,
                         "the already-running branch must exit before the line that launches python again")


if __name__ == "__main__":
    unittest.main()
