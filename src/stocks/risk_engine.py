"""Position sizing and exit-price math for the stocks paper-trading
engine. Pure functions over explicit state -- no I/O -- src.stocks.
paper_broker owns persistence and calls into these; this stays
trivially unit-testable.

Every exit level is ATR-based (volatility-scaled), not a fixed
percentage: a stock moving 1%/day and one moving 8%/day need very
different stop distances for the same "this trade is wrong" meaning.
"""

from src.stocks.config import (
    STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT,
    STOCKS_MAX_DAILY_LOSS_PCT,
    STOCKS_MAX_DRAWDOWN_PCT,
    STOCKS_MAX_HOLDING_DAYS,
    STOCKS_MAX_OPEN_POSITIONS,
    STOCKS_MAX_POSITION_USD,
    STOCKS_MAX_TRADES_PER_DAY,
    STOCKS_STARTING_CAPITAL_USD,
    STOCKS_STOP_LOSS_ATR_MULT,
    STOCKS_TAKE_PROFIT_ATR_MULT,
    STOCKS_TRAILING_ARM_ATR_MULT,
    STOCKS_TRAILING_STOP_ATR_MULT,
)


def deployed_capital_usd(open_positions):
    return sum(p.get("size_usd", 0) for p in open_positions)


def equity_usd(realized_pnl_usd, open_positions_unrealized_pnl_usd=0.0):
    return STOCKS_STARTING_CAPITAL_USD + realized_pnl_usd + open_positions_unrealized_pnl_usd


def can_open_new_position(state, regime=None):
    """state: {"open_positions": [...], "daily_pnl_usd": {date: float},
    "closed_trades": [...], "peak_equity_usd": float, "trades_today": {date: int}}.
    Returns (allowed: bool, reason: str | None).
    """
    open_positions = state.get("open_positions", [])
    if len(open_positions) >= STOCKS_MAX_OPEN_POSITIONS:
        return False, f"already at the max of {STOCKS_MAX_OPEN_POSITIONS} open position(s)"

    today = _today_key()
    trades_today = state.get("trades_today", {}).get(today, 0)
    if trades_today >= STOCKS_MAX_TRADES_PER_DAY:
        return False, f"already at today's max of {STOCKS_MAX_TRADES_PER_DAY} trade(s) -- overtrading guard"

    realized_today = state.get("daily_pnl_usd", {}).get(today, 0.0)
    daily_loss_cap = STOCKS_STARTING_CAPITAL_USD * (STOCKS_MAX_DAILY_LOSS_PCT / 100)
    if -realized_today >= daily_loss_cap:
        return False, f"daily loss cap reached (${-realized_today:.2f} of ${daily_loss_cap:.2f} allowed today)"

    peak_equity = state.get("peak_equity_usd", STOCKS_STARTING_CAPITAL_USD)
    current_equity = equity_usd(sum(t.get("pnl_usd", 0) for t in state.get("closed_trades", [])))
    if peak_equity > 0:
        drawdown_pct = max(0.0, (peak_equity - current_equity) / peak_equity * 100)
        if drawdown_pct >= STOCKS_MAX_DRAWDOWN_PCT:
            return False, f"circuit breaker: drawdown {drawdown_pct:.1f}% >= max {STOCKS_MAX_DRAWDOWN_PCT:.1f}% -- no new entries until it recovers"

    return True, None


def compute_position_size_usd(state, regime_multiplier=1.0):
    """Hard-capped at STOCKS_MAX_POSITION_USD, and never pushes total
    deployed capital past STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT of starting
    capital. regime_multiplier (src.stocks.regime.risk_multiplier)
    scales this down further in a risk-off regime.
    """
    deployment_cap = STOCKS_STARTING_CAPITAL_USD * (STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT / 100)
    remaining_room = max(0.0, deployment_cap - deployed_capital_usd(state.get("open_positions", [])))
    size = min(STOCKS_MAX_POSITION_USD, remaining_room) * regime_multiplier
    return round(max(0.0, size), 2)


def stop_loss_price(entry_price, atr_value):
    return round(entry_price - STOCKS_STOP_LOSS_ATR_MULT * atr_value, 4)


def take_profit_price(entry_price, atr_value):
    return round(entry_price + STOCKS_TAKE_PROFIT_ATR_MULT * atr_value, 4)


def update_trailing_stop(position, current_price):
    """Returns a possibly-updated trailing_stop_price for `position`
    (does not mutate it -- caller assigns). The trailing stop only
    "arms" (starts being tracked) once price has moved
    STOCKS_TRAILING_ARM_ATR_MULT ATRs in favor of the trade, and then
    only ever moves up (for a long), never down, same as a normal
    ratcheting trailing stop.
    """
    entry_price = position["entry_price"]
    atr_value = position["atr_at_entry"]
    arm_level = entry_price + STOCKS_TRAILING_ARM_ATR_MULT * atr_value

    if current_price < arm_level:
        return position.get("trailing_stop_price")  # not armed yet

    candidate = current_price - STOCKS_TRAILING_STOP_ATR_MULT * atr_value
    existing = position.get("trailing_stop_price")
    if existing is None:
        return round(candidate, 4)
    return round(max(existing, candidate), 4)


def check_exit(position, current_price, held_days):
    """Returns (should_exit: bool, reason: str | None). Checks, in
    order: hard stop-loss, trailing stop (if armed), take-profit,
    max-holding-time -- the first one that fires wins.
    """
    if current_price <= position["stop_loss_price"]:
        return True, "stop_loss"

    trailing = position.get("trailing_stop_price")
    if trailing is not None and current_price <= trailing:
        return True, "trailing_stop"

    if current_price >= position["take_profit_price"]:
        return True, "take_profit"

    if held_days >= STOCKS_MAX_HOLDING_DAYS:
        return True, "max_holding_time"

    return False, None


def _today_key():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
