"""Performance metrics shared by src.stocks.backtester results,
src.stocks.benchmarks, and (eventually) live paper-trading history --
one implementation, not three copies. Operates on a plain list of
pnl_pct floats (one per resolved trade) so it works identically for a
backtest trade list, a benchmark's trade list, or real closed_trades
from data/stocks/paper_positions.json.

total_return_pct/max_drawdown_pct are computed as a running SUM of
per-trade % returns, not compounded. This is deliberate: trades here
come from many different symbols whose entries can and do overlap in
calendar time (this backtester evaluates each symbol's own timeline
independently -- see src.stocks.backtester's module docstring), so
compounding them sequentially as if they were one account's trades, one
after another, wildly overstates the result (an early version of this
function did exactly that and produced a literal ~10^16% "return" for
a 47-symbol buy-and-hold backtest). Since every trade in this project
is sized as a fixed dollar amount (STOCKS_MAX_POSITION_USD), not a %
of a growing/shrinking account, a running sum of independent,
similarly-sized bets is the more honest approximation of aggregate
result here -- still not a literal portfolio equity curve (that would
need day-by-day position-level simulation), but comparable
apples-to-apples across strategies/baselines, which is what actually
matters for the "does this beat the baseline" question this module
exists to answer.
"""

import math
import statistics


def compute_metrics(pnl_pcts):
    """pnl_pcts: list of per-trade % returns (e.g. +5.2, -2.1, ...).
    Returns a dict of the standard set requested for benchmarking --
    every value is None (not 0, not a crash) when there isn't enough
    data to compute it meaningfully (e.g. Sharpe needs >=2 trades for a
    stdev at all).
    """
    n = len(pnl_pcts)
    if n == 0:
        return _empty_metrics()

    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p <= 0]
    win_rate = len(wins) / n * 100

    total_return_pct = sum(pnl_pcts)
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None

    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)

    expectancy = statistics.mean(pnl_pcts)

    sharpe = _sharpe(pnl_pcts)
    sortino = _sortino(pnl_pcts)
    max_drawdown_pct = _max_drawdown_pct(pnl_pcts)

    return {
        "trade_count": n,
        "win_rate_pct": round(win_rate, 1),
        "total_return_pct": round(total_return_pct, 2),
        "avg_win_pct": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss_pct": round(avg_loss, 2) if avg_loss is not None else None,
        "profit_factor": round(profit_factor, 2) if isinstance(profit_factor, float) and math.isfinite(profit_factor) else profit_factor,
        "expectancy_pct": round(expectancy, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown_pct": round(max_drawdown_pct, 2) if max_drawdown_pct is not None else None,
    }


def _sharpe(pnl_pcts, risk_free_pct=0.0):
    if len(pnl_pcts) < 2:
        return None
    excess = [p - risk_free_pct for p in pnl_pcts]
    mean = statistics.mean(excess)
    stdev = statistics.stdev(excess)
    if stdev == 0:
        return None
    return mean / stdev


def _sortino(pnl_pcts, risk_free_pct=0.0):
    if len(pnl_pcts) < 2:
        return None
    excess = [p - risk_free_pct for p in pnl_pcts]
    mean = statistics.mean(excess)
    downside = [min(0.0, e) for e in excess]
    downside_stdev = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
    if downside_stdev == 0:
        return None
    return mean / downside_stdev


def _max_drawdown_pct(pnl_pcts):
    """Max peak-to-trough drop in the running SUM of per-trade %
    returns (not a compounded equity curve -- see this module's
    docstring). Reported in the same %-points unit as total_return_pct,
    e.g. "15" means the cumulative sum fell 15 percentage-points from
    its running peak at some point.
    """
    cumulative, peak, max_dd = 0.0, 0.0, 0.0
    for pct in pnl_pcts:
        cumulative += pct
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def _empty_metrics():
    keys = (
        "trade_count", "win_rate_pct", "total_return_pct", "avg_win_pct", "avg_loss_pct",
        "profit_factor", "expectancy_pct", "sharpe", "sortino", "max_drawdown_pct",
    )
    return {k: (0 if k == "trade_count" else None) for k in keys}
