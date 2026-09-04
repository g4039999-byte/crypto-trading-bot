"""Position sizing and entry-gating math for LIVE stocks trading. Pure
functions over explicit state -- no I/O, no network -- exactly the same
shape/discipline as src.stocks.risk_engine (paper trading), but reading
the separate, much smaller STOCKS_LIVE_* limits from src/stocks/config.py
instead of the paper-trading defaults. Never imported by
src.stocks.risk_engine, src.stocks.paper_broker, or src.stocks.engine --
paper trading is completely unaffected by anything in this file.

Exit-price math (stop-loss/take-profit/trailing-stop ATR levels,
check_exit's precedence) is NOT duplicated here -- src.stocks.risk_engine's
stop_loss_price()/take_profit_price()/update_trailing_stop()/check_exit()
are pure ATR functions with no capital assumption baked in, so
src.stocks.live_trader reuses those exact, already-deeply-tested
functions unchanged for live positions too. This file only covers the
part that genuinely differs between paper and live: how much capital a
new position is allowed to risk.
"""

from src.stocks.config import (
    STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT,
    STOCKS_LIVE_MAX_DAILY_LOSS_PCT,
    STOCKS_LIVE_MAX_DRAWDOWN_PCT,
    STOCKS_LIVE_MAX_OPEN_POSITIONS,
    STOCKS_LIVE_MAX_POSITION_USD,
    STOCKS_LIVE_MAX_TRADES_PER_DAY,
    STOCKS_LIVE_STARTING_CAPITAL_USD,
)


def live_deployed_capital_usd(open_positions):
    return sum(p.get("size_usd", 0) for p in open_positions)


def live_equity_usd(realized_pnl_usd, open_positions_unrealized_pnl_usd=0.0):
    return STOCKS_LIVE_STARTING_CAPITAL_USD + realized_pnl_usd + open_positions_unrealized_pnl_usd


def can_open_new_live_position(state):
    """state: {"open_positions": [...], "closed_trades": [...],
    "daily_pnl_usd": {date: float}, "peak_equity_usd": float,
    "trades_today": {date: int}} -- same shape as paper's state, kept in
    its own file (src.stocks.live_ledger) so a bug in one can never
    corrupt the other. Returns (allowed: bool, reason: str | None).
    """
    open_positions = state.get("open_positions", [])
    if len(open_positions) >= STOCKS_LIVE_MAX_OPEN_POSITIONS:
        return False, f"already at the live max of {STOCKS_LIVE_MAX_OPEN_POSITIONS} open position(s)"

    today = _today_key()
    trades_today = state.get("trades_today", {}).get(today, 0)
    if trades_today >= STOCKS_LIVE_MAX_TRADES_PER_DAY:
        return False, f"already at today's live max of {STOCKS_LIVE_MAX_TRADES_PER_DAY} trade(s) -- overtrading guard"

    realized_today = state.get("daily_pnl_usd", {}).get(today, 0.0)
    daily_loss_cap = STOCKS_LIVE_STARTING_CAPITAL_USD * (STOCKS_LIVE_MAX_DAILY_LOSS_PCT / 100)
    if -realized_today >= daily_loss_cap:
        return False, f"live daily loss cap reached (${-realized_today:.2f} of ${daily_loss_cap:.2f} allowed today)"

    peak_equity = state.get("peak_equity_usd", STOCKS_LIVE_STARTING_CAPITAL_USD)
    current_equity = live_equity_usd(sum(t.get("pnl_usd", 0) for t in state.get("closed_trades", [])))
    if peak_equity > 0:
        drawdown_pct = max(0.0, (peak_equity - current_equity) / peak_equity * 100)
        if drawdown_pct >= STOCKS_LIVE_MAX_DRAWDOWN_PCT:
            return False, f"live circuit breaker: drawdown {drawdown_pct:.1f}% >= max {STOCKS_LIVE_MAX_DRAWDOWN_PCT:.1f}% -- no new live entries until it recovers"

    return True, None


def compute_live_position_size_usd(state, regime_multiplier=1.0, buying_power_usd=None):
    """Hard-capped at STOCKS_LIVE_MAX_POSITION_USD, never pushes total
    deployed live capital past STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT of
    the live starting capital, and -- when buying_power_usd is supplied
    (a fresh, real read from Alpaca's live account, see
    src.stocks.live_broker.get_live_account()) -- never sizes a position
    larger than what the account can actually afford right now, so a
    stale local capital assumption can never produce an order the
    account would reject or over-leverage.
    """
    deployment_cap = STOCKS_LIVE_STARTING_CAPITAL_USD * (STOCKS_LIVE_MAX_CAPITAL_DEPLOYMENT_PCT / 100)
    remaining_room = max(0.0, deployment_cap - live_deployed_capital_usd(state.get("open_positions", [])))
    size = min(STOCKS_LIVE_MAX_POSITION_USD, remaining_room) * regime_multiplier
    if buying_power_usd is not None:
        size = min(size, max(0.0, buying_power_usd))
    return round(max(0.0, size), 2)


def _today_key():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
