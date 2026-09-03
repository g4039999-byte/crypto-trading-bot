"""Historical, large-scale strategy research pipeline -- the fast path
to statistical sample size this project's live paper-trading loop alone
would take weeks to accumulate: run every daily-bar strategy against
years of real market data across the whole universe (thousands of
resolved trades from one run), evaluate walk-forward/out-of-sample/
market-regime-conditioned robustness, rank against baselines, and check
parameter sensitivity for the leading strategy so a good result isn't
mistaken for an overfit one.

Pipeline stage mapping (matches the sequence requested):
    Historical Backtest Results   -- src.stocks.backtester.backtest_all_strategies()
    -> Research Dataset            -- every resolved BacktestTrade, tagged
                                       below with a walk-forward fold and
                                       a market regime, from ONE causal,
                                       no-lookahead simulation pass (no
                                       re-simulating per fold/regime --
                                       see backtester.py's own docstring)
    -> Strategy Analysis            -- analyze_strategy(): combined / in-
                                       sample / out-of-sample / per-fold /
                                       per-regime metrics
    -> Candidate Improvements       -- parameter_sensitivity_check(): a
                                       small grid on the leading
                                       strategy, ranked by cross-fold
                                       STABILITY first, raw return second
    -> ranking + assess_live_readiness(): an explicit, stated-criteria
       verdict -- NEVER a side effect on STOCKS_LIVE_TRADING (which
       stays hard-set False regardless of any conclusion here) and
       never itself calls activate_strategy()/record_version() (see
       scripts/research_stocks_strategies.py, the CLI that runs this
       and decides what to do with the result, so a plain research run
       -- e.g. from a test -- has no side effect on which strategy is
       live-paper-trading).

Survivorship-bias disclosure: STOCKS_UNIVERSE is a fixed, hand-curated
list of large/mid-cap tickers that still exist and trade today. Any
company that would have been delisted/failed/acquired during the
lookback window is, by construction, absent from this backtest -- a
real, disclosed limitation (not fixable without a paid delisted-
security dataset) that makes these results somewhat optimistic relative
to "every stock that existed then", not just "stocks that survived to
still be interesting today". Documented, never hidden -- see the
"survivorship_bias_disclosure" field on every report this produces.
"""

import itertools
import logging
import time
from datetime import datetime, timezone

import pandas as pd

from src.stocks import bar_cache
from src.stocks.backtester import backtest_all_strategies, backtest_strategy
from src.stocks.benchmarks import (
    buy_and_hold,
    simple_breakout_baseline,
    simple_momentum_baseline,
    simple_volume_baseline,
)
from src.stocks.config import (
    BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE,
    MARKET_REGIME_SYMBOL,
    RESEARCH_LOOKBACK_DAYS,
    RESEARCH_WALK_FORWARD_FOLDS,
    STOCKS_COMMISSION_PER_TRADE_USD,
    STOCKS_SLIPPAGE_BPS,
    STOCKS_UNIVERSE,
)
from src.stocks.data_provider import get_provider
from src.stocks.performance import compute_metrics
from src.stocks.regime import compute_regime_series
from src.stocks.strategies import STRATEGIES

logger = logging.getLogger(__name__)

MIN_TRADES_PER_REGIME_BUCKET = 10  # below this, a regime-bucket metric is too thin a sample to report


def tag_trades_with_regime(trades, regime_series):
    """Mutates `trades` in place, setting regime_trend/regime_volatility
    from the regime_series row as-of (at or before) each trade's own
    entry_date -- the market's regime as of the day the position was
    actually opened. A no-op (never raises) if regime_series is empty.
    """
    if regime_series is None or regime_series.empty or not trades:
        return trades
    dates = pd.to_datetime(regime_series.index)
    for trade in trades:
        try:
            entry_date = pd.to_datetime(trade.entry_date)
            idx = dates.searchsorted(entry_date, side="right") - 1
            if idx < 0:
                continue
            row = regime_series.iloc[idx]
            trade.regime_trend = row["trend"]
            trade.regime_volatility = row["volatility"]
        except Exception:
            continue
    return trades


def _fold_metrics(trades, n_folds):
    by_fold = {i: [] for i in range(n_folds)}
    for t in trades:
        by_fold.setdefault(t.fold_index, []).append(t.pnl_pct)
    return {i: compute_metrics(pnls) for i, pnls in by_fold.items()}


def _regime_metrics(trades):
    buckets = {}
    for t in trades:
        if t.regime_trend is None:
            continue
        key = f"{t.regime_trend}_{t.regime_volatility}"
        buckets.setdefault(key, []).append(t.pnl_pct)
    return {key: compute_metrics(pnls) for key, pnls in buckets.items() if len(pnls) >= MIN_TRADES_PER_REGIME_BUCKET}


def _fold_stability_score(fold_metrics):
    """Fraction of ALL folds (not just the ones that happened to have
    trades) with a positive expectancy AND profit_factor > 1. A fold
    with zero trades counts as NOT passing -- a strategy that only ever
    fires in one narrow historical window out of several folds hasn't
    demonstrated its edge holds broadly, even if every trade it *did*
    take was a winner, and a strategy that only "wins" in one lucky
    fold out of several is exactly the overfitting/regime-specific
    pattern this exists to catch, even when its AGGREGATE numbers look
    good.
    """
    if not fold_metrics:
        return 0.0
    passing = sum(
        1 for m in fold_metrics.values()
        if m["trade_count"] > 0 and (m["expectancy_pct"] or 0) > 0 and _pf_value(m["profit_factor"]) > 1
    )
    return round(passing / len(fold_metrics), 2)


def _pf_value(profit_factor):
    if profit_factor is None:
        return 0.0
    if profit_factor == float("inf"):
        return 1e9
    return profit_factor


def analyze_strategy(name, trades, n_folds):
    combined = [t.pnl_pct for t in trades]
    in_sample = [t.pnl_pct for t in trades if t.in_sample]
    out_of_sample = [t.pnl_pct for t in trades if not t.in_sample]
    fold_metrics = _fold_metrics(trades, n_folds)
    regime_metrics = _regime_metrics(trades)
    return {
        "strategy": name,
        "combined": compute_metrics(combined),
        "in_sample": compute_metrics(in_sample),
        "out_of_sample": compute_metrics(out_of_sample),
        "per_fold": {str(k): v for k, v in sorted(fold_metrics.items())},
        "per_regime": regime_metrics,
        "fold_stability_score": _fold_stability_score(fold_metrics),
    }


def _expand_grid(param_grid):
    keys = list(param_grid.keys())
    return [dict(zip(keys, values)) for values in itertools.product(*(param_grid[k] for k in keys))]


def parameter_sensitivity_check(strategy_name, param_grid, symbols, lookback_days, n_folds=None):
    """Re-run `strategy_name`'s backtest once per combination in
    param_grid ({attr_name: [candidate values], ...} -- a small grid,
    not a combinatorial explosion), temporarily monkey-patching the
    strategy module's own attributes, then always restoring them
    (try/finally). Ranked by fold_stability_score FIRST, aggregate
    profit_factor second -- picking the single highest-return
    combination without checking cross-fold stability is exactly the
    overfitting failure mode this exists to guard against (item 10).
    Never raises: a combination whose backtest fails is recorded with
    zeroed-out metrics rather than aborting the whole grid.
    """
    n_folds = n_folds or RESEARCH_WALK_FORWARD_FOLDS
    module = STRATEGIES[strategy_name]
    originals = {attr: getattr(module, attr) for attr in param_grid}
    combos = _expand_grid(param_grid)

    results = []
    try:
        for combo in combos:
            for attr, value in combo.items():
                setattr(module, attr, value)
            try:
                trades = backtest_strategy(strategy_name, symbols, lookback_days, n_folds=n_folds)
                fold_metrics = _fold_metrics(trades, n_folds)
                combined_metrics = compute_metrics([t.pnl_pct for t in trades])
            except Exception:
                logger.exception("Parameter sensitivity check failed for %s with %s", strategy_name, combo)
                combined_metrics = compute_metrics([])
                fold_metrics = {}
            results.append({
                "params": dict(combo), "combined": combined_metrics,
                "fold_stability_score": _fold_stability_score(fold_metrics),
            })
    finally:
        for attr, value in originals.items():
            setattr(module, attr, value)

    results.sort(key=lambda r: (r["fold_stability_score"], _pf_value(r["combined"]["profit_factor"])), reverse=True)
    return results


def _rank_strategies(strategies_report, baselines_report):
    buy_and_hold_sharpe = baselines_report.get("buy_and_hold", {}).get("sharpe") or -999
    ranked = []
    for name, report in strategies_report.items():
        oos = report["out_of_sample"]
        combined = report["combined"]
        significant = (
            combined["trade_count"] >= BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE
            and oos["trade_count"] >= max(5, BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE // 2)
        )
        ranked.append({
            "strategy": name,
            "statistically_significant": significant,
            "combined_trade_count": combined["trade_count"],
            "out_of_sample_trade_count": oos["trade_count"],
            "out_of_sample_profit_factor": oos["profit_factor"],
            "out_of_sample_expectancy_pct": oos["expectancy_pct"],
            "combined_sharpe": combined["sharpe"],
            "fold_stability_score": report["fold_stability_score"],
            "beats_buy_and_hold_on_sharpe": (combined.get("sharpe") or -999) > buy_and_hold_sharpe,
        })
    ranked.sort(key=lambda r: (
        r["statistically_significant"],
        r["fold_stability_score"],
        r["out_of_sample_expectancy_pct"] if r["out_of_sample_expectancy_pct"] is not None else -999,
        _pf_value(r["out_of_sample_profit_factor"]),
    ), reverse=True)
    return ranked


def _calmar_like_ratio(total_return_pct, max_drawdown_pct):
    """total_return_pct / max_drawdown_pct on this project's summed-
    (not compounded-) return convention -- see performance.py's module
    docstring. Using max_drawdown_pct as an ABSOLUTE cap doesn't work
    here: both total_return_pct and max_drawdown_pct are running sums
    across every trade in the sample, so both scale up together with
    sample size (11,000+ trades over a 10-year, 47-symbol run produces
    a "max_drawdown_pct" over 100 in absolute terms purely from volume,
    which is not comparable to a real portfolio's max drawdown -- real
    trading is constrained by STOCKS_MAX_OPEN_POSITIONS/STOCKS_MAX_
    CAPITAL_DEPLOYMENT_PCT/the drawdown circuit breaker in a way this
    per-trade-sum metric doesn't model). Their RATIO is scale-invariant
    (a Calmar-ratio analogue): a strategy that earned several times its
    own worst cumulative drawdown is a meaningfully different claim than
    one that barely broke even against its drawdown, regardless of how
    many trades either was measured over.
    """
    if max_drawdown_pct is None or max_drawdown_pct <= 0:
        return float("inf") if (total_return_pct or 0) > 0 else 0.0
    return (total_return_pct or 0) / max_drawdown_pct


def assess_live_readiness(strategy_report, thresholds=None):
    """A candidate strategy is a "Live Candidate" only if EVERY one of
    these explicit, stated criteria (item 16) holds -- never a blind
    single-number score:
      - enough combined AND out-of-sample trades to be statistically
        meaningful (not adopted from a handful of lucky trades)
      - out-of-sample expectancy is positive (the edge held up on
        unseen data, not just the tuned/in-sample portion)
      - out-of-sample profit factor clears a real margin above 1.0 (not
        just barely positive)
      - cumulative return clears a stated multiple of the worst
        cumulative drawdown experienced (see _calmar_like_ratio() --
        NOT a raw drawdown-percent cap, which isn't meaningful on this
        project's summed-return convention across a large trade sample)
      - fold_stability_score shows the edge holds across MOST of the
        walk-forward folds, not one lucky one
    Returns {"verdict": "LIVE_CANDIDATE"|"NOT_READY", "criteria": {...
      each with pass/fail and the actual value}}. This function makes
    NO changes to any file and NEVER touches STOCKS_LIVE_TRADING --
    reaching LIVE_CANDIDATE here still requires a separate, explicit
    human decision before any real order could ever be placed (and this
    codebase has no code path that could place one regardless).
    """
    thresholds = thresholds or {
        "min_combined_trades": BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE,
        "min_out_of_sample_trades": max(10, BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE // 2),
        "min_out_of_sample_profit_factor": 1.15,
        "min_return_to_drawdown_ratio": 2.0,
        "min_fold_stability_score": 0.6,
    }
    combined = strategy_report["combined"]
    oos = strategy_report["out_of_sample"]
    stability = strategy_report["fold_stability_score"]
    calmar_ratio = _calmar_like_ratio(combined.get("total_return_pct"), combined.get("max_drawdown_pct"))

    checks = {
        "enough_combined_trades": {
            "pass": combined["trade_count"] >= thresholds["min_combined_trades"],
            "value": combined["trade_count"], "threshold": thresholds["min_combined_trades"],
        },
        "enough_out_of_sample_trades": {
            "pass": oos["trade_count"] >= thresholds["min_out_of_sample_trades"],
            "value": oos["trade_count"], "threshold": thresholds["min_out_of_sample_trades"],
        },
        "positive_out_of_sample_expectancy": {
            "pass": (oos["expectancy_pct"] or -999) > 0,
            "value": oos["expectancy_pct"], "threshold": 0,
        },
        "out_of_sample_profit_factor_above_threshold": {
            "pass": _pf_value(oos["profit_factor"]) >= thresholds["min_out_of_sample_profit_factor"],
            "value": oos["profit_factor"], "threshold": thresholds["min_out_of_sample_profit_factor"],
        },
        "return_to_drawdown_ratio_above_threshold": {
            "pass": calmar_ratio >= thresholds["min_return_to_drawdown_ratio"],
            "value": round(calmar_ratio, 2) if calmar_ratio != float("inf") else calmar_ratio, "threshold": thresholds["min_return_to_drawdown_ratio"],
        },
        "stable_across_walk_forward_folds": {
            "pass": stability >= thresholds["min_fold_stability_score"],
            "value": stability, "threshold": thresholds["min_fold_stability_score"],
        },
    }
    verdict = "LIVE_CANDIDATE" if all(c["pass"] for c in checks.values()) else "NOT_READY"
    return {"verdict": verdict, "criteria": checks, "thresholds_used": thresholds}


def run_research(symbols=None, lookback_days=None, n_folds=None):
    """The full pipeline. Returns a plain-dict, JSON-serializable
    report. Never calls activate_strategy()/record_version() itself --
    see the module docstring.
    """
    started_at = time.time()
    symbols = list(symbols) if symbols is not None else list(STOCKS_UNIVERSE)
    lookback_days = lookback_days or RESEARCH_LOOKBACK_DAYS
    n_folds = n_folds or RESEARCH_WALK_FORWARD_FOLDS

    logger.info("Research pipeline: backtesting every daily strategy across %s symbols, %s lookback days, %s folds", len(symbols), lookback_days, n_folds)
    strategy_trades = backtest_all_strategies(symbols, lookback_days, n_folds=n_folds)

    provider = get_provider()
    regime_bars = bar_cache.get_daily_bars_batch_cached(provider, [MARKET_REGIME_SYMBOL], lookback_days)
    regime_series = compute_regime_series(regime_bars.get(MARKET_REGIME_SYMBOL))
    for trades in strategy_trades.values():
        tag_trades_with_regime(trades, regime_series)

    strategies_report = {name: analyze_strategy(name, trades, n_folds) for name, trades in strategy_trades.items()}

    logger.info("Research pipeline: computing baselines")
    baselines_report = {
        "buy_and_hold": compute_metrics(buy_and_hold(symbols, lookback_days)),
        "simple_momentum": compute_metrics(simple_momentum_baseline(symbols, lookback_days)),
        "simple_breakout": compute_metrics(simple_breakout_baseline(symbols, lookback_days)),
        "simple_volume": compute_metrics(simple_volume_baseline(symbols, lookback_days)),
    }

    ranking = _rank_strategies(strategies_report, baselines_report)
    readiness = {name: assess_live_readiness(report) for name, report in strategies_report.items()}

    total_trades = sum(r["combined"]["trade_count"] for r in strategies_report.values())
    elapsed = time.time() - started_at
    logger.info("Research pipeline: done in %.1fs -- %s total trades across %s strategies", elapsed, total_trades, len(strategies_report))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "universe_size": len(symbols),
        "lookback_days": lookback_days,
        "n_folds": n_folds,
        "total_trades_across_all_strategies": total_trades,
        "strategies": strategies_report,
        "baselines": baselines_report,
        "ranking": ranking,
        "live_readiness": readiness,
        "costs_modeled": {
            "slippage_bps": STOCKS_SLIPPAGE_BPS,
            "commission_per_trade_usd": STOCKS_COMMISSION_PER_TRADE_USD,
        },
        "survivorship_bias_disclosure": (
            "STOCKS_UNIVERSE is a fixed list of tickers that still exist today; any "
            "symbol that would have been delisted/failed/acquired during the lookback "
            "window is absent by construction. Results are somewhat optimistic "
            "relative to a full historical universe including failed companies."
        ),
    }
