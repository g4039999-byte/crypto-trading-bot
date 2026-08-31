"""The single choke point every live-trading code path must go through
before it is allowed to place a real order.

Nothing else in this codebase is trusted to decide, on its own, whether a
real trade may happen -- every entry point calls trading_allowed() fresh,
immediately before acting, and refuses if it returns False. This is
deliberately re-checked on every decision (not just once at startup) so
that creating the kill-switch file stops trading immediately, without
restarting anything.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from src.config import CONFIRM_LIVE_TRADING, KILL_SWITCH_FILE, LIVE_TRADING, REQUIRED_CONFIRM_PHRASE

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    allowed: bool
    reasons: list


def _kill_switch_path():
    return Path(KILL_SWITCH_FILE)


def trading_allowed():
    """Return a GateResult saying whether a real order may be placed
    right now. Always re-reads config's live values and the filesystem,
    so this reflects the current state, not whatever it was at import
    time.
    """
    reasons = []

    if not LIVE_TRADING:
        reasons.append("LIVE_TRADING is not set to true")

    if CONFIRM_LIVE_TRADING != REQUIRED_CONFIRM_PHRASE:
        reasons.append("CONFIRM_LIVE_TRADING does not match the required confirmation phrase")

    if _kill_switch_path().exists():
        reasons.append(f"kill-switch file present at {KILL_SWITCH_FILE}")

    allowed = not reasons
    if not allowed:
        logger.info("Live trading is blocked: %s", "; ".join(reasons))

    return GateResult(allowed=allowed, reasons=reasons)


def engage_kill_switch(reason="manually engaged"):
    """Create the kill-switch file, stopping all future trades until it
    is removed by hand. Safe to call repeatedly.
    """
    path = _kill_switch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stopped: {reason}\n", encoding="utf-8")
    logger.warning("Kill switch engaged (%s) -- wrote %s", reason, path)


def release_kill_switch():
    """Remove the kill-switch file. Trading can resume (subject to the
    other gates in trading_allowed()) after this.
    """
    path = _kill_switch_path()
    if path.exists():
        path.unlink()
        logger.warning("Kill switch released -- removed %s", path)
