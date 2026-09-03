"""System-health / auto-recovery bookkeeping for the continuous stocks
loop (src.stocks.engine.run_forever). Answers, for the dashboard and
for the loop itself: is a cycle currently succeeding, how long has it
been failing, why, and how many recovery attempts has it made -- and
gives run_forever() the exponential-backoff-with-a-cap it needs to
survive a transient outage (rate limit, timeout, connection error, any
temporary data-provider failure) without ever needing a human to
restart the process.

Never raises: every function here degrades to a safe default on a
read/write failure of its own state file, the same convention as every
other src.stocks module.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.stocks.config import (
    STOCKS_HEALTH_FILE_NAME,
    STOCKS_RECOVERY_BACKOFF_BASE_SECONDS,
    STOCKS_RECOVERY_BACKOFF_MAX_SECONDS,
)

logger = logging.getLogger(__name__)

HEALTH_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / STOCKS_HEALTH_FILE_NAME

_DEFAULT_STATE = {
    "status": "STARTING",  # STARTING | RUNNING | RECOVERING | DEGRADED
    "last_success_at": None,
    "last_success_summary": None,
    "consecutive_failures": 0,
    "recovery_attempts_total": 0,
    "outage_started_at": None,
    "outage_reason": None,
    "last_recovery_at": None,
    "last_updated_at": None,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_health():
    if not HEALTH_FILE.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def _save_health(state):
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        state["last_updated_at"] = _now_iso()
        HEALTH_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("Could not persist stocks health state -- non-fatal")


def record_success(summary=None):
    """Call after a cycle completes without raising. Clears any
    in-progress outage and resets the failure streak.
    """
    state = load_health()
    was_recovering = state["status"] in ("RECOVERING", "DEGRADED")
    state["status"] = "RUNNING"
    state["last_success_at"] = _now_iso()
    state["last_success_summary"] = summary
    state["consecutive_failures"] = 0
    if was_recovering:
        state["last_recovery_at"] = _now_iso()
        logger.warning(
            "Stocks loop recovered after %s failed cycle(s) (outage started %s, reason: %s)",
            state.get("recovery_attempts_total"), state.get("outage_started_at"), state.get("outage_reason"),
        )
    state["outage_started_at"] = None
    state["outage_reason"] = None
    _save_health(state)
    return state


def record_failure(reason):
    """Call after a cycle raises. Returns the backoff delay (seconds)
    the caller should sleep before retrying -- exponential in the
    consecutive-failure count, capped so a long outage still gets
    retried at a sane, bounded interval rather than growing forever.
    """
    state = load_health()
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    state["recovery_attempts_total"] = state.get("recovery_attempts_total", 0) + 1
    state["outage_reason"] = str(reason)[:500]
    if state.get("outage_started_at") is None:
        state["outage_started_at"] = _now_iso()
    state["status"] = "DEGRADED" if state["consecutive_failures"] >= 5 else "RECOVERING"
    _save_health(state)

    delay = min(
        STOCKS_RECOVERY_BACKOFF_BASE_SECONDS * (2 ** (state["consecutive_failures"] - 1)),
        STOCKS_RECOVERY_BACKOFF_MAX_SECONDS,
    )
    logger.warning(
        "Stocks cycle failed (%s consecutive, attempt #%s total): %s -- retrying in %.0fs",
        state["consecutive_failures"], state["recovery_attempts_total"], reason, delay,
    )
    return delay
