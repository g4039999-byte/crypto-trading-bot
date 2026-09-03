"""Local, always-available simulated position ledger for US stocks
paper trading -- the source of truth regardless of whether Alpaca is
configured (src.stocks.alpaca_client.submit_paper_order() is called
best-effort alongside this, purely to mirror the trade onto a real
Alpaca paper account for cross-checking; its failure never blocks or
alters what's recorded here). Mirrors src/paper_portfolio.py's design
on the crypto side.

State: data/stocks/paper_positions.json. Decisions: data/stocks/
paper_trade_log.jsonl (src.stocks.paper_logger). Both entirely separate
from every crypto-side file.

Every position records everything requested for post-trade analysis:
entry reason/score/strategy/indicators snapshot, stop/target, MFE/MAE
(tracked live via update_mfe_mae() every monitoring cycle), and at
close: exit price/reason/time/P&L and whether the entry signal actually
played out (was_correct -- a win via take_profit/trailing_stop counts
as the signal having been right; a stop_loss/max_holding_time exit
counts as wrong).
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.stocks import alpaca_client
from src.stocks.config import STOCKS_STARTING_CAPITAL_USD
from src.stocks.paper_logger import log_decision
from src.stocks.risk_engine import (
    can_open_new_position,
    check_exit,
    compute_position_size_usd,
    stop_loss_price,
    take_profit_price,
    update_trailing_stop,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "paper_positions.json"


def _empty_state():
    return {
        "open_positions": [], "closed_trades": [], "daily_pnl_usd": {},
        "trades_today": {}, "peak_equity_usd": STOCKS_STARTING_CAPITAL_USD,
    }


def _today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    """Restart-safe by construction: this is the ONLY place open
    positions/closed trades/trade-count-today live, so whatever this
    returns is exactly what src.stocks.engine sees as "already
    happened" on a fresh process start -- there is no separate
    in-memory state to reconcile, so a duplicate buy of an
    already-open symbol is impossible as long as this file reflects
    reality, which save_state()'s atomic write below exists to
    guarantee even across a crash mid-write.
    """
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # A genuinely corrupt file (not just "missing") is data loss --
        # preserve it for forensics instead of silently discarding it,
        # and log loudly, but still return a safe empty state so the
        # loop can keep running rather than crash-looping forever.
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
    """Atomic write (temp file in the same directory + os.replace) so a
    crash or power loss mid-write can never leave paper_positions.json
    torn/half-written -- the file is always either the previous valid
    state or the new one, never something in between. This is what
    makes load_state()'s restart-safety guarantee actually hold.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STATE_FILE.parent), prefix=".paper_positions_", suffix=".tmp")
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


def reset_paper_state():
    save_state(_empty_state())
    logger.info("Stocks paper trading state reset")


def realized_pnl_usd(state=None):
    state = state or load_state()
    return sum(t.get("pnl_usd", 0) for t in state.get("closed_trades", []))


def open_position(symbol, entry_price, size_usd, atr_at_entry, *,
                   strategy=None, entry_score=None, entry_reason=None,
                   features_snapshot=None, regime_snapshot=None, x_entity=None):
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")

    state = load_state()
    shares = round(size_usd / entry_price, 4)

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
        "features_snapshot": features_snapshot,
        "regime_snapshot": regime_snapshot,
        "x_entity": x_entity,
        "mfe_price": entry_price,  # max favorable excursion (best price seen)
        "mae_price": entry_price,  # max adverse excursion (worst price seen)
    }

    state["open_positions"].append(position)
    today = _today_key()
    state["trades_today"][today] = state.get("trades_today", {}).get(today, 0) + 1
    save_state(state)

    try:
        alpaca_client.submit_paper_order(symbol, shares, "buy")
    except Exception:
        logger.exception("Best-effort Alpaca paper-order mirror failed for %s BUY -- local ledger is unaffected", symbol)

    logger.info("[STOCKS PAPER] Opened position: %s size=$%.2f entry=$%.2f", symbol, size_usd, entry_price)
    return position


def _set_trailing_stop(symbol, trailing_stop_price):
    state = load_state()
    for position in state["open_positions"]:
        if position["symbol"] == symbol:
            position["trailing_stop_price"] = trailing_stop_price
    save_state(state)


def update_mfe_mae(symbol, current_price):
    """Call every monitoring cycle for each open position -- tracks the
    best/worst price seen so far, used to compute MFE/MAE at close.
    """
    state = load_state()
    for position in state["open_positions"]:
        if position["symbol"] != symbol:
            continue
        position["mfe_price"] = max(position["mfe_price"], current_price)
        position["mae_price"] = min(position["mae_price"], current_price)
    save_state(state)


def close_position(symbol, exit_price, reason):
    state = load_state()
    remaining, closed = [], None
    for position in state["open_positions"]:
        if position["symbol"] == symbol and closed is None:
            closed = position
        else:
            remaining.append(position)

    if closed is None:
        logger.warning("close_position called for %s but no open stocks paper position was found", symbol)
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
    }
    state["closed_trades"].append(closed_trade)

    current_equity = STOCKS_STARTING_CAPITAL_USD + realized_pnl_usd(state)
    state["peak_equity_usd"] = max(state.get("peak_equity_usd", STOCKS_STARTING_CAPITAL_USD), current_equity)

    save_state(state)

    try:
        alpaca_client.submit_paper_order(symbol, closed["shares"], "sell")
    except Exception:
        logger.exception("Best-effort Alpaca paper-order mirror failed for %s SELL -- local ledger is unaffected", symbol)

    logger.info("[STOCKS PAPER] Closed position: %s pnl=$%.2f reason=%s", symbol, pnl_usd, reason)
    return {"pnl_usd": pnl_usd, "position": closed_trade}


def evaluate_exit_for_open_positions(current_prices):
    """current_prices: {symbol: price}. Checks every open position for
    an exit (stop/trailing/take-profit/max-holding), closes any that
    trigger, and updates MFE/MAE for the rest. Returns the list of
    close results. A symbol with no price this cycle is skipped for
    both the exit check and MFE/MAE update, same "missing data never
    crashes monitoring" convention as the crypto side.
    """
    state = load_state()
    closed_results = []
    for position in list(state.get("open_positions", [])):
        price = current_prices.get(position["symbol"])
        if price is None:
            continue

        new_trailing = update_trailing_stop(position, price)
        if new_trailing != position.get("trailing_stop_price"):
            _set_trailing_stop(position["symbol"], new_trailing)
            position = {**position, "trailing_stop_price": new_trailing}

        held_days = (datetime.now(timezone.utc) - datetime.fromisoformat(position["opened_at"])).total_seconds() / 86400
        should_exit, reason = check_exit(position, price, held_days)
        if should_exit:
            result = close_position(position["symbol"], price, reason)
            log_decision("SELL", position["symbol"], reason, extra={"exit_price": price, "pnl_usd": result["pnl_usd"] if result else None})
            closed_results.append(result)
        else:
            update_mfe_mae(position["symbol"], price)
    return closed_results
