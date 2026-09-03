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


class TestStocksIsIndependentOfCrypto(unittest.TestCase):
    def test_start_stocks_never_touches_the_radar_process_or_pid_file(self):
        source = inspect.getsource(webapp_module.start_stocks)
        self.assertIn("src.stocks.run", source)
        self.assertNotIn("src.radar", source)
        self.assertNotIn('"--live"', source)  # quoted form: an actual CLI arg, not the explanatory docstring prose

    def test_stocks_and_crypto_process_state_are_fully_separate(self):
        self.assertIsNot(webapp_module._stocks_lock, webapp_module._lock)
        self.assertNotEqual(webapp_module.STOCKS_PID_FILE, webapp_module.PID_FILE)


class TestBuildStocksStatus(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(webapp_module, "is_stocks_running", return_value=False)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_empty_state_produces_a_valid_zeroed_payload(self):
        with mock.patch("src.stocks.paper_broker.load_state", return_value={"open_positions": [], "closed_trades": [], "daily_pnl_usd": {}}), \
                mock.patch("src.stocks.strategy_registry.get_active_strategy", return_value=None), \
                mock.patch("src.stocks.strategy_registry.list_versions", return_value=[]):
            status = webapp_module.build_stocks_status()

        self.assertFalse(status["live_trading"])
        self.assertEqual(status["open_positions"], [])
        self.assertEqual(status["recent_trades"], [])
        self.assertIsNone(status["active_strategy"])
        self.assertEqual(status["balance"]["closed_trade_count"], 0)

    def test_balance_reflects_realized_pnl_and_open_positions(self):
        state = {
            "open_positions": [{"symbol": "AAPL", "size_usd": 500.0, "entry_price": 100.0, "opened_at": "t", "strategy": "breakout", "entry_score": 70}],
            "closed_trades": [{"symbol": "MSFT", "pnl_usd": 25.0, "pnl_pct": 5.0, "reason": "take_profit", "closed_at": "2026-01-01T00:00:00+00:00", "strategy": "breakout", "was_correct": True}],
            "daily_pnl_usd": {webapp_module._today_key(): 25.0},
        }
        with mock.patch("src.stocks.paper_broker.load_state", return_value=state), \
                mock.patch("src.stocks.strategy_registry.get_active_strategy", return_value="breakout"), \
                mock.patch("src.stocks.strategy_registry.list_versions", return_value=[]):
            status = webapp_module.build_stocks_status()

        self.assertEqual(status["balance"]["total_pnl_usd"], 25.0)
        self.assertEqual(status["balance"]["deployed_usd"], 500.0)
        self.assertEqual(status["balance"]["win_rate_pct"], 100.0)
        self.assertEqual(status["active_strategy"], "breakout")
        self.assertEqual(len(status["open_positions"]), 1)
        self.assertEqual(status["recent_trades"][0]["symbol"], "MSFT")

    def test_never_raises_even_if_paper_broker_state_is_corrupt(self):
        with mock.patch("src.stocks.paper_broker.load_state", side_effect=RuntimeError("corrupt state file")):
            status = webapp_module.build_stocks_status()
        self.assertFalse(status["live_trading"])
        self.assertEqual(status["open_positions"], [])

    def test_live_trading_is_always_reported_false(self):
        with mock.patch("src.stocks.paper_broker.load_state", return_value={"open_positions": [], "closed_trades": [], "daily_pnl_usd": {}}), \
                mock.patch("src.stocks.strategy_registry.get_active_strategy", return_value=None), \
                mock.patch("src.stocks.strategy_registry.list_versions", return_value=[]):
            status = webapp_module.build_stocks_status()
        self.assertFalse(status["live_trading"])

    def test_includes_system_health_and_learning_status(self):
        health_state = {
            "status": "RUNNING", "last_success_at": "t", "consecutive_failures": 0,
            "recovery_attempts_total": 2, "outage_started_at": None,
            "outage_reason": None, "last_recovery_at": "t2",
        }
        learning_state = {"last_run_at": "t3", "last_action": "no_change", "last_action_reason": "breakout still best", "history": [{"at": "t3", "action": "no_change", "reason": "x"}]}
        with mock.patch("src.stocks.paper_broker.load_state", return_value={"open_positions": [], "closed_trades": [], "daily_pnl_usd": {}}), \
                mock.patch("src.stocks.strategy_registry.get_active_strategy", return_value="breakout"), \
                mock.patch("src.stocks.strategy_registry.list_versions", return_value=[]), \
                mock.patch("src.stocks.health.load_health", return_value=health_state), \
                mock.patch("src.stocks.learning_engine.get_learning_state", return_value=learning_state):
            status = webapp_module.build_stocks_status()

        self.assertEqual(status["system_health"]["status"], "RUNNING")
        self.assertEqual(status["system_health"]["recovery_attempts_total"], 2)
        self.assertEqual(status["learning"]["last_action"], "no_change")
        self.assertEqual(len(status["learning"]["recent_history"]), 1)

    def test_health_or_learning_lookup_failure_still_returns_a_safe_payload(self):
        with mock.patch("src.stocks.health.load_health", side_effect=RuntimeError("disk error")):
            status = webapp_module.build_stocks_status()
        self.assertFalse(status["live_trading"])
        self.assertEqual(status["system_health"]["status"], "UNKNOWN")


class TestStocksProcessControlIsReal(unittest.TestCase):
    def setUp(self):
        webapp_module._managed_stocks_process = None
        self.addCleanup(setattr, webapp_module, "_managed_stocks_process", None)
        patcher = mock.patch.object(webapp_module, "_clear_stocks_pid")
        self.addCleanup(patcher.stop)
        patcher.start()
        patcher2 = mock.patch.object(webapp_module, "_write_stocks_pid")
        self.addCleanup(patcher2.stop)
        patcher2.start()

    def test_start_stocks_launches_the_stocks_loop_module(self):
        fake_proc = mock.Mock(pid=5151)
        fake_proc.poll.return_value = None

        with mock.patch.object(webapp_module, "is_stocks_running", return_value=False), \
                mock.patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
            ok, message = webapp_module.start_stocks()

        self.assertTrue(ok)
        args, kwargs = mock_popen.call_args
        command = args[0]
        self.assertIn("src.stocks.run", command)
        self.assertIn("--loop", command)
        self.assertNotIn("--live", command)
        self.assertIs(webapp_module._managed_stocks_process, fake_proc)

    def test_start_stocks_refuses_when_already_running(self):
        with mock.patch.object(webapp_module, "is_stocks_running", return_value=True), \
                mock.patch("subprocess.Popen") as mock_popen:
            ok, message = webapp_module.start_stocks()

        self.assertFalse(ok)
        mock_popen.assert_not_called()

    def test_stop_stocks_kills_the_tracked_process(self):
        webapp_module._managed_stocks_process = mock.Mock(pid=888)
        webapp_module._managed_stocks_process.wait.return_value = None

        with mock.patch.object(webapp_module, "_pid_is_alive", return_value=True), \
                mock.patch.object(webapp_module, "_kill_pid") as mock_kill:
            ok, message = webapp_module.stop_stocks()

        self.assertTrue(ok)
        mock_kill.assert_called_once_with(888)
        self.assertIsNone(webapp_module._managed_stocks_process)

    def test_stop_stocks_when_nothing_running_is_a_no_op(self):
        with mock.patch.object(webapp_module, "_pid_is_alive", return_value=False):
            ok, message = webapp_module.stop_stocks()
        self.assertFalse(ok)


class TestBuildStocksDashboard(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(webapp_module, "is_stocks_running", return_value=True)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _enter_patches(self, patches):
        """A `with`-able bundle of an arbitrary-length list of
        mock.patch(...) context managers (contextlib.ExitStack), so
        _patch_common() below can freely add/remove patches without
        every call site needing to name each one individually -- a
        fixed `with p[0], p[1], ...:` list once silently dropped a
        newly-added patch because a call site wasn't updated to include
        it (see the incident this fixes: _read_last_cycle wasn't
        actually mocked, and a test read the real production
        last_cycle.json).
        """
        import contextlib
        stack = contextlib.ExitStack()
        for p in patches:
            stack.enter_context(p)
        return stack

    def _patch_common(self, state=None, last_cycle=None, versions=None, active_strategy="breakout",
                       learning_state=None, health_state=None, x_configured=False):
        state = state if state is not None else {"open_positions": [], "closed_trades": [], "daily_pnl_usd": {}}
        learning_state = learning_state or {"last_run_at": None, "last_action": None, "last_action_reason": None, "history": []}
        health_state = health_state or {"status": "RUNNING", "last_success_at": "t", "consecutive_failures": 0,
                                         "recovery_attempts_total": 0, "outage_started_at": None,
                                         "outage_reason": None, "last_recovery_at": None, "process_started_at": None}
        return [
            mock.patch("src.stocks.paper_broker.load_state", return_value=state),
            mock.patch("src.stocks.strategy_registry.get_active_strategy", return_value=active_strategy),
            mock.patch("src.stocks.strategy_registry.list_versions", return_value=versions or []),
            mock.patch("src.stocks.learning_engine.get_learning_state", return_value=learning_state),
            mock.patch("src.stocks.health.load_health", return_value=health_state),
            mock.patch("src.x_client.is_configured", return_value=x_configured),
            mock.patch.object(webapp_module, "_read_last_cycle", return_value=last_cycle),
        ]

    def test_opportunities_include_strategy_confidence_for_the_scanner(self):
        # Regression: build_stocks_dashboard()'s own opportunities_view
        # mapping once silently dropped strategy_confidence even though
        # src.stocks.engine already wrote it into last_cycle.json --
        # caught via a real jsdom run against the live server showing
        # "—" instead of a real percentage in the Confidence column.
        last_cycle = {"scanned": 1, "candidates": 1, "opportunities": [
            {"symbol": "AAPL", "score": 70, "strategy": "breakout", "strategy_confidence": 0.82,
             "action": "BUY", "reason": "r", "price": 100.0, "entry_zone": 100.0, "stop_loss": 95.0, "take_profit": 110.0},
        ]}
        patches = self._patch_common(last_cycle=last_cycle)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()
        self.assertEqual(d["opportunities"][0]["strategy_confidence"], 0.82)

    def test_empty_state_is_fully_shaped_and_never_raises(self):
        patches = self._patch_common()
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()

        self.assertFalse(d["live_trading"])
        self.assertIn(d["market_status"], ("OPEN", "PRE_MARKET", "AFTER_HOURS", "CLOSED"))
        self.assertEqual(d["overview"]["open_position_count"], 0)
        self.assertEqual(d["overview"]["closed_trade_count"], 0)
        self.assertEqual(d["opportunities"], [])
        self.assertEqual(d["positions"], [])
        self.assertEqual(d["trades"], [])
        self.assertEqual(d["performance"]["equity_curve"], [])
        self.assertIsNone(d["overview"]["win_rate_pct"])

    def test_open_position_with_a_live_price_shows_unrealized_pnl(self):
        state = {
            "open_positions": [{"symbol": "AAPL", "entry_price": 100.0, "last_price": 110.0, "shares": 10.0,
                                 "size_usd": 1000.0, "opened_at": "2026-01-01T00:00:00+00:00", "strategy": "breakout",
                                 "entry_score": 70, "stop_loss_price": 95.0, "take_profit_price": 120.0}],
            "closed_trades": [], "daily_pnl_usd": {},
        }
        patches = self._patch_common(state=state)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()

        self.assertEqual(d["overview"]["unrealized_pnl_usd"], 100.0)
        self.assertEqual(d["positions"][0]["unrealized_pnl_usd"], 100.0)
        self.assertAlmostEqual(d["positions"][0]["unrealized_pnl_pct"], 10.0)

    def test_open_position_without_a_live_price_yet_shows_none_not_zero(self):
        state = {
            "open_positions": [{"symbol": "AAPL", "entry_price": 100.0, "last_price": None, "shares": 10.0,
                                 "size_usd": 1000.0, "opened_at": "2026-01-01T00:00:00+00:00"}],
            "closed_trades": [], "daily_pnl_usd": {},
        }
        patches = self._patch_common(state=state)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()

        self.assertIsNone(d["positions"][0]["unrealized_pnl_usd"])
        self.assertEqual(d["overview"]["unrealized_pnl_usd"], 0.0)  # excluded from the sum, not treated as a real zero

    def test_closed_trades_feed_performance_series_and_overview_metrics(self):
        closed = [
            {"symbol": "AAPL", "pnl_usd": 50.0, "pnl_pct": 5.0, "opened_at": "2026-01-01T00:00:00+00:00",
             "closed_at": "2026-01-02T00:00:00+00:00", "strategy": "breakout", "reason": "take_profit",
             "regime_snapshot": {"trend": "BULLISH"}},
            {"symbol": "MSFT", "pnl_usd": -20.0, "pnl_pct": -2.0, "opened_at": "2026-01-02T00:00:00+00:00",
             "closed_at": "2026-01-03T00:00:00+00:00", "strategy": "breakout", "reason": "stop_loss",
             "regime_snapshot": {"trend": "SIDEWAYS"}},
        ]
        state = {"open_positions": [], "closed_trades": closed, "daily_pnl_usd": {webapp_module._today_key(): -20.0}}
        patches = self._patch_common(state=state)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()

        self.assertEqual(d["overview"]["closed_trade_count"], 2)
        self.assertEqual(d["overview"]["realized_pnl_usd"], 30.0)
        self.assertEqual(len(d["performance"]["equity_curve"]), 2)
        self.assertEqual(d["performance"]["equity_curve"][-1]["v"], 10030.0)  # starting 10000 + net 30
        self.assertIn("breakout", d["performance"]["by_strategy"])
        self.assertIn("BULLISH", d["performance"]["by_regime"])

    def test_large_pnl_values_are_formatted_not_left_as_raw_floats_or_crashing(self):
        closed = [{"symbol": "X", "pnl_usd": 1234567.891, "pnl_pct": 999.999,
                   "opened_at": "2026-01-01T00:00:00+00:00", "closed_at": "2026-01-02T00:00:00+00:00"}]
        state = {"open_positions": [], "closed_trades": closed, "daily_pnl_usd": {}}
        patches = self._patch_common(state=state)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()
        self.assertEqual(d["overview"]["realized_pnl_usd"], 1234567.89)

    def test_x_disabled_reports_unavailable_not_broken(self):
        patches = self._patch_common(x_configured=False)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()
        self.assertFalse(d["market_intelligence"]["x_status"]["configured"])

    def test_scanner_stopped_is_reflected_in_process_running_and_overview(self):
        with mock.patch.object(webapp_module, "is_stocks_running", return_value=False):
            patches = self._patch_common()
            with self._enter_patches(patches):
                d = webapp_module.build_stocks_dashboard()
        self.assertFalse(d["process_running"])
        self.assertEqual(d["overview"]["system_state"], "STOPPED")

    def test_a_lookup_failure_anywhere_returns_the_safe_empty_shape(self):
        with mock.patch("src.stocks.paper_broker.load_state", side_effect=RuntimeError("disk error")):
            d = webapp_module.build_stocks_dashboard()
        self.assertFalse(d["live_trading"])
        self.assertEqual(d["positions"], [])
        self.assertEqual(d["overview"]["system_state"], "STOPPED")

    def test_degraded_health_status_sets_has_problem(self):
        health_state = {"status": "DEGRADED", "last_success_at": "t", "consecutive_failures": 6,
                         "recovery_attempts_total": 6, "outage_started_at": "t2",
                         "outage_reason": "timeout", "last_recovery_at": None, "process_started_at": None}
        patches = self._patch_common(health_state=health_state)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()
        self.assertTrue(d["overview"]["has_problem"])

    def test_learning_history_is_surfaced_in_the_learning_section(self):
        learning_state = {"last_run_at": "t", "last_action": "adopted", "last_action_reason": "breakout beat momentum",
                           "history": [{"at": "t", "action": "adopted", "reason": "x"}]}
        patches = self._patch_common(learning_state=learning_state)
        with self._enter_patches(patches):
            d = webapp_module.build_stocks_dashboard()
        self.assertEqual(d["learning"]["last_action"], "adopted")
        self.assertEqual(d["overview"]["learning_status"], "adopted")


class TestStocksRoutes(unittest.TestCase):
    def setUp(self):
        webapp_module.app.testing = True
        self.client = webapp_module.app.test_client()

    def test_stocks_status_endpoint(self):
        with mock.patch.object(webapp_module, "build_stocks_status", return_value={"ok": True}):
            response = self.client.get("/api/stocks/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_stocks_start_endpoint_calls_start_stocks_and_returns_fresh_status(self):
        with mock.patch.object(webapp_module, "start_stocks", return_value=(True, "بدأ")) as mock_start, \
                mock.patch.object(webapp_module, "build_stocks_status", return_value={"process_running": True}):
            response = self.client.post("/api/stocks/start")

        mock_start.assert_called_once()
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["status"]["process_running"])

    def test_stocks_stop_endpoint_calls_stop_stocks(self):
        with mock.patch.object(webapp_module, "stop_stocks", return_value=(True, "توقف")) as mock_stop, \
                mock.patch.object(webapp_module, "build_stocks_status", return_value={}):
            response = self.client.post("/api/stocks/stop")

        mock_stop.assert_called_once()
        self.assertTrue(response.get_json()["ok"])

    def test_stocks_dashboard_page_serves_and_is_rtl_arabic(self):
        response = self.client.get("/stocks")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('dir="rtl"', html)
        self.assertIn('lang="ar"', html)

    def test_stocks_dashboard_endpoint_calls_build_stocks_dashboard(self):
        with mock.patch.object(webapp_module, "build_stocks_dashboard", return_value={"ok": True}):
            response = self.client.get("/api/stocks/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_stocks_restart_endpoint_stops_then_starts(self):
        call_order = []
        with mock.patch.object(webapp_module, "stop_stocks", side_effect=lambda: call_order.append("stop") or (True, "توقف")), \
                mock.patch.object(webapp_module, "start_stocks", side_effect=lambda: call_order.append("start") or (True, "بدأ")), \
                mock.patch.object(webapp_module, "build_stocks_dashboard", return_value={}):
            response = self.client.post("/api/stocks/restart")

        self.assertEqual(call_order, ["stop", "start"])
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


class TestStocksDashboardPageIsSound(unittest.TestCase):
    """Same source-text-check discipline as TestDashboardHasARequestTimeout
    above, applied to the new full /stocks page -- it's a much larger
    page with its own polling loop and its own set of control buttons,
    and must not have quietly skipped the timeout-wrapper convention the
    original dashboard already established.
    """

    def setUp(self):
        self.html = (PROJECT_ROOT / "webapp" / "templates" / "stocks_dashboard.html").read_text(encoding="utf-8")

    def test_is_rtl_arabic(self):
        self.assertIn('lang="ar"', self.html)
        self.assertIn('dir="rtl"', self.html)

    def test_uses_abort_controller_with_a_timeout(self):
        self.assertIn("AbortController", self.html)
        self.assertIn("controller.signal", self.html)
        self.assertIn("ACTION_TIMEOUT_MS", self.html)
        self.assertIn("STATUS_TIMEOUT_MS", self.html)

    def test_every_fetch_call_goes_through_the_timeout_wrapper(self):
        raw_fetch_lines = [line for line in self.html.splitlines() if "fetch(" in line and "fetchWithTimeout(" not in line]
        self.assertEqual(len(raw_fetch_lines), 1, f"expected exactly one raw fetch() call (the wrapper's own): {raw_fetch_lines!r}")
        self.assertIn("controller.signal", raw_fetch_lines[0])

    def test_polls_the_new_dashboard_endpoint(self):
        self.assertIn("/api/stocks/dashboard", self.html)

    def test_control_buttons_call_paper_only_endpoints(self):
        # No control on this page can reach a live-trading endpoint --
        # there isn't one to reach, but the page's own action wiring
        # must still only ever target the paper-only stocks API.
        for endpoint in ("/api/stocks/start", "/api/stocks/stop", "/api/stocks/restart"):
            self.assertIn(endpoint, self.html)
        self.assertNotIn("/api/live", self.html)
        self.assertNotIn("live_trading=true", self.html.lower())

    def test_live_trading_is_shown_as_permanently_locked_not_a_toggle(self):
        self.assertIn("Live Trading", self.html)
        self.assertIn("مقفل دائمًا", self.html)
        # A literal input/button that could flip live trading must not exist.
        self.assertNotIn('id="btn-live"', self.html)
        self.assertNotIn('id="toggle-live"', self.html)

    def test_notifications_never_let_a_render_error_break_the_poll_loop(self):
        # checkNotifications() is called every refresh() -- it must be
        # defensively wrapped so a malformed/unexpected payload shape
        # can't stop the dashboard from continuing to poll.
        start = self.html.index("function checkNotifications")
        end = self.html.index("\n}", start)
        body = self.html[start:end]
        self.assertIn("try {", body)
        self.assertIn("catch", body)

    def test_has_every_required_section(self):
        for section_id in ("overview", "scanner", "positions", "history", "performance", "lab", "learning", "intel", "health"):
            self.assertIn(f'id="{section_id}"', self.html)


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
