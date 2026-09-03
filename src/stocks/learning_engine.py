"""Self-learning loop for the stocks subsystem:

    Paper Trading -> collect results -> analyze trades -> discover
    patterns -> propose adjustments -> Backtest -> walk-forward /
    out-of-sample validation -> compare to the active strategy -> adopt
    ONLY a real, significant improvement -> record the new Strategy
    Version (the previous one is never deleted -- strategy_registry's
    history is append-only) -> automatic rollback if the active
    strategy's own live results degrade -> repeat.

Called periodically (not every cycle) from src.stocks.engine.run_forever
via maybe_run_learning_cycle() -- gated by BOTH a minimum number of new
closed paper trades (STOCKS_LEARNING_MIN_NEW_TRADES) and a minimum time
since the last run (STOCKS_LEARNING_CHECK_INTERVAL_SECONDS), so this
never re-backtests on every single 5-minute cycle, and never reacts to
one or two trades. Also runnable directly for a forced, immediate run:
`python -m src.stocks.learning_engine`.

Overfitting / look-ahead-bias guardrails already live one layer down,
in src.stocks.backtester (no-lookahead fills, walk-forward split) and
src.stocks.performance (summed, not compounded, returns across
independent parallel trades) -- this module's own guardrail is
_dominates(): a candidate must clear the active strategy's out-of-
sample profit factor by a real margin (STOCKS_LEARNING_MIN_PF_
IMPROVEMENT, not just a nominal tick up), meet the same statistical-
significance trade-count floor as everything else in this project
(BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE), and not be meaningfully worse
on expectancy or drawdown -- "clearly and robustly better", not
"noisier this run".
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.stocks.backtester import backtest_strategy
from src.stocks.config import (
    BACKTEST_LOOKBACK_DAYS,
    BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE,
    STOCKS_LEARNING_CHECK_INTERVAL_SECONDS,
    STOCKS_LEARNING_MIN_NEW_TRADES,
    STOCKS_LEARNING_MIN_PF_IMPROVEMENT,
    STOCKS_LEARNING_ROLLBACK_MIN_TRADES,
    STOCKS_UNIVERSE,
)
from src.stocks.paper_broker import load_state
from src.stocks.performance import compute_metrics
from src.stocks.strategy_registry import (
    DAILY_STRATEGIES,
    activate_strategy,
    get_active_strategy,
    get_previous_strategy,
    record_version,
)

logger = logging.getLogger(__name__)

LEARNING_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "learning_state.json"

_DEFAULT_STATE = {
    "last_run_at": None,
    "closed_trades_seen": 0,
    "last_action": None,       # "adopted" | "rolled_back" | "no_change" | "skipped_insufficient_data"
    "last_action_reason": None,
    "history": [],              # bounded append-only log of every learning-cycle decision
}


def _load_state():
    if not LEARNING_STATE_FILE.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(LEARNING_STATE_FILE.read_text(encoding="utf-8"))
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def _save_state(state):
    try:
        LEARNING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEARNING_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("Could not persist stocks learning state -- non-fatal")
    return state


def _record(state, action, reason):
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state["last_action"] = action
    state["last_action_reason"] = reason
    state["history"] = (state.get("history") or [])[-49:] + [{"at": state["last_run_at"], "action": action, "reason": reason}]
    return _save_state(state)


def _seconds_since(iso_timestamp):
    if not iso_timestamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds()


def analyze_live_paper_performance():
    """Metrics computed from the CURRENTLY active strategy's own closed
    PAPER trades (real fills this process has made, not a backtest) --
    trades logged under a different strategy (e.g. before it was
    activated) are excluded so this reflects the active strategy alone.
    """
    active = get_active_strategy()
    trades = load_state().get("closed_trades", [])
    if active:
        trades = [t for t in trades if t.get("strategy") == active]
    pnl_pcts = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
    return active, compute_metrics(pnl_pcts)


def _dominates(candidate_metrics, active_metrics):
    """Strict "really is better" rule -- see module docstring."""
    if candidate_metrics["trade_count"] < BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE:
        return False

    cand_pf = candidate_metrics["profit_factor"]
    active_pf = active_metrics["profit_factor"] or 0
    cand_pf_value = cand_pf if cand_pf != float("inf") else 1e9
    active_pf_value = active_pf if active_pf != float("inf") else 1e9
    if cand_pf_value < active_pf_value + STOCKS_LEARNING_MIN_PF_IMPROVEMENT:
        return False

    cand_exp = candidate_metrics["expectancy_pct"] if candidate_metrics["expectancy_pct"] is not None else -999.0
    active_exp = active_metrics["expectancy_pct"] if active_metrics["expectancy_pct"] is not None else -999.0
    if cand_exp < active_exp - 0.1:  # small tolerance, not a real regression
        return False

    cand_dd = candidate_metrics["max_drawdown_pct"] or 0
    active_dd = active_metrics["max_drawdown_pct"] or 0
    if active_dd > 0 and cand_dd > active_dd * 1.5:
        return False

    return True


def run_learning_cycle(force=False):
    """The full pipeline described in the module docstring. Safe to
    call directly (a script, a test, or run_forever's periodic check).
    Returns the persisted learning-state dict.
    """
    state = _load_state()
    closed_trades = load_state().get("closed_trades", [])
    new_trade_count = len(closed_trades) - state.get("closed_trades_seen", 0)

    # The historical-backtest-driven search below (Step 2) needs no live
    # paper trades at all -- gating it on trade count would mean a quiet
    # market (0 new closed trades) blocks it forever, exactly the bug
    # this OR-based check avoids: TIME ALONE is sufficient to run a
    # cycle; a burst of new trades can only make it run EARLIER, never
    # required to make it run at all. See STOCKS_LEARNING_MIN_NEW_TRADES'
    # own comment in config.py.
    enough_time_passed = _seconds_since(state.get("last_run_at")) >= STOCKS_LEARNING_CHECK_INTERVAL_SECONDS
    enough_new_trades = new_trade_count >= STOCKS_LEARNING_MIN_NEW_TRADES
    if not force and not (enough_time_passed or enough_new_trades):
        return _record(state, "skipped_insufficient_data",
                        f"checked too recently and only {new_trade_count} new closed paper trade(s) "
                        f"since last run -- next run in <={STOCKS_LEARNING_CHECK_INTERVAL_SECONDS:.0f}s "
                        f"or after {STOCKS_LEARNING_MIN_NEW_TRADES} new trades, whichever comes first")

    state["closed_trades_seen"] = len(closed_trades)

    # --- Step 1: rollback check -- is the active strategy's OWN live
    # paper performance bad enough to revert, independent of any fresh
    # backtest? This is the fastest, cheapest check, so it runs first. ---
    active_strategy, live_metrics = analyze_live_paper_performance()
    if active_strategy and live_metrics["trade_count"] >= STOCKS_LEARNING_ROLLBACK_MIN_TRADES:
        if (live_metrics["expectancy_pct"] or 0) < 0:
            previous = get_previous_strategy()
            if previous and previous != active_strategy:
                activate_strategy(previous)
                reason = (
                    f"{active_strategy}'s own live paper trades (n={live_metrics['trade_count']}) turned "
                    f"negative (expectancy {live_metrics['expectancy_pct']}%, PF {live_metrics['profit_factor']}) "
                    f"-- rolled back to the previously active strategy, {previous}"
                )
                logger.warning("Learning cycle: %s", reason)
                return _record(state, "rolled_back", reason)

    # --- Step 2: fresh walk-forward backtest of every daily strategy on
    # current real data, compared against the active strategy's own
    # fresh out-of-sample numbers (apples to apples, same data window). ---
    symbols = list(STOCKS_UNIVERSE)
    results = {}
    for name in DAILY_STRATEGIES:
        try:
            trades = backtest_strategy(name, symbols, BACKTEST_LOOKBACK_DAYS)
            out_of_sample = [t.pnl_pct for t in trades if not t.in_sample]
            results[name] = compute_metrics(out_of_sample)
        except Exception:
            logger.exception("Learning cycle: backtest failed for %s -- excluded from this comparison", name)

    baseline_name = active_strategy or "breakout"
    baseline_metrics = results.get(baseline_name)
    if baseline_metrics is None:
        return _record(state, "skipped_insufficient_data", f"could not backtest the baseline strategy {baseline_name!r} this run")

    best_name, best_metrics = None, None
    for name, metrics in results.items():
        if name == baseline_name:
            continue
        if _dominates(metrics, baseline_metrics) and (best_metrics is None or (metrics["profit_factor"] or 0) > (best_metrics["profit_factor"] or 0)):
            best_name, best_metrics = name, metrics

    if best_name is None:
        return _record(state, "no_change", f"{baseline_name} remains the best out-of-sample strategy of {sorted(results)}")

    rationale = (
        f"{best_name} beat the active strategy ({baseline_name}) out-of-sample on a fresh "
        f"backtest of {len(symbols)} symbols (PF {best_metrics['profit_factor']} vs {baseline_metrics['profit_factor']}, "
        f"expectancy {best_metrics['expectancy_pct']}% vs {baseline_metrics['expectancy_pct']}%, "
        f"maxDD {best_metrics['max_drawdown_pct']}% vs {baseline_metrics['max_drawdown_pct']}%, n={best_metrics['trade_count']})."
    )
    record_version(best_name, rationale=rationale)
    activate_strategy(best_name)
    logger.warning("Learning cycle: %s", rationale)
    return _record(state, "adopted", rationale)


def get_learning_state():
    """Public, read-only accessor for the dashboard (webapp/app.py) --
    just reads the persisted state, triggers no backtest/network call.
    """
    return _load_state()


def maybe_run_learning_cycle():
    """The entry point src.stocks.engine.run_forever() calls every
    trading cycle -- run_learning_cycle's own gating decides whether
    there's actually enough new, fresh evidence to do anything.
    """
    return run_learning_cycle(force=False)


def main():
    from src.logging_config import setup_logging
    setup_logging()
    result = run_learning_cycle(force=True)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
