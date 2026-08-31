"""Tiny local portfolio state: at most one open position, hard position
sizing caps, and a daily realized-loss cap.

State lives in data/positions.json (plain JSON, human-readable, meant to
be inspected). This module never talks to the network or a wallet -- it
only does position-sizing arithmetic and bookkeeping. Nothing here places
a trade; src/live_trader.py decides what to do with these numbers, and
src/kill_switch.py decides whether it's allowed to act on that decision.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    MAX_CAPITAL_DEPLOYMENT_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_OPEN_POSITIONS,
    MAX_TRADE_USD,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TOTAL_CAPITAL_USD,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "positions.json"

def _empty_state():
    # A fresh dict with fresh (not shared) mutable containers every call --
    # returning a shared module-level list/dict here would let one call
    # site's mutations leak into every other "empty" state.
    return {"open_positions": [], "daily_pnl_usd": {}, "closed_trades": []}


def _today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    if not STATE_FILE.exists():
        return _empty_state()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s -- treating as empty: %s", STATE_FILE, exc)
        return _empty_state()

    for key, default in _empty_state().items():
        data.setdefault(key, default)
    return data


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def daily_loss_usd(state=None):
    state = state or load_state()
    return -min(0.0, state.get("daily_pnl_usd", {}).get(_today_key(), 0.0))


def daily_loss_cap_hit(state=None):
    state = state or load_state()
    cap = TOTAL_CAPITAL_USD * (MAX_DAILY_LOSS_PCT / 100)
    return daily_loss_usd(state) >= cap


def deployed_capital_usd(state=None):
    state = state or load_state()
    return sum(p["size_usd"] for p in state.get("open_positions", []))


def can_open_new_position(state=None):
    """Returns (allowed: bool, reason: str | None)."""
    state = state or load_state()

    if len(state.get("open_positions", [])) >= MAX_OPEN_POSITIONS:
        return False, f"already at the max of {MAX_OPEN_POSITIONS} open position(s)"

    if daily_loss_cap_hit(state):
        return False, (
            f"daily loss cap reached (${daily_loss_usd(state):.2f} of "
            f"${TOTAL_CAPITAL_USD * MAX_DAILY_LOSS_PCT / 100:.2f} allowed today)"
        )

    return True, None


def compute_position_size_usd(state=None):
    """Hard-capped position size for a new trade. Never exceeds
    MAX_TRADE_USD, and never pushes total deployed capital past
    MAX_CAPITAL_DEPLOYMENT_PCT of TOTAL_CAPITAL_USD (always leaves a
    reserve, e.g. for network fees).
    """
    state = state or load_state()
    deployment_cap = TOTAL_CAPITAL_USD * (MAX_CAPITAL_DEPLOYMENT_PCT / 100)
    remaining_room = max(0.0, deployment_cap - deployed_capital_usd(state))
    return round(min(MAX_TRADE_USD, remaining_room), 2)


def open_position(token_address, symbol, entry_price_usd, size_usd):
    if entry_price_usd <= 0:
        raise ValueError("entry_price_usd must be positive")

    state = load_state()
    amount_tokens = size_usd / entry_price_usd

    position = {
        "token_address": token_address,
        "symbol": symbol,
        "entry_price_usd": entry_price_usd,
        "amount_tokens": amount_tokens,
        "size_usd": size_usd,
        "stop_loss_price_usd": entry_price_usd * (1 - STOP_LOSS_PCT / 100),
        "take_profit_price_usd": entry_price_usd * (1 + TAKE_PROFIT_PCT / 100),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }

    state["open_positions"].append(position)
    save_state(state)
    logger.info("Opened position: %s size=$%.2f entry=$%s", symbol, size_usd, entry_price_usd)
    return position


def check_exit(position, current_price_usd):
    """Returns (should_exit: bool, reason: str | None) for an open
    position given the current price. Pure decision logic -- does not
    touch state or place any order.
    """
    if current_price_usd <= position["stop_loss_price_usd"]:
        return True, "stop_loss"
    if current_price_usd >= position["take_profit_price_usd"]:
        return True, "take_profit"
    return False, None


def close_position(token_address, exit_price_usd, reason):
    state = load_state()
    remaining = []
    closed = None

    for position in state["open_positions"]:
        if position["token_address"] == token_address and closed is None:
            closed = position
        else:
            remaining.append(position)

    if closed is None:
        logger.warning("close_position called for %s but no open position was found", token_address)
        return None

    pnl_usd = (exit_price_usd - closed["entry_price_usd"]) * closed["amount_tokens"]

    state["open_positions"] = remaining
    today = _today_key()
    state["daily_pnl_usd"][today] = state["daily_pnl_usd"].get(today, 0.0) + pnl_usd
    state["closed_trades"].append(
        {
            **closed,
            "exit_price_usd": exit_price_usd,
            "pnl_usd": pnl_usd,
            "reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_state(state)
    logger.info("Closed position: %s pnl=$%.2f reason=%s", closed["symbol"], pnl_usd, reason)
    return {"pnl_usd": pnl_usd, "position": closed}
