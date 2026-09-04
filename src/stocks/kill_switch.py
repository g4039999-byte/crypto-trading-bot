"""The single choke point every stocks live-trading code path must go
through before it is allowed to place a real order. Mirrors
src/kill_switch.py (crypto side) exactly, kept as a fully separate
module/file/env-var-namespace on purpose -- same convention as every
other stocks/crypto split in this project.

Nothing else in src/stocks is trusted to decide, on its own, whether a
real trade may happen -- src.stocks.live_trader calls trading_allowed()
fresh, immediately before acting, and refuses if it returns False. This
is re-checked on every decision (not just once at startup) so that
creating the kill-switch file stops live trading immediately, without
restarting anything.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from src.stocks.config import (
    STOCKS_CONFIRM_LIVE_TRADING,
    STOCKS_KILL_SWITCH_FILE,
    STOCKS_LIVE_TRADING,
    STOCKS_REQUIRED_CONFIRM_PHRASE,
)

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    allowed: bool
    reasons: list


def _kill_switch_path():
    return Path(STOCKS_KILL_SWITCH_FILE)


def trading_allowed():
    """Return a GateResult saying whether a real stocks order may be
    placed right now. Always re-reads config's live values and the
    filesystem, so this reflects the current state, not whatever it was
    at import time. This is Layers 2+3 of the three-layer gate (see
    src/stocks/config.py's LIVE TRADING GATE block); Layer 1
    (STOCKS_EXECUTION_ENABLED_IN_CODE) is checked separately, inside
    src/stocks/live_broker.py itself, and is not reflected here.
    """
    reasons = []

    if not STOCKS_LIVE_TRADING:
        reasons.append("STOCKS_LIVE_TRADING is not set to true")

    if STOCKS_CONFIRM_LIVE_TRADING != STOCKS_REQUIRED_CONFIRM_PHRASE:
        reasons.append("STOCKS_CONFIRM_LIVE_TRADING does not match the required confirmation phrase")

    if _kill_switch_path().exists():
        reasons.append(f"kill-switch file present at {STOCKS_KILL_SWITCH_FILE}")

    allowed = not reasons
    if not allowed:
        logger.info("Stocks live trading is blocked: %s", "; ".join(reasons))

    return GateResult(allowed=allowed, reasons=reasons)


def engage_kill_switch(reason="manually engaged"):
    """Create the kill-switch file, stopping all future live stock
    trades until it is removed by hand. Safe to call repeatedly. This is
    the emergency stop: it works even if src.stocks.live_broker's
    in-code gate were ever flipped on, and takes effect on the very next
    decision with no restart required.
    """
    path = _kill_switch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stopped: {reason}\n", encoding="utf-8")
    logger.warning("Stocks live-trading kill switch engaged (%s) -- wrote %s", reason, path)


def release_kill_switch():
    """Remove the kill-switch file. Live trading can resume (subject to
    the other two gate layers) after this.
    """
    path = _kill_switch_path()
    if path.exists():
        path.unlink()
        logger.warning("Stocks live-trading kill switch released -- removed %s", path)
