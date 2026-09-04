"""Local ledger of REAL open/closed stocks positions -- completely
separate file (data/stocks/live_positions.json) from paper trading's
data/stocks/paper_positions.json, on purpose, so a bug in one can never
corrupt or mix into the other. Alpaca's own live account is the actual
source of truth for what is really held; this ledger is this project's
own record of what it believes it did, used for duplicate-order
protection, sizing/circuit-breaker math (src.stocks.live_risk), and the
dashboard, and is only ever written to by src.stocks.live_trader AFTER
a real order has actually been confirmed filled by Alpaca -- never on
intent alone. Mirrors src.stocks.paper_broker's atomic-write/corrupt-
file-preserving persistence pattern exactly.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "live_positions.json"


def _empty_state():
    return {
        "open_positions": [], "closed_trades": [], "daily_pnl_usd": {},
        "trades_today": {}, "peak_equity_usd": 0.0,
    }


def _today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    """Restart-safe by construction, same guarantee as
    paper_broker.load_state(): whatever this returns is exactly what
    src.stocks.live_trader sees as "already happened" on a fresh process
    start.
    """
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s -- treating as empty: %s", STATE_FILE, exc)
        try:
            corrupt_copy = STATE_FILE.with_suffix(".corrupt." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
            STATE_FILE.replace(corrupt_copy)
            logger.error("Preserved the corrupt file at %s for inspection", corrupt_copy)
        except OSError:
            pass
        return _empty_state()
    for key, default in _empty_state().items():
        data.setdefault(key, default)
    return data


def save_state(state):
    """Atomic write (temp file + os.replace), same guarantee as
    paper_broker.save_state(): a crash mid-write can never leave
    live_positions.json torn/half-written.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STATE_FILE.parent), prefix=".live_positions_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def has_open_position(symbol, state=None):
    state = state or load_state()
    return any(p["symbol"] == symbol for p in state.get("open_positions", []))


def realized_pnl_usd(state=None):
    state = state or load_state()
    return sum(t.get("pnl_usd", 0) for t in state.get("closed_trades", []))


def record_open_position(symbol, entry_price, shares, size_usd, atr_at_entry, *,
                          order_id, client_order_id, strategy=None, entry_score=None,
                          entry_reason=None, starting_capital_usd=0.0):
    """Called ONLY after src.stocks.live_broker has confirmed a real BUY
    order actually filled -- never on submission alone. order_id/
    client_order_id are Alpaca's own identifiers for the fill that
    caused this, kept for audit and for the duplicate-order check.
    """
    from src.stocks.risk_engine import stop_loss_price, take_profit_price

    state = load_state()
    position = {
        "symbol": symbol,
        "entry_price": entry_price,
        "shares": shares,
        "size_usd": size_usd,
        "atr_at_entry": atr_at_entry,
        "stop_loss_price": stop_loss_price(entry_price, atr_at_entry),
        "take_profit_price": take_profit_price(entry_price, atr_at_entry),
        "trailing_stop_price": None,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "entry_score": entry_score,
        "entry_reason": entry_reason,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "mfe_price": entry_price,
        "mae_price": entry_price,
    }
    state["open_positions"].append(position)
    today = _today_key()
    state["trades_today"][today] = state.get("trades_today", {}).get(today, 0) + 1
    if not state.get("peak_equity_usd"):
        state["peak_equity_usd"] = starting_capital_usd
    save_state(state)
    logger.warning("[STOCKS LIVE] Recorded a REAL open position: %s size=$%.2f entry=$%.2f order_id=%s", symbol, size_usd, entry_price, order_id)
    return position


def update_mfe_mae(symbol, current_price):
    state = load_state()
    for position in state["open_positions"]:
        if position["symbol"] != symbol:
            continue
        position["mfe_price"] = max(position["mfe_price"], current_price)
        position["mae_price"] = min(position["mae_price"], current_price)
        position["last_price"] = current_price
        position["last_price_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def set_trailing_stop(symbol, trailing_stop_price):
    state = load_state()
    for position in state["open_positions"]:
        if position["symbol"] == symbol:
            position["trailing_stop_price"] = trailing_stop_price
    save_state(state)


def record_close_position(symbol, exit_price, reason, *, order_id, client_order_id, starting_capital_usd=0.0):
    """Called ONLY after src.stocks.live_broker has confirmed a real
    SELL order actually filled.
    """
    state = load_state()
    remaining, closed = [], None
    for position in state["open_positions"]:
        if position["symbol"] == symbol and closed is None:
            closed = position
        else:
            remaining.append(position)

    if closed is None:
        logger.warning("record_close_position called for %s but no open LIVE position was found in the ledger", symbol)
        return None

    pnl_usd = (exit_price - closed["entry_price"]) * closed["shares"]
    entry_price = closed["entry_price"]
    mfe_pct = (closed["mfe_price"] - entry_price) / entry_price * 100 if entry_price else None
    mae_pct = (closed["mae_price"] - entry_price) / entry_price * 100 if entry_price else None
    was_correct = reason in ("take_profit", "trailing_stop") or pnl_usd > 0

    state["open_positions"] = remaining
    today = _today_key()
    state["daily_pnl_usd"][today] = state["daily_pnl_usd"].get(today, 0.0) + pnl_usd

    closed_trade = {
        **closed,
        "exit_price": exit_price,
        "pnl_usd": pnl_usd,
        "pnl_pct": (exit_price - entry_price) / entry_price * 100 if entry_price else None,
        "reason": reason,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "was_correct": was_correct,
        "close_order_id": order_id,
        "close_client_order_id": client_order_id,
    }
    state["closed_trades"].append(closed_trade)

    current_equity = starting_capital_usd + realized_pnl_usd(state)
    state["peak_equity_usd"] = max(state.get("peak_equity_usd", starting_capital_usd) or starting_capital_usd, current_equity)

    save_state(state)
    logger.warning("[STOCKS LIVE] Recorded a REAL closed position: %s pnl=$%.2f reason=%s order_id=%s", symbol, pnl_usd, reason, order_id)
    return {"pnl_usd": pnl_usd, "position": closed_trade}
