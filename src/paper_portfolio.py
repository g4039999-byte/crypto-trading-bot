"""Position tracking for PAPER trading only.

This is a deliberate near-duplicate of src/portfolio.py rather than a
shared module: paper and (eventual) live position state must never be
able to mix, even by an import mistake, so this file owns its own state
file (data/paper_positions.json) and never touches
src/portfolio.py or data/positions.json. It mirrors the same sizing/
stop-loss/take-profit rules so a paper run is a faithful rehearsal of
what live trading would do.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    MAX_CAPITAL_DEPLOYMENT_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_HOLDING_MINUTES,
    MAX_TRADE_USD,
    PAPER_MAX_OPEN_POSITIONS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TOTAL_CAPITAL_USD,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "paper_positions.json"


def _empty_state():
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


def reset_paper_state():
    """Wipe paper-trading state back to empty (fresh $TOTAL_CAPITAL_USD,
    no open/closed positions). Never touches real trading data -- only
    data/paper_positions.json.
    """
    save_state(_empty_state())
    logger.info("Paper trading state reset")


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
    state = state or load_state()

    if len(state.get("open_positions", [])) >= PAPER_MAX_OPEN_POSITIONS:
        return False, f"already at the max of {PAPER_MAX_OPEN_POSITIONS} open paper position(s)"

    if daily_loss_cap_hit(state):
        return False, (
            f"daily (paper) loss cap reached (${daily_loss_usd(state):.2f} of "
            f"${TOTAL_CAPITAL_USD * MAX_DAILY_LOSS_PCT / 100:.2f} allowed today)"
        )

    return True, None


def compute_position_size_usd(state=None):
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
    logger.info("[PAPER] Opened position: %s size=$%.2f entry=$%s", symbol, size_usd, entry_price_usd)
    return position


def check_exit(position, current_price_usd):
    if current_price_usd <= position["stop_loss_price_usd"]:
        return True, "stop_loss"
    if current_price_usd >= position["take_profit_price_usd"]:
        return True, "take_profit"

    # A position that has neither hit stop-loss nor take-profit for too
    # long is a decision being avoided, not a good trade in progress --
    # force it so capital and attention move on to the next opportunity
    # instead of holding a flat/stale token indefinitely.
    opened_at = position.get("opened_at")
    if opened_at:
        try:
            opened_dt = datetime.fromisoformat(opened_at)
            held_minutes = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 60
            if held_minutes >= MAX_HOLDING_MINUTES:
                return True, "max_holding_time"
        except (ValueError, TypeError):
            pass

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
        logger.warning("close_position called for %s but no open paper position was found", token_address)
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
    logger.info("[PAPER] Closed position: %s pnl=$%.2f reason=%s", closed["symbol"], pnl_usd, reason)
    return {"pnl_usd": pnl_usd, "position": closed}
