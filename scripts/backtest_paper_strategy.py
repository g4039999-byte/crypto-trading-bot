"""Replay src/paper_trader.py's entry/exit rules against real historical
snapshot data (data/snapshots.json) already collected by the radar during
live runs, to compare the CURRENT rule set against a CANDIDATE change
using actual market data instead of a handful of live trades.

Read-only analysis. Never places, opens, or closes any real or paper
position -- data/paper_positions.json and data/paper_trade_log.jsonl are
never touched. Safe to run any time, as many times as needed.

Why this exists: after the first live paper-trading run (2026-09-03)
lost on 3/3 closed trades, the two losers shared a visible pattern in
their own snapshot history -- liquidity had already collapsed >50% from
its recent peak by the time of entry, in both cases -- and one of them
was a *second* entry into a token that had already stopped out once,
20 minutes earlier, and was still falling. Rather than tune thresholds
off that one incident, this replays EVERY token this radar has ever
recorded (167 tokens / ~7000 snapshots as of this writing) under both
rule sets and reports the aggregate difference.

Known approximations (data/snapshots.json stores less than a live
DexScreener pair payload, so some of scoring.calculate_score()'s inputs
are reconstructed rather than the real 24h figures):
  - age_minutes = minutes since THIS script's replay first saw the
    token (i.e. since the radar's first snapshot of it), not the
    token's true on-chain pair age.
  - priceChange.h24 = % change from the token's first recorded price to
    the current snapshot, not a true rolling 24h window.
  - The Jupiter honeypot/sellability check is assumed to pass for every
    candidate (no historical quote data exists to replay it) -- this
    harness is testing the liquidity/trend/re-entry rules specifically,
    not honeypot detection.
Both strategies see the exact same (approximated) data, so a difference
between them reflects the rule change being tested, not the
approximation -- but the absolute numbers (win rate, $ PnL) should be
read as directional, not as a precise forecast.

Usage:
    python -m scripts.backtest_paper_strategy
"""

import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import json  # noqa: E402

from src.momentum import calculate_momentum  # noqa: E402
from src.observation import compute_trend  # noqa: E402
from src.risk import assess_token_safety  # noqa: E402
from src.scoring import calculate_score  # noqa: E402

SNAPSHOT_FILE = PROJECT_ROOT / "data" / "snapshots.json"

SELLABLE_STUB = {"sellable": True, "reason": None, "round_trip_loss_pct": 2.0}
TRADE_SIZE_USD = 5.0  # matches MAX_TRADE_USD -- fixed, for comparable $ PnL across strategies


@dataclass
class Strategy:
    name: str
    min_score: int
    entry_trends: tuple
    min_liquidity_usd: float
    min_volume_24h_usd: float
    min_age_minutes: float
    max_age_minutes: float
    stop_loss_pct: float
    take_profit_pct: float
    max_holding_minutes: float
    max_liq_drawdown_pct: float = None  # None = no liquidity-decline guard
    stop_loss_cooldown_minutes: float = 0  # 0 = no cooldown; otherwise, minutes to skip re-entry after a stop_loss on that token


@dataclass
class Trade:
    token: str
    entry_idx: int
    entry_ts: datetime
    entry_price: float
    entry_score: int
    entry_trend: str
    entry_age_minutes: float
    seconds_since_first_seen: float
    exit_ts: datetime = None
    exit_price: float = None
    reason: str = None
    pnl_usd: float = None
    pnl_pct: float = None


def _parse_ts(s):
    return datetime.fromisoformat(s)


def _load_snapshots():
    """price_usd comes straight from DexScreener's priceUsd field, which
    is a JSON *string* (e.g. "0.0004858") -- src/snapshot.py stores it
    as-is. Coerce to float here, once, so every consumer below can treat
    price_usd as a number.
    """
    data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    out = {}
    for addr, hist in data.items():
        clean = []
        for h in hist:
            if not isinstance(h, dict):
                continue
            try:
                price = float(h.get("price_usd"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            clean.append({**h, "price_usd": price})
        if len(clean) >= 2:
            out[addr] = clean
    return out


def _build_pair(snapshot, price_change_pct):
    return {
        "liquidity": {"usd": snapshot.get("liquidity_usd")},
        "volume": {"h24": snapshot.get("volume_24h")},
        "priceChange": {"h24": price_change_pct},
        "txns": {"h24": {"buys": snapshot.get("buys_24h") or 0, "sells": snapshot.get("sells_24h") or 0}},
    }


def _evaluate_point(history, idx):
    """Everything the real pipeline would know about this token at
    history[idx], reconstructed from historical snapshots only (nothing
    from indices > idx -- no lookahead).
    """
    current = history[idx]
    previous = history[idx - 1]
    trend = compute_trend(previous, current)["trend"]

    first = history[0]
    age_minutes = (_parse_ts(current["timestamp"]) - _parse_ts(first["timestamp"])).total_seconds() / 60
    first_price = first.get("price_usd")
    price = current.get("price_usd")
    price_change_pct = ((price - first_price) / first_price * 100) if first_price else 0.0

    pair = _build_pair(current, price_change_pct)
    base_score = calculate_score(pair, age_minutes=age_minutes)
    momentum_score = calculate_momentum(pair)
    score = round(base_score * 0.60 + momentum_score * 0.40)

    peak_liq = max((h.get("liquidity_usd") or 0) for h in history[: idx + 1])
    liquidity = current.get("liquidity_usd") or 0
    liq_drawdown_pct = ((peak_liq - liquidity) / peak_liq * 100) if peak_liq else 0.0

    return {
        "score": score,
        "trend": trend,
        "liquidity": current.get("liquidity_usd"),
        "volume": current.get("volume_24h"),
        "age": age_minutes,
        "buys": current.get("buys_24h") or 0,
        "sells": current.get("sells_24h") or 0,
        "price_usd": price,
        "liq_drawdown_pct": liq_drawdown_pct,
    }


def _passes_entry(evaluated, strategy):
    if evaluated["score"] < strategy.min_score:
        return False
    if evaluated["trend"] not in strategy.entry_trends:
        return False
    if strategy.max_liq_drawdown_pct is not None and evaluated["liq_drawdown_pct"] > strategy.max_liq_drawdown_pct:
        return False
    risk = assess_token_safety(
        evaluated, SELLABLE_STUB,
        min_liquidity_usd=strategy.min_liquidity_usd,
        min_volume_24h_usd=strategy.min_volume_24h_usd,
        min_pair_age_minutes=strategy.min_age_minutes,
        max_pair_age_minutes=strategy.max_age_minutes,
    )
    return risk.passed


def _check_exit(entry_price, entry_ts, current_price, current_ts, strategy):
    if current_price <= entry_price * (1 - strategy.stop_loss_pct / 100):
        return "stop_loss"
    if current_price >= entry_price * (1 + strategy.take_profit_pct / 100):
        return "take_profit"
    held_minutes = (current_ts - entry_ts).total_seconds() / 60
    if held_minutes >= strategy.max_holding_minutes:
        return "max_holding_time"
    return None


def replay_token(token, history, strategy):
    """One token's full snapshot history, walked in order. Returns the
    list of closed Trades (each fully resolved by the end of the
    history -- an entry still open when the data runs out is dropped
    from the stats, same as the live system just hasn't decided yet).
    """
    trades = []
    in_position = None
    cooldown_until = None  # datetime | None -- set after a stop_loss when strategy.stop_loss_cooldown_minutes > 0

    for idx in range(1, len(history)):
        current = history[idx]
        ts = _parse_ts(current["timestamp"])
        price = current.get("price_usd")

        if in_position is not None:
            reason = _check_exit(in_position.entry_price, in_position.entry_ts, price, ts, strategy)
            if reason:
                in_position.exit_ts = ts
                in_position.exit_price = price
                in_position.reason = reason
                in_position.pnl_pct = (price - in_position.entry_price) / in_position.entry_price * 100
                in_position.pnl_usd = in_position.pnl_pct / 100 * TRADE_SIZE_USD
                trades.append(in_position)
                if strategy.stop_loss_cooldown_minutes and reason == "stop_loss":
                    cooldown_until = ts + timedelta(minutes=strategy.stop_loss_cooldown_minutes)
                in_position = None
            continue  # mirrors the live code: never also re-enter in the same step as an exit

        if cooldown_until is not None and ts < cooldown_until:
            continue

        evaluated = _evaluate_point(history, idx)
        if _passes_entry(evaluated, strategy):
            in_position = Trade(
                token=token, entry_idx=idx, entry_ts=ts, entry_price=price,
                entry_score=evaluated["score"], entry_trend=evaluated["trend"],
                entry_age_minutes=evaluated["age"],
                seconds_since_first_seen=(ts - _parse_ts(history[0]["timestamp"])).total_seconds(),
            )

    return trades


def run_backtest(strategy, snapshots):
    all_trades = []
    for token, history in snapshots.items():
        all_trades.extend(replay_token(token, history, strategy))
    return all_trades


def dataset_time_bounds(snapshots):
    """(earliest, latest) timestamp across every snapshot in the
    dataset -- for display only (see compute_oos_cutoff()'s docstring
    for why the actual split does NOT use the midpoint of this range).
    """
    all_ts = _all_snapshot_timestamps(snapshots)
    return min(all_ts), max(all_ts)


def _all_snapshot_timestamps(snapshots):
    return [
        _parse_ts(h["timestamp"])
        for history in snapshots.values()
        for h in history
        if h.get("timestamp")
    ]


def compute_oos_cutoff(snapshots, in_sample_fraction=0.7):
    """A single global cutoff timestamp splitting the dataset into an
    in-sample fraction (before the cutoff) and an out-of-sample fraction
    (from the cutoff on) -- the same chronological-holdout principle
    src/stocks/research_pipeline.py uses (BACKTEST_IN_SAMPLE_FRACTION),
    applied here per-trade via split_trades_by_cutoff() rather than
    per-token, since most tokens' own histories are far shorter than the
    dataset's full span.

    Deliberately the in_sample_fraction-th PERCENTILE of every snapshot's
    own timestamp, not the midpoint of (earliest, latest) -- the two are
    very different in practice: SNAPSHOT_HISTORY_LIMIT caps how much
    history any one token keeps, so as the radar runs longer, older
    snapshots keep aging out while the number of actively-tracked tokens
    grows, making the dataset's snapshot DENSITY heavily skewed toward
    the recent end (observed directly: with SNAPSHOT_HISTORY_LIMIT=60,
    90% of all snapshots on 2026-09-04 were from the last ~24h even
    though the earliest single snapshot was ~4.6 days old). A midpoint-
    of-range cutoff put essentially 100% of trades in the "out-of-sample"
    bucket and left "in-sample" empty -- useless for validation. The
    percentile split instead reflects where the actual entry
    opportunities are, so both buckets end up with real trades to judge.
    """
    all_ts = sorted(_all_snapshot_timestamps(snapshots))
    if not all_ts:
        raise ValueError("cannot compute an out-of-sample cutoff from an empty dataset")
    idx = min(len(all_ts) - 1, int(len(all_ts) * in_sample_fraction))
    return all_ts[idx]


def split_trades_by_cutoff(trades, cutoff_ts):
    """Returns (in_sample, out_of_sample) -- trades entered before
    cutoff_ts vs at/after it. A trade's *entry* time decides which
    bucket it falls in, even if it exits after the cutoff (mirrors
    entering a real position: the decision was made with only
    in-sample-window information available at that moment).
    """
    in_sample = [t for t in trades if t.entry_ts < cutoff_ts]
    out_of_sample = [t for t in trades if t.entry_ts >= cutoff_ts]
    return in_sample, out_of_sample


def summarize(name, trades, verbose=True):
    n = len(trades)
    if verbose:
        print(f"\n=== {name}: {n} closed trade(s) ===")
    if n == 0:
        if verbose:
            print("(no trades)")
        return {"n": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0}

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    avg_win = statistics.mean(t.pnl_usd for t in wins) if wins else 0.0
    avg_loss = statistics.mean(t.pnl_usd for t in losses) if losses else 0.0
    expectancy = total_pnl / n  # avg $ PnL per trade -- comparable across strategies with different trade counts, unlike raw total_pnl

    reason_counts = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    if verbose:
        avg_age = statistics.mean(t.entry_age_minutes for t in trades)
        avg_discovery_to_entry_s = statistics.mean(t.seconds_since_first_seen for t in trades)
        print(f"wins: {len(wins)} | losses: {len(losses)} | win rate: {100*len(wins)/n:.1f}%")
        print(f"total PnL: ${total_pnl:+.2f} | expectancy: ${expectancy:+.3f}/trade | avg win: ${avg_win:+.2f} | avg loss: ${avg_loss:+.2f}")
        print(f"avg age at entry: {avg_age:.1f}m | avg time from first-seen to entry: {avg_discovery_to_entry_s:.0f}s")
        print(f"exit reasons: {reason_counts}")
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "total_pnl": total_pnl,
        "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": expectancy,
    }


def summarize_with_oos(name, trades, cutoff_ts, verbose=True):
    """Full-dataset summary plus the same broken out separately for the
    in-sample and out-of-sample windows (see compute_oos_cutoff()) --
    the out-of-sample numbers are what should actually decide whether a
    candidate strategy is adopted, not the aggregate.
    """
    overall = summarize(f"{name} (full dataset)", trades, verbose=verbose)
    in_sample, out_of_sample = split_trades_by_cutoff(trades, cutoff_ts)
    overall["in_sample"] = summarize(f"{name} (in-sample)", in_sample, verbose=verbose)
    overall["out_of_sample"] = summarize(f"{name} (out-of-sample)", out_of_sample, verbose=verbose)
    return overall


CURRENT = Strategy(
    name="CURRENT (deployed)",
    min_score=45, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=5, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=50, max_holding_minutes=240,
    max_liq_drawdown_pct=None, stop_loss_cooldown_minutes=0,
)

CANDIDATE = Strategy(
    name="CANDIDATE (deployed, matches src/config.py's PAPER_* defaults)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=25, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
)


def main():
    snapshots = _load_snapshots()
    print(f"Loaded {len(snapshots)} tokens with >=2 usable snapshots from {SNAPSHOT_FILE}")

    cutoff_ts = compute_oos_cutoff(snapshots, in_sample_fraction=0.7)
    earliest, latest = dataset_time_bounds(snapshots)
    print(f"Dataset spans {earliest.isoformat()} .. {latest.isoformat()}")
    print(f"Out-of-sample cutoff (70% in-sample / 30% out-of-sample by time): {cutoff_ts.isoformat()}")

    results = {}
    for strategy in (CURRENT, CANDIDATE):
        trades = run_backtest(strategy, snapshots)
        results[strategy.name] = summarize_with_oos(strategy.name, trades, cutoff_ts)

    print("\n=== Verdict (full dataset, then out-of-sample only) ===")
    cur, cand = results[CURRENT.name], results[CANDIDATE.name]
    if cur and cand:
        print(f"CURRENT:   {cur['n']} trades, {cur['wins']}W/{cur['losses']}L, total PnL ${cur['total_pnl']:+.2f}, expectancy ${cur['expectancy']:+.3f}/trade")
        print(f"CANDIDATE: {cand['n']} trades, {cand['wins']}W/{cand['losses']}L, total PnL ${cand['total_pnl']:+.2f}, expectancy ${cand['expectancy']:+.3f}/trade")
        cur_oos, cand_oos = cur["out_of_sample"], cand["out_of_sample"]
        print(f"CURRENT   out-of-sample:   {cur_oos['n']} trades, expectancy ${cur_oos['expectancy']:+.3f}/trade")
        print(f"CANDIDATE out-of-sample:   {cand_oos['n']} trades, expectancy ${cand_oos['expectancy']:+.3f}/trade")


if __name__ == "__main__":
    main()
