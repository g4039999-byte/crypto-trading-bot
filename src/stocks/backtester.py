"""Historical backtest engine for the daily-bar strategies (momentum,
breakout, mean_reversion, pullback, relative_volume -- vwap_reclaim is
intraday-only, see its own module docstring for why it isn't covered
here). Uses src.stocks.data_provider (yfinance by default -- free, no
account needed), through src.stocks.bar_cache so repeated runs against
the same symbol/lookback don't refetch from the network.

Look-ahead discipline: a signal on day i is computed from bars[:i+1]
only (never a later bar), and if it fires, the fill price is day i+1's
OPEN, not day i's close -- the earliest price actually achievable by a
decision made after day i's bar closed. Exits are checked against each
subsequent day's own high/low, in the same PRECEDENCE order src.stocks.
risk_engine.check_exit() uses live -- stop_loss, then trailing_stop,
then take_profit, then max_holding_time -- the conservative assumption
when daily bars can't say which actually came first intraday. The
trailing stop itself is simulated with risk_engine.update_trailing_stop()
(the exact function the live loop calls), updated off each day's HIGH
before that day's exits are checked -- omitting this would silently
backtest a DIFFERENT, simpler exit rule (fixed stop/target only) than
what paper/live trading actually runs, which is not a minor detail: it
changes the exit price on every trade that would have trailed.

Realistic costs: every fill (entry and exit alike) is moved against the
trader by STOCKS_SLIPPAGE_BPS and STOCKS_COMMISSION_PER_TRADE_USD is
subtracted from each trade's dollar P&L equivalent, expressed as a
pct-of-entry-price adjustment so it composes cleanly with pnl_pct --
see _apply_costs(). Skipping this would let a backtest look better than
any execution that could actually happen.

Walk-forward: BACKTEST_IN_SAMPLE_FRACTION splits each symbol's bar
INDEX range (not calendar date -- see fold_index below for why that
distinction matters) into an earlier in-sample portion and a later
out-of-sample portion, and results are reported for both separately --
since these strategies use fixed, hand-set thresholds rather than
parameters fit to this data, this isn't "tune then validate" so much as
"does this strategy's edge hold up in a later, unseen period or was the
in-sample result a fluke of that specific window". A strategy whose
out-of-sample numbers collapse relative to in-sample is a
straightforward overfitting/regime-specific warning sign even without
any parameter fitting having happened.

Beyond that single split, every trade also carries a `fold_index` (0..
RESEARCH_WALK_FORWARD_FOLDS-1, by its entry bar's position within the
symbol's own history) so a caller (src.stocks.research_pipeline) can
report metrics per fold WITHOUT re-running the simulation N times -- a
single causal, no-lookahead pass already produces every trade; which
calendar period a completed trade's entry falls into is just a label
applied to it afterward. This is what makes multi-period robustness
reporting fast rather than an O(N folds) re-simulation.

Concurrency: symbols are backtested in parallel (BACKTEST_MAX_WORKERS
threads) -- each symbol's simulation is independent of every other's,
and the historical-research pipeline's whole point is running this
across dozens of symbols x several strategies quickly.

Performance: the naive way to evaluate "what would this strategy have
seen on day i" is compute_features(df.iloc[:i+1]) -- but that redoes
every rolling/EWM computation over the ENTIRE window up to i, at EVERY
one of a symbol's n bars: O(n) work times n bars = O(n^2) total. For a
short backtest that's unnoticeable; for years of history across dozens
of symbols it dominates completely (this was measured directly: an
early version of a 10-year, 47-symbol research run was killed after
~15 minutes still computing the FIRST strategy's FIRST symbol). Instead,
src.stocks.features.compute_features_series(df) computes every column
as one vectorized pass over the whole symbol history ONCE (O(n) total),
and the loop below just reads each bar's already-computed row out of it
(O(1) per bar) -- see that function's own docstring and
tests/stocks/test_features.py's parity tests, which guarantee this
produces byte-for-byte identical features to calling compute_features()
fresh, so the backtest simulates against exactly the same numbers the
live loop would have seen.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from src.stocks import bar_cache
from src.stocks.config import (
    BACKTEST_IN_SAMPLE_FRACTION,
    BACKTEST_MAX_WORKERS,
    RESEARCH_WALK_FORWARD_FOLDS,
    STOCKS_COMMISSION_PER_TRADE_USD,
    STOCKS_MAX_HOLDING_DAYS,
    STOCKS_SLIPPAGE_BPS,
)
from src.stocks.data_provider import get_provider
from src.stocks.features import compute_features_series, features_row_to_dict
from src.stocks.risk_engine import stop_loss_price, take_profit_price, update_trailing_stop
from src.stocks.strategies import STRATEGIES

logger = logging.getLogger(__name__)

MIN_BARS_BEFORE_TRADING = 55  # enough history for SMA50/ATR14 to be meaningful


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    entry_date: str
    entry_price: float
    exit_date: str = None
    exit_price: float = None
    reason: str = None
    pnl_pct: float = None
    confidence: float = None
    in_sample: bool = True
    fold_index: int = 0
    regime_trend: str = None       # tagged post-hoc by research_pipeline.tag_trades_with_regime(); None until then
    regime_volatility: str = None


def _apply_costs(entry_price, exit_price, side_is_long=True):
    """Slippage (moves both fills against the trader) + a flat
    commission expressed as a % drag on this specific trade's notional
    (so it scales sensibly across the very different position sizes a
    real backtest walks through, while still reflecting a real, fixed
    per-trade dollar cost for a typical position size). Returns the
    cost-adjusted (entry_price, exit_price).
    """
    slippage_frac = STOCKS_SLIPPAGE_BPS / 10000.0
    # A long buys slightly higher and sells slightly lower than the
    # reference price -- slippage always works against the trader.
    adj_entry = entry_price * (1 + slippage_frac)
    adj_exit = exit_price * (1 - slippage_frac)
    return adj_entry, adj_exit


def _commission_drag_pct(entry_price, position_size_usd=1500.0):
    """Round-trip commission expressed as a % of entry price, using a
    representative position size (this project's own STOCKS_MAX_POSITION_USD
    default) since the backtester itself doesn't simulate account
    equity/compounding -- see src.stocks.performance's module docstring
    on why pnl_pct here is a per-trade %, not a %-of-a-growing-account.
    Zero when STOCKS_COMMISSION_PER_TRADE_USD is 0 (Alpaca's actual,
    current commission-free equities trading -- see config.py).
    """
    if STOCKS_COMMISSION_PER_TRADE_USD <= 0 or entry_price <= 0:
        return 0.0
    shares = position_size_usd / entry_price
    round_trip_commission_usd = STOCKS_COMMISSION_PER_TRADE_USD * 2
    return (round_trip_commission_usd / (shares * entry_price)) * 100


def _backtest_one_symbol(strategy_name, symbol, df, split_index, fold_boundaries):
    strategy_module = STRATEGIES[strategy_name]
    trades = []
    position = None

    features_series = compute_features_series(df)  # ONE vectorized pass -- see module docstring

    for i in range(MIN_BARS_BEFORE_TRADING, len(df)):
        if position is not None:
            day = df.iloc[i]
            held_days = i - position["entry_index"]

            # Trailing stop, simulated with the same src.stocks.risk_engine
            # function the live loop actually calls -- a backtest that
            # never modeled this would be testing a DIFFERENT exit rule
            # than what paper (and any future live) trading uses, which
            # is not a subtlety, it changes the exit price and therefore
            # every pnl_pct in this backtest. Updated off the day's HIGH
            # (the best price the position could have touched that day,
            # which is what a trail tracked continuously would have
            # ratcheted against) before checking whether it (or the hard
            # stop/target) also got hit that same day.
            position["trailing_stop_price"] = update_trailing_stop(position, float(day["high"]))

            exit_price, reason = None, None
            if day["low"] <= position["stop_loss_price"]:
                exit_price, reason = position["stop_loss_price"], "stop_loss"
            elif position["trailing_stop_price"] is not None and day["low"] <= position["trailing_stop_price"]:
                exit_price, reason = position["trailing_stop_price"], "trailing_stop"
            elif day["high"] >= position["take_profit_price"]:
                exit_price, reason = position["take_profit_price"], "take_profit"
            elif held_days >= STOCKS_MAX_HOLDING_DAYS:
                exit_price, reason = float(day["close"]), "max_holding_time"

            if exit_price is not None:
                entry_price, adj_exit_price = _apply_costs(position["entry_price"], exit_price)
                pnl_pct = (adj_exit_price - entry_price) / entry_price * 100
                pnl_pct -= _commission_drag_pct(position["entry_price"])
                fold_index = next((f for f, upper in enumerate(fold_boundaries) if position["entry_index"] < upper), len(fold_boundaries) - 1)
                trades.append(BacktestTrade(
                    symbol=symbol, strategy=strategy_name,
                    entry_date=str(df.index[position["entry_index"]]), entry_price=entry_price,
                    exit_date=str(df.index[i]), exit_price=float(adj_exit_price), reason=reason,
                    pnl_pct=float(pnl_pct), confidence=position["confidence"],
                    in_sample=position["entry_index"] < split_index,
                    fold_index=fold_index,
                ))
                position = None
            continue  # never also evaluate a fresh entry the same day a position was open

        if i + 1 >= len(df):
            break  # no next-day open left to fill a fresh entry at

        try:
            features = features_row_to_dict(features_series.iloc[i])
            # Only relative_volume.py touches `df` at all among the daily
            # strategies, and only via df.iloc[-1] for the latest bar --
            # a cheap single-row slice gives it that without the O(i)
            # cost of slicing the whole window up to i (see module
            # docstring). vwap_reclaim (the one strategy that genuinely
            # needs several recent bars) is intraday-only and never
            # reaches this function -- see backtest_strategy()'s TIMEFRAME
            # guard.
            latest_bar = df.iloc[i : i + 1]
            signal = strategy_module.generate_signal(features, latest_bar)
        except Exception:
            continue

        if signal["action"] != "BUY":
            continue

        atr_value = features.get("atr")
        if not atr_value or atr_value <= 0:
            continue

        raw_entry_price = float(df["open"].iloc[i + 1])
        position = {
            "entry_index": i + 1, "entry_price": raw_entry_price, "atr_at_entry": atr_value,
            "stop_loss_price": stop_loss_price(raw_entry_price, atr_value),
            "take_profit_price": take_profit_price(raw_entry_price, atr_value),
            "trailing_stop_price": None,
            "confidence": signal["confidence"],
        }

    return trades


def _fold_boundaries(n_bars, n_folds):
    """n_folds equal-count upper-bound indices, e.g. n_bars=100,
    n_folds=5 -> [20, 40, 60, 80, 100]. A trade whose entry_index is
    less than fold_boundaries[k] (and not less than fold_boundaries[k-1])
    belongs to fold k.
    """
    n_folds = max(1, n_folds)
    step = max(1, n_bars // n_folds)
    boundaries = [step * (k + 1) for k in range(n_folds - 1)]
    boundaries.append(n_bars)
    return boundaries


def _fetch_bars(symbols, lookback_days):
    provider = get_provider()
    return bar_cache.get_daily_bars_batch_cached(provider, list(symbols), lookback_days)


def _run_one_symbol_safely(strategy_name, symbol, df, split_index, fold_boundaries):
    try:
        return _backtest_one_symbol(strategy_name, symbol, df, split_index, fold_boundaries)
    except Exception:
        logger.exception("Backtest failed for %s on %s -- skipping this symbol", strategy_name, symbol)
        return []


def backtest_strategy(strategy_name, symbols, lookback_days=730, n_folds=None):
    """Returns the list of resolved BacktestTrade objects across every
    symbol (a position still open at the end of a symbol's data is
    dropped, not counted -- same "unresolved, exclude from stats"
    convention scripts/backtest_paper_strategy.py uses on the crypto
    side). Never raises: a symbol with unusable data is logged and
    skipped. Symbols are backtested concurrently -- see the module
    docstring.
    """
    if strategy_name not in STRATEGIES:
        raise KeyError(f"Unknown strategy {strategy_name!r}")
    if getattr(STRATEGIES[strategy_name], "TIMEFRAME", "daily") != "daily":
        raise ValueError(
            f"{strategy_name} is not backtestable on daily bars (TIMEFRAME="
            f"{getattr(STRATEGIES[strategy_name], 'TIMEFRAME', None)!r}) -- see its module docstring"
        )

    n_folds = RESEARCH_WALK_FORWARD_FOLDS if n_folds is None else n_folds
    bars_by_symbol = _fetch_bars(symbols, lookback_days)

    all_trades = []
    jobs = []
    for symbol, df in bars_by_symbol.items():
        if df is None or df.empty or len(df) < MIN_BARS_BEFORE_TRADING + 2:
            continue
        split_index = int(len(df) * BACKTEST_IN_SAMPLE_FRACTION)
        jobs.append((symbol, df, split_index))

    with ThreadPoolExecutor(max_workers=BACKTEST_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_run_one_symbol_safely, strategy_name, symbol, df, split_index, _fold_boundaries(len(df), n_folds)): symbol
            for symbol, df, split_index in jobs
        }
        for future in as_completed(futures):
            all_trades.extend(future.result())

    return all_trades


def backtest_all_strategies(symbols, lookback_days=730, n_folds=None):
    """{strategy_name: [BacktestTrade, ...]} for every registered
    daily-bar strategy -- one batched, cached data fetch shared across
    all of them (the expensive part), not one per strategy. Each
    strategy's per-symbol jobs run concurrently.
    """
    n_folds = RESEARCH_WALK_FORWARD_FOLDS if n_folds is None else n_folds
    bars_by_symbol = _fetch_bars(symbols, lookback_days)

    jobs = []
    for symbol, df in bars_by_symbol.items():
        if df is None or df.empty or len(df) < MIN_BARS_BEFORE_TRADING + 2:
            continue
        split_index = int(len(df) * BACKTEST_IN_SAMPLE_FRACTION)
        jobs.append((symbol, df, split_index))

    daily_strategy_names = [name for name, module in STRATEGIES.items() if getattr(module, "TIMEFRAME", "daily") == "daily"]

    results = {name: [] for name in daily_strategy_names}
    with ThreadPoolExecutor(max_workers=BACKTEST_MAX_WORKERS) as pool:
        futures = {}
        for strategy_name in daily_strategy_names:
            for symbol, df, split_index in jobs:
                future = pool.submit(_run_one_symbol_safely, strategy_name, symbol, df, split_index, _fold_boundaries(len(df), n_folds))
                futures[future] = strategy_name
        for future in as_completed(futures):
            results[futures[future]].extend(future.result())

    return results
