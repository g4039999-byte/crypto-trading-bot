"""Local, single-user web control panel for a non-technical end user.

Run with (from the project root):   python -m webapp.app
then open:                          http://127.0.0.1:5000

What this is and is NOT:
  - It is a thin presentation + process-control layer over functionality
    that already exists in this project. It adds no new trading rule,
    no new entry/exit logic, and no new scoring logic -- it only reads
    existing state files (via src.paper_portfolio, src.opportunity_watchlist,
    src.news_signal_engine, src.config) and starts/stops the exact same
    process a person could already start by hand from a terminal:
        python -m src.radar --loop --paper
  - The Start button always launches PAPER trading (`--paper`). This
    file never imports src.live_trader or src.wallet, and never sets
    LIVE_TRADING -- there is no code path here that can place a real
    order, regardless of what buttons are clicked.
  - Emergency Stop does two independent things: it kills the running
    radar process AND engages src.kill_switch (the same kill switch
    src/live_trader.py already checks before every real order), so it
    is the strongest stop available in this project, not just a UI
    state flip.
  - Binds to 127.0.0.1 only (not 0.0.0.0): this is a local control
    panel for the one person running it on this machine, not a service
    exposed to the network.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    # Defensive: makes `python webapp/app.py` work too, not just
    # `python -m webapp.app` from the project root (which already puts
    # the root on sys.path automatically).
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import KILL_SWITCH_FILE, PAPER_MAX_OPEN_POSITIONS, TOTAL_CAPITAL_USD  # noqa: E402
from src.kill_switch import engage_kill_switch, release_kill_switch  # noqa: E402
from src.logging_config import setup_logging  # noqa: E402
from src.news_signal_engine import active_signals  # noqa: E402
from src.opportunity_watchlist import list_all  # noqa: E402
from src.paper_portfolio import load_state as load_paper_state  # noqa: E402

logger = logging.getLogger(__name__)

app = Flask(__name__)

HOST = "127.0.0.1"
PORT = 5000

PID_FILE = PROJECT_ROOT / "data" / "webapp_radar.pid"

# ---------------------------------------------------------------------------
# Process control: start/stop `python -m src.radar --loop --paper` as a
# background process. Tracked both in-memory (this Flask process) and via
# a PID file (so Stop/status still work correctly even if the web server
# itself was restarted after Start was pressed).
# ---------------------------------------------------------------------------

# Reentrant on purpose: start_radar() acquires this and then, while still
# holding it, calls is_radar_running() -- which also acquires it. A plain
# threading.Lock() self-deadlocks on that (a thread can't re-acquire a
# lock it already holds), which freezes the whole single-threaded Flask
# dev server on the very first Start click. threading.RLock() allows the
# same thread to re-enter safely.
_lock = threading.RLock()
_managed_process = None  # subprocess.Popen | None


def _is_windows():
    return os.name == "nt"


def _write_pid(pid):
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _read_pid():
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _clear_pid():
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _pid_is_alive(pid):
    """Best-effort liveness check. Never raises -- an unreadable process
    table just means "assume not running" rather than crashing a status
    check.
    """
    if pid is None:
        return False
    try:
        if _is_windows():
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def is_radar_running():
    with _lock:
        if _managed_process is not None and _managed_process.poll() is None:
            return True
    return _pid_is_alive(_read_pid())


def start_radar():
    """Start the paper radar loop unless one is already running. Always
    releases the kill switch first, so pressing Start after an
    Emergency Stop is enough to resume on its own.

    Returns (ok: bool, message_ar: str).
    """
    global _managed_process
    with _lock:
        if is_radar_running():
            return False, "التداول يعمل بالفعل."

        release_kill_switch()

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _is_windows() else 0
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "src.radar", "--loop", "--paper"],
                cwd=str(PROJECT_ROOT),
                creationflags=creationflags,
            )
        except OSError as exc:
            logger.exception("Failed to start the radar process")
            return False, f"تعذّر بدء التداول: {exc}"

        _managed_process = proc
        _write_pid(proc.pid)
        logger.warning("Web UI: paper radar loop started (pid=%s)", proc.pid)
        return True, "تم بدء التداول التجريبي (Paper Trading) بنجاح."


def _kill_pid(pid):
    if pid is None:
        return
    try:
        if _is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        logger.exception("Could not stop radar process pid=%s", pid)


def stop_radar():
    """Stop the running radar loop, if any. Returns (ok, message_ar).

    Bounded to a few seconds worst case (5s taskkill + 5s wait below) so
    a Stop click can never block the request handler anywhere near as
    long as the browser's own fetch timeout -- see ACTION_TIMEOUT_MS in
    templates/index.html, which is kept comfortably above this budget on
    purpose.
    """
    global _managed_process
    with _lock:
        pid = _managed_process.pid if _managed_process is not None else _read_pid()

        if not _pid_is_alive(pid):
            _managed_process = None
            _clear_pid()
            return False, "التداول متوقف بالفعل."

        _kill_pid(pid)
        if _managed_process is not None:
            try:
                _managed_process.wait(timeout=5)
            except Exception:
                pass
        _managed_process = None
        _clear_pid()
        logger.warning("Web UI: paper radar loop stopped (pid=%s)", pid)
        return True, "تم إيقاف التداول."


def emergency_stop():
    """Kill the radar process (best-effort) AND engage the project's
    kill switch. Never raises -- this must always succeed at recording
    "stop everything" even if the process was already gone.
    """
    stop_radar()
    engage_kill_switch(reason="emergency stop pressed in the web UI")
    logger.warning("Web UI: EMERGENCY STOP engaged")
    return True, "تم تفعيل إيقاف الطوارئ. اضغط تشغيل التداول لاستئناف العمل."


# ---------------------------------------------------------------------------
# Stocks process control -- runs `python -m src.stocks.run --loop`
# (US stocks, paper trading only) as its own independent background
# process, entirely separate from the crypto radar above: its own lock,
# its own PID file, its own Start/Stop. Either system can be started,
# stopped, or broken without affecting the other. Deliberately a
# parallel copy of the crypto control-flow above (not a shared helper)
# so this addition can never risk the crypto side's already-working,
# already-tested process control.
# ---------------------------------------------------------------------------

STOCKS_PID_FILE = PROJECT_ROOT / "data" / "webapp_stocks.pid"
STOCKS_LAST_CYCLE_FILE = PROJECT_ROOT / "data" / "stocks" / "last_cycle.json"

_stocks_lock = threading.RLock()
_managed_stocks_process = None


def _write_stocks_pid(pid):
    STOCKS_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOCKS_PID_FILE.write_text(str(pid), encoding="utf-8")


def _read_stocks_pid():
    if not STOCKS_PID_FILE.exists():
        return None
    try:
        return int(STOCKS_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _clear_stocks_pid():
    try:
        STOCKS_PID_FILE.unlink()
    except OSError:
        pass


def is_stocks_running():
    with _stocks_lock:
        if _managed_stocks_process is not None and _managed_stocks_process.poll() is None:
            return True
    return _pid_is_alive(_read_stocks_pid())


def start_stocks():
    """Start the US-stocks paper-trading loop. Always paper -- there is
    no --live flag on src.stocks.run, and STOCKS_LIVE_TRADING is
    hard-set False at the source level (src/stocks/config.py); nothing
    here can place a real brokerage order regardless of what's clicked.
    Returns (ok: bool, message_ar: str).
    """
    global _managed_stocks_process
    with _stocks_lock:
        if is_stocks_running():
            return False, "تداول الأسهم يعمل بالفعل."

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _is_windows() else 0
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "src.stocks.run", "--loop"],
                cwd=str(PROJECT_ROOT),
                creationflags=creationflags,
            )
        except OSError as exc:
            logger.exception("Failed to start the stocks process")
            return False, f"تعذّر بدء تداول الأسهم: {exc}"

        _managed_stocks_process = proc
        _write_stocks_pid(proc.pid)
        logger.warning("Web UI: stocks paper loop started (pid=%s)", proc.pid)
        return True, "تم بدء التداول التجريبي للأسهم الأمريكية بنجاح."


def stop_stocks():
    """Stop the running stocks loop, if any. Returns (ok, message_ar)."""
    global _managed_stocks_process
    with _stocks_lock:
        pid = _managed_stocks_process.pid if _managed_stocks_process is not None else _read_stocks_pid()

        if not _pid_is_alive(pid):
            _managed_stocks_process = None
            _clear_stocks_pid()
            return False, "تداول الأسهم متوقف بالفعل."

        _kill_pid(pid)
        if _managed_stocks_process is not None:
            try:
                _managed_stocks_process.wait(timeout=5)
            except Exception:
                pass
        _managed_stocks_process = None
        _clear_stocks_pid()
        logger.warning("Web UI: stocks paper loop stopped (pid=%s)", pid)
        return True, "تم إيقاف تداول الأسهم."


# ---------------------------------------------------------------------------
# Read-only data for the dashboard -- Arabic labels for display only, the
# underlying values (status/trend/event_type/...) are exactly what the
# existing project modules already produce.
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "NEW": "🆕 جديدة",
    "WATCHING": "👀 قيد المراقبة",
    "QUALIFIED": "✅ مؤهلة",
    "REJECTED": "❌ مرفوضة",
    "EXPIRED": "⌛ منتهية الصلاحية",
}

_TREND_LABELS = {
    "STRONG": "🚀 قوي جدًا",
    "RISING": "📈 صاعد",
    "WEAK": "📉 ضعيف",
    "NEUTRAL": "➖ مستقر",
    "INSUFFICIENT_DATA": "بيانات غير كافية بعد",
    "ERROR": "تعذّر التحليل",
}

_EVENT_TYPE_LABELS = {
    "LISTING": "📋 إدراج في منصة",
    "PARTNERSHIP": "🤝 شراكة",
    "REGULATORY": "⚖️ تنظيمي / قانوني",
    "HACK_EXPLOIT": "🚨 اختراق / استغلال",
    "MACRO": "🌐 اقتصاد كلي",
    "GENERIC": "📰 عام",
}

_SENTIMENT_LABELS = {
    "POSITIVE": "إيجابي 🙂",
    "NEGATIVE": "سلبي 🙁",
    "NEUTRAL": "محايد",
}

_URGENCY_LABELS = {
    "HIGH": "عاجل 🔴",
    "MEDIUM": "متوسط 🟠",
    "LOW": "عادي 🟢",
}

_EXIT_REASON_LABELS = {
    "stop_loss": "وقف خسارة",
    "take_profit": "جني أرباح",
    "max_holding_time": "انتهاء مدة الاحتفاظ",
}


def _fmt_money(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _short_address(address):
    if not address or not isinstance(address, str) or len(address) <= 10:
        return address or "?"
    return f"{address[:4]}…{address[-4:]}"


def _latest_history(entry):
    history = entry.get("history") or []
    return history[-1] if history else {}


def _read_last_cycle():
    """The last cycle's summary (incl. the opportunities snapshot),
    written by src.stocks.engine every cycle -- or None if no cycle has
    ever run yet, or the file is missing/corrupt. Reads STOCKS_LAST_
    CYCLE_FILE as a module-level constant specifically so tests can
    redirect it -- an inline path here once meant a test's synthetic
    run_cycle() call silently overwrote the real, live production file
    (see tests/stocks/test_engine.py's _EngineTestIsolation docstring
    for the incident this fixes).
    """
    if not STOCKS_LAST_CYCLE_FILE.exists():
        return None
    try:
        return json.loads(STOCKS_LAST_CYCLE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_stocks_status():
    """Everything the US-stocks dashboard section needs. Never raises,
    never makes a live network/data-provider call itself -- reads only
    data/stocks/*.json (paper positions, the last cycle's own summary
    written by src.stocks.engine, the strategy registry). A completely
    fresh install (no cycle has ever run) returns valid, empty-looking
    data, not an error.
    """
    try:
        from src.stocks import health as stocks_health
        from src.stocks import learning_engine as stocks_learning
        from src.stocks.config import STOCKS_LIVE_TRADING, STOCKS_MAX_OPEN_POSITIONS, STOCKS_STARTING_CAPITAL_USD
        from src.stocks.paper_broker import load_state as load_stocks_state
        from src.stocks.strategy_registry import get_active_strategy, list_versions

        process_running = is_stocks_running()
        state = load_stocks_state()
        open_positions = state.get("open_positions", [])
        closed_trades = state.get("closed_trades", [])

        total_pnl = _fmt_money(sum((t.get("pnl_usd") or 0) for t in closed_trades))
        today_pnl = _fmt_money(state.get("daily_pnl_usd", {}).get(_today_key(), 0.0))
        deployed = _fmt_money(sum((p.get("size_usd") or 0) for p in open_positions))
        balance_estimate = _fmt_money(STOCKS_STARTING_CAPITAL_USD + total_pnl)
        wins = [t for t in closed_trades if (t.get("pnl_usd") or 0) > 0]
        win_rate = round(100 * len(wins) / len(closed_trades), 1) if closed_trades else None
        peak_equity = state.get("peak_equity_usd", STOCKS_STARTING_CAPITAL_USD)
        drawdown_pct = round(max(0.0, (peak_equity - balance_estimate) / peak_equity * 100), 2) if peak_equity else 0.0

        positions_view = [{
            "symbol": p.get("symbol"), "size_usd": _fmt_money(p.get("size_usd")),
            "entry_price": p.get("entry_price"), "opened_at": p.get("opened_at"),
            "strategy": p.get("strategy"), "entry_score": p.get("entry_score"),
            "entry_reason": p.get("entry_reason"),
            "stop_loss_price": p.get("stop_loss_price"), "take_profit_price": p.get("take_profit_price"),
            "trailing_stop_price": p.get("trailing_stop_price"),
            "relative_volume": (p.get("features_snapshot") or {}).get("relative_volume"),
        } for p in open_positions]

        recent_closed = sorted(closed_trades, key=lambda t: t.get("closed_at") or "", reverse=True)[:10]
        trades_view = [{
            "symbol": t.get("symbol"), "pnl_usd": _fmt_money(t.get("pnl_usd")),
            "pnl_pct": round(t.get("pnl_pct"), 2) if t.get("pnl_pct") is not None else None,
            "is_win": (t.get("pnl_usd") or 0) > 0, "reason": t.get("reason"),
            "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"), "strategy": t.get("strategy"),
            "was_correct": t.get("was_correct"), "entry_score": t.get("entry_score"),
            "entry_reason": t.get("entry_reason"),
            "mfe_pct": round(t["mfe_pct"], 2) if t.get("mfe_pct") is not None else None,
            "mae_pct": round(t["mae_pct"], 2) if t.get("mae_pct") is not None else None,
        } for t in recent_closed]

        last_cycle = _read_last_cycle()

        versions = list_versions()
        last_adopted = versions[-1] if versions else None

        health_state = stocks_health.load_health()
        learning_state = stocks_learning.get_learning_state()

        return {
            "process_running": process_running,
            "live_trading": STOCKS_LIVE_TRADING,  # always False -- shown so the dashboard can assert it, never toggle it
            "balance": {
                "estimated_balance_usd": balance_estimate,
                "deployed_usd": deployed,
                "today_pnl_usd": today_pnl,
                "total_pnl_usd": total_pnl,
                "starting_capital_usd": _fmt_money(STOCKS_STARTING_CAPITAL_USD),
                "win_rate_pct": win_rate,
                "drawdown_pct": drawdown_pct,
                "closed_trade_count": len(closed_trades),
            },
            "open_positions": positions_view,
            "recent_trades": trades_view,
            "max_open_positions": STOCKS_MAX_OPEN_POSITIONS,
            "active_strategy": get_active_strategy(),
            "last_adopted_version": last_adopted,
            "last_cycle": last_cycle,
            "system_health": {
                "status": health_state.get("status"),
                "last_success_at": health_state.get("last_success_at"),
                "consecutive_failures": health_state.get("consecutive_failures"),
                "recovery_attempts_total": health_state.get("recovery_attempts_total"),
                "outage_started_at": health_state.get("outage_started_at"),
                "outage_reason": health_state.get("outage_reason"),
                "last_recovery_at": health_state.get("last_recovery_at"),
            },
            "learning": {
                "last_run_at": learning_state.get("last_run_at"),
                "last_action": learning_state.get("last_action"),
                "last_action_reason": learning_state.get("last_action_reason"),
                "recent_history": (learning_state.get("history") or [])[-5:],
            },
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.exception("Failed to build stocks status -- returning a safe empty payload")
        return {
            "process_running": False, "live_trading": False,
            "balance": {"estimated_balance_usd": 0, "deployed_usd": 0, "today_pnl_usd": 0, "total_pnl_usd": 0,
                        "starting_capital_usd": 0, "win_rate_pct": None, "drawdown_pct": 0, "closed_trade_count": 0},
            "open_positions": [], "recent_trades": [], "max_open_positions": 0,
            "active_strategy": None, "last_adopted_version": None, "last_cycle": None,
            "system_health": {"status": "UNKNOWN", "last_success_at": None, "consecutive_failures": None,
                               "recovery_attempts_total": None, "outage_started_at": None,
                               "outage_reason": None, "last_recovery_at": None},
            "learning": {"last_run_at": None, "last_action": None, "last_action_reason": None, "recent_history": []},
            "server_time": datetime.now(timezone.utc).isoformat(),
        }


_STOCKS_EMPTY_DASHBOARD = {
    "process_running": False, "live_trading": False, "market_status": "CLOSED",
    "overview": {
        "system_state": "STOPPED", "equity_usd": 0, "available_cash_usd": 0, "deployed_usd": 0,
        "unrealized_pnl_usd": 0, "realized_pnl_usd": 0, "daily_pnl_usd": 0, "total_pnl_usd": 0,
        "starting_capital_usd": 0, "drawdown_pct": 0, "win_rate_pct": None, "profit_factor": None,
        "expectancy_pct": None, "closed_trade_count": 0, "open_position_count": 0, "opportunity_count": 0,
        "active_strategy": None, "learning_status": None, "has_problem": False,
    },
    "opportunities": [], "positions": [], "trades": [],
    "performance": {"equity_curve": [], "daily_pnl": [], "cumulative_pnl": [], "drawdown_curve": [],
                     "win_loss_distribution": [], "by_strategy": {}, "by_regime": {}, "by_ticker": {}, "avg_win_vs_avg_loss": {}},
    "strategy_lab": {"active_strategy": None, "versions": [], "daily_strategies": []},
    "learning": {"last_run_at": None, "last_action": None, "last_action_reason": None, "recent_history": []},
    "market_intelligence": {"regime": None, "x_status": {"configured": False, "enabled": False}, "data_source_health": {}},
    "system_health": {"status": "UNKNOWN", "last_success_at": None, "consecutive_failures": None,
                       "recovery_attempts_total": None, "outage_started_at": None, "outage_reason": None,
                       "last_recovery_at": None, "process_started_at": None, "uptime_seconds": None, "last_cycle": None},
    "server_time": None,
}


def _uptime_seconds(process_started_at):
    if not process_started_at:
        return None
    try:
        started = datetime.fromisoformat(process_started_at)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except (ValueError, TypeError):
        return None


def _pnl_class(value):
    if value is None:
        return "flat"
    return "pos" if value > 0 else ("neg" if value < 0 else "flat")


def build_stocks_dashboard():
    """The full aggregate payload for the new /stocks dashboard page --
    strictly additive to build_stocks_status() above (that endpoint is
    untouched, still used by the compact section on the original
    dashboard). Same non-negotiable rule: reads only local state files
    (paper_positions.json, last_cycle.json, strategy_versions.json,
    learning_state.json, health_status.json) plus in-memory config
    (X configured/enabled) -- NEVER a live data-provider/broker/X call,
    so polling this endpoint can never itself trigger network traffic,
    burn rate-limit budget, or block on a slow upstream. Never raises:
    any failure degrades to _STOCKS_EMPTY_DASHBOARD, a safe, fully-shaped
    empty payload the frontend can always render without special-casing.
    """
    try:
        from src.stocks import health as stocks_health
        from src.stocks import learning_engine as stocks_learning
        from src.stocks import market_hours as stocks_market_hours
        from src.stocks.config import STOCKS_LIVE_TRADING, STOCKS_MAX_OPEN_POSITIONS, STOCKS_STARTING_CAPITAL_USD
        from src.stocks.paper_broker import load_state as load_stocks_state
        from src.stocks.performance import compute_metrics
        from src.stocks.strategy_registry import DAILY_STRATEGIES, get_active_strategy, list_versions
        from src.x_client import is_configured as x_is_configured

        process_running = is_stocks_running()
        market_status = stocks_market_hours.market_status()
        state = load_stocks_state()
        open_positions = state.get("open_positions", [])
        closed_trades = state.get("closed_trades", [])

        realized_pnl = sum((t.get("pnl_usd") or 0) for t in closed_trades)
        unrealized_pnl = sum(
            ((p.get("last_price") - p["entry_price"]) * p["shares"])
            for p in open_positions if p.get("last_price") is not None and p.get("entry_price") and p.get("shares")
        )
        deployed = sum((p.get("size_usd") or 0) for p in open_positions)
        equity = STOCKS_STARTING_CAPITAL_USD + realized_pnl + unrealized_pnl
        available_cash = STOCKS_STARTING_CAPITAL_USD + realized_pnl - deployed
        daily_pnl = state.get("daily_pnl_usd", {}).get(_today_key(), 0.0)

        peak_equity = state.get("peak_equity_usd", STOCKS_STARTING_CAPITAL_USD)
        realized_balance = STOCKS_STARTING_CAPITAL_USD + realized_pnl
        drawdown_pct = round(max(0.0, (peak_equity - realized_balance) / peak_equity * 100), 2) if peak_equity else 0.0

        closed_pnl_pcts = [t["pnl_pct"] for t in closed_trades if t.get("pnl_pct") is not None]
        overall_metrics = compute_metrics(closed_pnl_pcts)

        last_cycle = _read_last_cycle()

        health_state = stocks_health.load_health()
        learning_state = stocks_learning.get_learning_state()
        versions = list_versions()
        active_strategy = get_active_strategy()

        opportunities = (last_cycle or {}).get("opportunities") or []
        opportunities_view = [{
            "symbol": o.get("symbol"), "price": o.get("price"), "pct_change_1d": o.get("pct_change_1d"),
            "volume": o.get("volume"), "relative_volume": o.get("relative_volume"), "atr_pct": o.get("atr_pct"),
            "score": o.get("score"), "strategy": o.get("strategy"), "action": o.get("action"), "reason": o.get("reason"),
            "entry_zone": o.get("entry_zone"), "stop_loss": o.get("stop_loss"), "take_profit": o.get("take_profit"),
            "risk_reward": o.get("risk_reward"),
        } for o in opportunities]

        positions_view = []
        for p in open_positions:
            entry_price, last_price, shares = p.get("entry_price"), p.get("last_price"), p.get("shares")
            unrealized_usd = (last_price - entry_price) * shares if last_price is not None and entry_price and shares else None
            unrealized_pct = (last_price - entry_price) / entry_price * 100 if last_price is not None and entry_price else None
            held_seconds = None
            try:
                held_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(p["opened_at"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                pass
            positions_view.append({
                "symbol": p.get("symbol"), "entry_price": entry_price, "current_price": last_price,
                "size_usd": _fmt_money(p.get("size_usd")), "unrealized_pnl_usd": _fmt_money(unrealized_usd) if unrealized_usd is not None else None,
                "unrealized_pnl_pct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
                "stop_loss_price": p.get("stop_loss_price"), "take_profit_price": p.get("take_profit_price"),
                "trailing_stop_price": p.get("trailing_stop_price"), "held_hours": round(held_seconds / 3600, 1) if held_seconds is not None else None,
                "opened_at": p.get("opened_at"), "strategy": p.get("strategy"), "entry_score": p.get("entry_score"),
                "entry_reason": p.get("entry_reason"), "last_price_at": p.get("last_price_at"),
            })

        trades_view = []
        for t in sorted(closed_trades, key=lambda t: t.get("closed_at") or "", reverse=True)[:200]:
            held_hours = None
            try:
                held_hours = round((datetime.fromisoformat(t["closed_at"]) - datetime.fromisoformat(t["opened_at"])).total_seconds() / 3600, 1)
            except (KeyError, ValueError, TypeError):
                pass
            trades_view.append({
                "symbol": t.get("symbol"), "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
                "size_usd": _fmt_money(t.get("size_usd")), "pnl_usd": _fmt_money(t.get("pnl_usd")),
                "pnl_pct": round(t["pnl_pct"], 2) if t.get("pnl_pct") is not None else None,
                "is_win": (t.get("pnl_usd") or 0) > 0, "held_hours": held_hours, "strategy": t.get("strategy"),
                "entry_score": t.get("entry_score"), "entry_reason": t.get("entry_reason"), "reason": t.get("reason"),
                "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"),
                "mfe_pct": round(t["mfe_pct"], 2) if t.get("mfe_pct") is not None else None,
                "mae_pct": round(t["mae_pct"], 2) if t.get("mae_pct") is not None else None,
                "was_correct": t.get("was_correct"),
            })

        performance = _build_stocks_performance(closed_trades, STOCKS_STARTING_CAPITAL_USD)

        strategy_lab = {
            "active_strategy": active_strategy,
            "daily_strategies": list(DAILY_STRATEGIES),
            "versions": versions[-10:],  # most recent first for display
        }

        regime = (last_cycle or {}).get("regime")
        market_intel = {
            "regime": regime,
            "x_status": {"configured": x_is_configured(), "enabled": x_is_configured()},
            "data_source_health": {
                "scanner": health_state.get("status"),
                "last_cycle_scanned": (last_cycle or {}).get("scanned"),
                "last_cycle_candidates": (last_cycle or {}).get("candidates"),
            },
        }

        has_problem = health_state.get("status") in ("DEGRADED", "RECOVERING") or not process_running

        return {
            "process_running": process_running,
            "live_trading": STOCKS_LIVE_TRADING,
            "market_status": market_status,
            "overview": {
                "system_state": "RUNNING" if process_running else "STOPPED",
                "equity_usd": _fmt_money(equity),
                "available_cash_usd": _fmt_money(available_cash),
                "deployed_usd": _fmt_money(deployed),
                "unrealized_pnl_usd": _fmt_money(unrealized_pnl),
                "realized_pnl_usd": _fmt_money(realized_pnl),
                "daily_pnl_usd": _fmt_money(daily_pnl),
                "total_pnl_usd": _fmt_money(realized_pnl + unrealized_pnl),
                "starting_capital_usd": _fmt_money(STOCKS_STARTING_CAPITAL_USD),
                "drawdown_pct": drawdown_pct,
                "win_rate_pct": overall_metrics.get("win_rate_pct"),
                "profit_factor": overall_metrics.get("profit_factor"),
                "expectancy_pct": overall_metrics.get("expectancy_pct"),
                "closed_trade_count": len(closed_trades),
                "open_position_count": len(open_positions),
                "opportunity_count": len(opportunities_view),
                "max_open_positions": STOCKS_MAX_OPEN_POSITIONS,
                "active_strategy": active_strategy,
                "learning_status": learning_state.get("last_action"),
                "has_problem": has_problem,
            },
            "opportunities": opportunities_view,
            "positions": positions_view,
            "trades": trades_view,
            "performance": performance,
            "strategy_lab": strategy_lab,
            "learning": {
                "last_run_at": learning_state.get("last_run_at"),
                "last_action": learning_state.get("last_action"),
                "last_action_reason": learning_state.get("last_action_reason"),
                "recent_history": (learning_state.get("history") or [])[-10:],
            },
            "market_intelligence": market_intel,
            "system_health": {
                "status": health_state.get("status"),
                "last_success_at": health_state.get("last_success_at"),
                "consecutive_failures": health_state.get("consecutive_failures"),
                "recovery_attempts_total": health_state.get("recovery_attempts_total"),
                "outage_started_at": health_state.get("outage_started_at"),
                "outage_reason": health_state.get("outage_reason"),
                "last_recovery_at": health_state.get("last_recovery_at"),
                "process_started_at": health_state.get("process_started_at"),
                "uptime_seconds": _uptime_seconds(health_state.get("process_started_at")),
                "last_cycle": last_cycle,
            },
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.exception("Failed to build stocks dashboard -- returning a safe empty payload")
        return {**_STOCKS_EMPTY_DASHBOARD, "server_time": datetime.now(timezone.utc).isoformat()}


def _build_stocks_performance(closed_trades, starting_capital):
    """Chart-ready series computed from closed_trades, sorted oldest-
    first by close time -- equity curve, daily P/L, cumulative P/L,
    drawdown curve (all on this project's summed-return dollar
    convention, consistent with src.stocks.performance's documented
    choice not to compound), win/loss distribution buckets, and
    breakdowns by strategy/regime/ticker. Pure computation, no I/O.
    """
    ordered = sorted((t for t in closed_trades if t.get("closed_at")), key=lambda t: t["closed_at"])

    equity_curve, cumulative_pnl, drawdown_curve = [], [], []
    running_total, peak = 0.0, 0.0
    for t in ordered:
        running_total += t.get("pnl_usd") or 0
        peak = max(peak, running_total)
        equity_curve.append({"t": t["closed_at"], "v": round(starting_capital + running_total, 2)})
        cumulative_pnl.append({"t": t["closed_at"], "v": round(running_total, 2)})
        drawdown_curve.append({"t": t["closed_at"], "v": round(running_total - peak, 2)})

    daily = {}
    for t in ordered:
        day = t["closed_at"][:10]
        daily[day] = daily.get(day, 0.0) + (t.get("pnl_usd") or 0)
    daily_pnl = [{"t": day, "v": round(v, 2)} for day, v in sorted(daily.items())]

    wins = [t["pnl_pct"] for t in ordered if (t.get("pnl_usd") or 0) > 0 and t.get("pnl_pct") is not None]
    losses = [t["pnl_pct"] for t in ordered if (t.get("pnl_usd") or 0) <= 0 and t.get("pnl_pct") is not None]
    buckets = [(-100, -10), (-10, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 10), (10, 100)]
    win_loss_distribution = []
    all_pcts = [t["pnl_pct"] for t in ordered if t.get("pnl_pct") is not None]
    for lo, hi in buckets:
        count = sum(1 for p in all_pcts if lo <= p < hi)
        win_loss_distribution.append({"range": f"{lo}% إلى {hi}%", "count": count})

    def _group_metrics(key_fn):
        from src.stocks.performance import compute_metrics
        groups = {}
        for t in ordered:
            key = key_fn(t)
            if key is None:
                continue
            groups.setdefault(key, []).append(t.get("pnl_pct"))
        return {k: compute_metrics([p for p in v if p is not None]) for k, v in groups.items()}

    by_strategy = _group_metrics(lambda t: t.get("strategy"))
    by_regime = _group_metrics(lambda t: (t.get("regime_snapshot") or {}).get("trend"))
    by_ticker = _group_metrics(lambda t: t.get("symbol"))

    return {
        "equity_curve": equity_curve,
        "daily_pnl": daily_pnl,
        "cumulative_pnl": cumulative_pnl,
        "drawdown_curve": drawdown_curve,
        "win_loss_distribution": win_loss_distribution,
        "by_strategy": by_strategy,
        "by_regime": by_regime,
        "by_ticker": by_ticker,
        "avg_win_vs_avg_loss": {
            "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        },
    }


def build_status():
    """Everything the dashboard needs, in one JSON-serializable dict.
    Never raises: every sub-lookup already degrades gracefully in the
    underlying modules (empty state, missing file, etc.), and this
    function does no I/O of its own beyond calling those.
    """
    kill_switch_engaged = Path(KILL_SWITCH_FILE).exists()
    process_running = is_radar_running()

    if kill_switch_engaged:
        system_state, system_label = "EMERGENCY", "🛑 تم تفعيل إيقاف الطوارئ"
    elif process_running:
        system_state, system_label = "RUNNING", "🟢 التداول يعمل الآن"
    else:
        system_state, system_label = "STOPPED", "🔴 التداول متوقف"

    paper_state = load_paper_state()
    open_positions = paper_state.get("open_positions", [])
    closed_trades = paper_state.get("closed_trades", [])

    total_pnl = _fmt_money(sum((t.get("pnl_usd") or 0) for t in closed_trades))
    today_pnl = _fmt_money(paper_state.get("daily_pnl_usd", {}).get(_today_key(), 0.0))
    deployed = _fmt_money(sum((p.get("size_usd") or 0) for p in open_positions))
    balance_estimate = _fmt_money(TOTAL_CAPITAL_USD + total_pnl)
    available_cash = _fmt_money(TOTAL_CAPITAL_USD + total_pnl - deployed)

    positions_view = [
        {
            "symbol": p.get("symbol") or "?",
            "size_usd": _fmt_money(p.get("size_usd")),
            "entry_price_usd": p.get("entry_price_usd"),
            "opened_at": p.get("opened_at"),
            "entry_score": p.get("entry_score"),
            "entry_trend": p.get("entry_trend"),
        }
        for p in open_positions
    ]

    recent_closed = sorted(closed_trades, key=lambda t: t.get("closed_at") or "", reverse=True)[:10]
    trades_view = [
        {
            "symbol": t.get("symbol") or "?",
            "pnl_usd": _fmt_money(t.get("pnl_usd")),
            "is_win": (t.get("pnl_usd") or 0) > 0,
            "reason": _EXIT_REASON_LABELS.get(t.get("reason"), t.get("reason") or "?"),
            "closed_at": t.get("closed_at"),
            "entry_score": t.get("entry_score"),
            "entry_trend": t.get("entry_trend"),
        }
        for t in recent_closed
    ]

    watchlist = list_all()
    ranked = sorted(
        watchlist,
        key=lambda e: (_latest_history(e).get("score") if _latest_history(e).get("score") is not None else -1),
        reverse=True,
    )
    top_opportunities = []
    for entry in ranked[:6]:
        latest = _latest_history(entry)
        trend = latest.get("trend")
        x_signal = latest.get("x_signal")
        top_opportunities.append({
            "symbol": entry.get("symbol") or "?",
            "address_short": _short_address(entry.get("address")),
            "status": _STATUS_LABELS.get(entry.get("status"), entry.get("status") or "?"),
            "score": latest.get("score"),
            "trend": _TREND_LABELS.get(trend, trend or "غير متاح"),
            "news_count": len(entry.get("news") or []),
            "x_entity": x_signal.get("entity") if x_signal else None,
            "x_confidence": x_signal.get("confidence") if x_signal else None,
            "x_mentions": x_signal.get("independent_mentions") if x_signal else None,
            "x_possible_clone": bool(x_signal.get("is_possible_clone")) if x_signal else False,
        })

    last_update = None
    timestamps = [e.get("last_updated_at") for e in watchlist if e.get("last_updated_at")]
    if timestamps:
        last_update = max(timestamps)

    news_items = []
    for signal_ in active_signals()[:6]:
        text = signal_.get("text") or ""
        if len(text) > 140:
            text = text[:140].rstrip() + "…"
        news_items.append({
            "event_type": _EVENT_TYPE_LABELS.get(signal_.get("event_type"), signal_.get("event_type") or "?"),
            "sentiment": _SENTIMENT_LABELS.get(signal_.get("sentiment"), signal_.get("sentiment") or "?"),
            "urgency": _URGENCY_LABELS.get(signal_.get("urgency"), signal_.get("urgency") or "?"),
            "assets": ", ".join(signal_.get("affected_assets") or []) or "—",
            "text": text,
        })

    return {
        "system_state": system_state,
        "system_label": system_label,
        "kill_switch_engaged": kill_switch_engaged,
        "process_running": process_running,
        "balance": {
            "estimated_balance_usd": balance_estimate,
            "available_cash_usd": available_cash,
            "deployed_usd": deployed,
            "today_pnl_usd": today_pnl,
            "total_pnl_usd": total_pnl,
            "starting_capital_usd": _fmt_money(TOTAL_CAPITAL_USD),
        },
        "open_positions": positions_view,
        "recent_trades": trades_view,
        "radar": {
            "tracked_count": len(watchlist),
            "last_update": last_update,
            "top_opportunities": top_opportunities,
        },
        "news": news_items,
        "max_open_positions": PAPER_MAX_OPEN_POSITIONS,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(build_status())


@app.route("/api/start", methods=["POST"])
def api_start():
    ok, message = start_radar()
    return jsonify({"ok": ok, "message": message, "status": build_status()})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, message = stop_radar()
    return jsonify({"ok": ok, "message": message, "status": build_status()})


@app.route("/api/emergency", methods=["POST"])
def api_emergency():
    ok, message = emergency_stop()
    return jsonify({"ok": ok, "message": message, "status": build_status()})


@app.route("/api/stocks/status")
def api_stocks_status():
    return jsonify(build_stocks_status())


@app.route("/api/stocks/start", methods=["POST"])
def api_stocks_start():
    ok, message = start_stocks()
    return jsonify({"ok": ok, "message": message, "status": build_stocks_status()})


@app.route("/api/stocks/stop", methods=["POST"])
def api_stocks_stop():
    ok, message = stop_stocks()
    return jsonify({"ok": ok, "message": message, "status": build_stocks_status()})


@app.route("/stocks")
def stocks_dashboard_page():
    return render_template("stocks_dashboard.html")


@app.route("/api/stocks/dashboard")
def api_stocks_dashboard():
    return jsonify(build_stocks_dashboard())


@app.route("/api/stocks/restart", methods=["POST"])
def api_stocks_restart():
    """Stop then start the stocks loop in one server-side action -- the
    dashboard's "Restart Scanner" button. Still paper-only: calls the
    exact same start_stocks()/stop_stocks() every other control uses.
    """
    stop_stocks()
    ok, message = start_stocks()
    return jsonify({"ok": ok, "message": message, "status": build_stocks_dashboard()})


def _port_is_free(host, port):
    """True if `host:port` is free to bind right now, checked with our
    own throwaway socket -- deliberately WITHOUT SO_REUSEADDR, so this
    fails reliably the moment something else (including a previous,
    still-running instance of this same web panel) is already listening
    there.

    This exists because Werkzeug's dev server sets SO_REUSEADDR on the
    socket it actually serves from, and on Windows that can let a
    second `app.run()` on an already-used port bind "successfully" but
    never actually receive any request -- a silent, half-alive second
    process that can leave the UI's buttons hanging forever with no
    error, instead of the second start failing loudly like it should.
    Checking with our own plain socket first avoids relying on that
    Werkzeug behavior at all.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main():
    setup_logging()

    if not _port_is_free(HOST, PORT):
        message = (
            f"المنفذ {PORT} مستخدَم بالفعل -- يبدو أن لوحة التحكم تعمل مسبقًا.\n"
            f"افتح المتصفح مباشرة على http://{HOST}:{PORT} بدلاً من تشغيل نسخة جديدة،\n"
            f"أو أغلق العملية التي تستخدم هذا المنفذ أولًا إن كنت تريد إعادة التشغيل."
        )
        logger.error(
            "Refusing to start: port %s is already in use on %s -- "
            "the web control panel is probably already running. Not "
            "starting a second instance.",
            PORT, HOST,
        )
        # Plain text on purpose (no emoji): this console may be running
        # under a non-UTF-8 Windows codepage (e.g. cp1256), which raises
        # UnicodeEncodeError on some emoji -- the one message meant to
        # tell a non-technical user exactly what went wrong must never
        # itself crash before it can be printed.
        print(f"\nخطأ: {message}\n")
        sys.exit(1)

    logger.info("Starting the web control panel on http://%s:%s", HOST, PORT)
    # threaded=True: without it, Werkzeug's dev server handles one
    # request at a time. Stop/Start both do a few seconds of bounded
    # subprocess work (see stop_radar) while holding the request handler
    # -- on a single-threaded server that would also stall the page's
    # own periodic /api/status poll (and any other open tab) for the
    # same few seconds instead of just the one action button.
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
