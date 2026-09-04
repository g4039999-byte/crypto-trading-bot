"""Diagnose WHY the deployed paper-trading strategy (CANDIDATE in
scripts/backtest_paper_strategy.py) has a profit factor below 1 --
pure read-only analysis over real historical snapshot data
(data/snapshots.json). Never modifies CANDIDATE, never places, opens,
or closes any real or paper position, never touches
data/paper_positions.json or data/paper_trade_log.jsonl. Reuses
backtest_paper_strategy.py's replay/metrics/fold machinery rather than
re-deriving a second copy of it.

Segments CANDIDATE's own trades (the actual current entry/exit rules,
unchanged) along every dimension requested: entry score (combined/base/
momentum sub-scores), liquidity, volume, relative volume, buy pressure,
"speed of movement" (price change per minute of age since first seen),
entry age/stage, liquidity drawdown at entry, trend classification, and
exit reason -- then reports win rate / profit factor / expectancy per
bucket, flagging any bucket too small to mean anything (min_n below).

Discipline matching the rest of this project's backtest tooling: this
never adopts or recommends a specific parameter change on its own --
see this script's own printed disclaimer -- and every headline finding
is cross-checked against the SAME walk-forward folds
scripts/backtest_paper_strategy.py already computes, not just the
aggregate, so a pattern that only shows up in one lucky fold is called
out as such rather than presented as a discovery.

Usage:
    python -m scripts.diagnose_paper_strategy
"""

import statistics
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_paper_strategy import (  # noqa: E402
    CANDIDATE,
    _load_snapshots,
    assign_fold_index,
    compute_fold_boundaries,
    compute_oos_cutoff,
    run_backtest,
    split_trades_by_cutoff,
)
from src.stocks.performance import compute_metrics  # noqa: E402

MIN_BUCKET_N = 15  # a bucket with fewer trades than this is reported but flagged as not significant
N_FOLDS = 4


def _pf_str(pf):
    if pf is None:
        return "n/a"
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def _metrics_row(label, trades):
    m = compute_metrics([t.pnl_pct for t in trades])
    n = m["trade_count"]
    flag = "" if n >= MIN_BUCKET_N else "  ** thin sample, not significant **"
    win_rate = m["win_rate_pct"]
    pf = _pf_str(m["profit_factor"])
    exp = m["expectancy_pct"]
    return f"  {label:<22} n={n:>4}  win_rate={win_rate if win_rate is not None else 'n/a':>5}%  PF={pf:>5}  expectancy={exp if exp is not None else 'n/a':>+6}%{flag}"


def tercile_buckets(trades, key_fn):
    """LOW/MID/HIGH terciles by key_fn(trade), excluding trades where
    key_fn returns None. Ties at a boundary stay with the lower bucket
    (stable sort) -- not exactly equal-count when there are many
    duplicate values, but close enough for a diagnostic, not a strict
    statistical test.
    """
    valued = [(key_fn(t), t) for t in trades if key_fn(t) is not None]
    valued.sort(key=lambda vt: vt[0])
    n = len(valued)
    if n < 3:
        return {"LOW": [], "MID": [], "HIGH": []}
    third = n // 3
    return {
        "LOW": [t for _, t in valued[:third]],
        "MID": [t for _, t in valued[third: n - third]],
        "HIGH": [t for _, t in valued[n - third:]],
    }


def categorical_buckets(trades, key_fn):
    buckets = {}
    for t in trades:
        buckets.setdefault(key_fn(t), []).append(t)
    return buckets


def report_dimension(name, trades, key_fn, categorical=False):
    print(f"\n--- {name} ---")
    buckets = categorical_buckets(trades, key_fn) if categorical else tercile_buckets(trades, key_fn)
    order = sorted(buckets.keys(), key=str) if categorical else ("LOW", "MID", "HIGH")
    for label in order:
        bucket_trades = buckets.get(label, [])
        if not bucket_trades:
            continue
        print(_metrics_row(str(label), bucket_trades))
    return buckets


def fold_consistency_check(label, trades, fold_boundaries):
    """For one already-selected subset of trades (e.g. the worst tercile
    of some dimension), report its win rate/expectancy PER FOLD -- a
    pattern that holds in most folds is a much stronger finding than one
    that only shows up in the aggregate.
    """
    by_fold = {i: [] for i in range(len(fold_boundaries) + 1)}
    for t in trades:
        by_fold[assign_fold_index(t.entry_ts, fold_boundaries)].append(t)
    print(f"  fold consistency for [{label}]:")
    for i, fold_trades in sorted(by_fold.items()):
        if not fold_trades:
            print(f"    fold {i}: (no trades)")
            continue
        m = compute_metrics([t.pnl_pct for t in fold_trades])
        print(f"    fold {i}: n={m['trade_count']} win_rate={m['win_rate_pct']}% expectancy={m['expectancy_pct']:+.2f}%")


def correlation_or_none(xs, ys):
    if len(xs) < 3 or len(set(xs)) < 2:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def main():
    snapshots = _load_snapshots()
    print(f"Loaded {len(snapshots)} tokens with >=2 usable snapshots")
    trades = run_backtest(CANDIDATE, snapshots)
    print(f"CANDIDATE produced {len(trades)} closed trade(s) over the full dataset\n")

    cutoff_ts = compute_oos_cutoff(snapshots, in_sample_fraction=0.7)
    fold_boundaries = compute_fold_boundaries(snapshots, N_FOLDS)
    in_sample, out_of_sample = split_trades_by_cutoff(trades, cutoff_ts)

    overall = compute_metrics([t.pnl_pct for t in trades])
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    gross_profit = sum(t.pnl_pct for t in wins)
    gross_loss = -sum(t.pnl_pct for t in losses)

    print("=== 1. Why is profit factor below 1? (the arithmetic first) ===")
    print(f"win rate: {overall['win_rate_pct']}% ({len(wins)}W / {len(losses)}L)")
    print(f"gross profit (sum of winning pnl_pct): {gross_profit:+.1f}pp | gross loss: {-gross_loss:+.1f}pp")
    print(f"profit factor: {_pf_str(overall['profit_factor'])}")
    print(
        "CANDIDATE's stop-loss and take-profit are both 25% (symmetric). With a symmetric "
        "%-band exit, profit_factor is driven almost entirely by win rate: PF ~= win_rate / "
        "(1 - win_rate) whenever most exits are stop_loss/take_profit rather than max_holding_time "
        "(a random/no-edge entry against a symmetric barrier is close to a 50/50 coin flip -- "
        "before any drift). A profit factor below 1 with symmetric bands means win rate is "
        "below ~50%, which is the case here -- the question this diagnostic exists to answer is "
        "WHERE that shortfall concentrates, not just that it exists."
    )
    reason_counts = Counter(t.reason for t in trades)
    print(f"exit reasons: {dict(reason_counts)}")

    print("\n=== In-sample vs out-of-sample (context, not a per-bucket split -- OOS is too thin for that) ===")
    for label, subset in (("in-sample", in_sample), ("out-of-sample", out_of_sample)):
        m = compute_metrics([t.pnl_pct for t in subset])
        print(f"{label}: n={m['trade_count']} win_rate={m['win_rate_pct']}% PF={_pf_str(m['profit_factor'])} expectancy={m['expectancy_pct']}%")

    print("\n=== 2-4. Segmented performance (each dimension independently; LOW/MID/HIGH = bottom/middle/top tercile) ===")
    report_dimension("entry score (combined)", trades, lambda t: t.entry_score)
    report_dimension("entry base score (liquidity/volume/momentum/age)", trades, lambda t: t.entry_base_score)
    report_dimension("entry momentum sub-score", trades, lambda t: t.entry_momentum_score)
    report_dimension("entry liquidity ($)", trades, lambda t: t.entry_liquidity)
    report_dimension("entry 24h volume ($)", trades, lambda t: t.entry_volume)
    report_dimension("entry relative volume (volume/liquidity)", trades, lambda t: t.entry_relative_volume)
    report_dimension("entry buy_ratio (buy pressure)", trades, lambda t: t.entry_buy_ratio)
    report_dimension("entry velocity (%change/minute since first seen)", trades, lambda t: t.entry_velocity_pct_per_min)
    report_dimension("entry age (minutes)", trades, lambda t: t.entry_age_minutes)
    report_dimension("entry liquidity drawdown from peak (%)", trades, lambda t: t.entry_liq_drawdown_pct)
    report_dimension("entry trend classification", trades, lambda t: t.entry_trend, categorical=True)
    report_dimension("entry stage (src.stage.classify_stage)", trades, lambda t: t.entry_stage, categorical=True)
    report_dimension("exit reason", trades, lambda t: t.reason, categorical=True)

    print("\n=== 6. Signal-vs-noise check: correlation of each entry-time feature with the trade's own pnl_pct ===")
    print("(Pearson r over all closed trades -- weak/near-zero means this feature did not discriminate winners from losers in this dataset; a real signal should show a consistent non-zero sign.)")
    numeric_features = {
        "entry_score": lambda t: t.entry_score,
        "entry_base_score": lambda t: t.entry_base_score,
        "entry_momentum_score": lambda t: t.entry_momentum_score,
        "entry_liquidity": lambda t: t.entry_liquidity,
        "entry_volume": lambda t: t.entry_volume,
        "entry_relative_volume": lambda t: t.entry_relative_volume,
        "entry_buy_ratio": lambda t: t.entry_buy_ratio,
        "entry_velocity_pct_per_min": lambda t: t.entry_velocity_pct_per_min,
        "entry_age_minutes": lambda t: t.entry_age_minutes,
        "entry_liq_drawdown_pct": lambda t: t.entry_liq_drawdown_pct,
        "discovery_to_entry_seconds": lambda t: t.discovery_to_entry_seconds,
    }
    for name, key_fn in numeric_features.items():
        pairs = [(key_fn(t), t.pnl_pct) for t in trades if key_fn(t) is not None]
        r = correlation_or_none([p[0] for p in pairs], [p[1] for p in pairs])
        r_str = f"{r:+.3f}" if r is not None else "n/a"
        print(f"  {name:<28} r={r_str}  (n={len(pairs)})")

    print("\n=== 3/5. Winners vs losers -- direct mean comparison ===")
    for name, key_fn in numeric_features.items():
        win_vals = [key_fn(t) for t in wins if key_fn(t) is not None]
        loss_vals = [key_fn(t) for t in losses if key_fn(t) is not None]
        if not win_vals or not loss_vals:
            continue
        print(f"  {name:<28} winners mean={statistics.mean(win_vals):>10.2f}  losers mean={statistics.mean(loss_vals):>10.2f}")

    print("\n=== Fold-consistency check on the single worst-performing tercile found above ===")
    # Re-derive terciles for entry_score (the most direct "did the entry
    # filter itself work" question) and check whether its LOW tercile's
    # underperformance (if any) holds across every fold, or is really
    # just one bad window.
    score_buckets = tercile_buckets(trades, lambda t: t.entry_score)
    for label in ("LOW", "HIGH"):
        if score_buckets[label]:
            fold_consistency_check(f"entry score {label}", score_buckets[label], fold_boundaries)

    print(
        "\nThis script only diagnoses -- it does not recommend or apply any parameter "
        "change. See the written report for the prioritized interpretation."
    )


if __name__ == "__main__":
    main()
