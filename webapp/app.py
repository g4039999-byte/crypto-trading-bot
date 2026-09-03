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

from src.config import KILL_SWITCH_FILE, MAX_OPEN_POSITIONS, TOTAL_CAPITAL_USD  # noqa: E402
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
        top_opportunities.append({
            "symbol": entry.get("symbol") or "?",
            "address_short": _short_address(entry.get("address")),
            "status": _STATUS_LABELS.get(entry.get("status"), entry.get("status") or "?"),
            "score": latest.get("score"),
            "trend": _TREND_LABELS.get(trend, trend or "غير متاح"),
            "news_count": len(entry.get("news") or []),
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
        "max_open_positions": MAX_OPEN_POSITIONS,
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
