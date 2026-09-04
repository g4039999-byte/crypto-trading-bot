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

2026-09-04: added an optional trailing-stop exit (Strategy.trailing_arm_pct/
trailing_stop_pct, both None by default = the exact original fixed-%
stop-loss/take-profit/max-holding behavior, unchanged) plus profit
factor / max drawdown / Sharpe / Sortino (via src.stocks.performance.
compute_metrics -- the same market-agnostic metrics function the stocks
side's walk-forward pipeline uses, operating on plain per-trade %
returns) and a walk-forward fold-stability score, mirrored from src/
stocks/research_pipeline.py's methodology: trades are bucketed
chronologically into N folds and a strategy only scores well if its edge
holds in MOST folds, not just in aggregate or in one lucky window.

Usage:
    python -m scripts.backtest_paper_strategy
"""

import hashlib
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
from src.stage import classify_stage  # noqa: E402
from src.stocks.performance import compute_metrics  # noqa: E402 -- market-agnostic; operates on plain pnl_pct floats

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
    stop_loss_pct: float  # hard floor -- always active, even once a trailing stop has armed (mirrors src.stocks.risk_engine.check_exit's precedence)
    take_profit_pct: float  # None = uncapped (rely on the trailing stop / max holding time to ever close a big winner)
    max_holding_minutes: float
    max_liq_drawdown_pct: float = None  # None = no liquidity-decline guard
    stop_loss_cooldown_minutes: float = 0  # 0 = no cooldown; otherwise, minutes to skip re-entry after a stop_loss on that token
    trailing_arm_pct: float = None  # None/0 = trailing (if enabled) is active from entry; otherwise only arms once price is this many % above entry
    trailing_stop_pct: float = None  # None = trailing stop disabled entirely (exact original behavior); otherwise trail this many % below the peak price seen since entry
    # --- Entry-momentum-quality controls (2026-09-04) -- all None/False
    # by default (exact original behavior, unchanged for CURRENT/
    # CANDIDATE). See scripts/diagnose_paper_strategy.py's finding: the
    # trend classification (STRONG/RISING/NEUTRAL, a short-term buy-flow
    # DELTA measure) is fold-consistently profitable for NEUTRAL and
    # fold-consistently unprofitable for STRONG/RISING -- these fields
    # let a candidate weight/gate/confirm RISING and STRONG differently
    # from simply excluding them via entry_trends, without touching
    # scoring.py/momentum.py's actual point formulas.
    trend_score_override: dict = None  # e.g. {"RISING": 55, "STRONG": 55} -- a trend key present here must ALSO clear this (higher) min_score, on top of the base min_score check; a "weight reduction" rather than a hard exclusion.
    max_velocity_pct_per_min: float = None  # None = no cap; otherwise reject entry if price has moved more than this many %/minute since first-seen (a proxy for "already pumped hard/fast -- buying the top" rather than early, sustainable momentum).
    require_trend_persistence: bool = False  # if True, a STRONG/RISING entry additionally requires the PREVIOUS evaluated point for this token to have ALSO been STRONG/RISING (momentum that has held for at least two consecutive checks, not a single-snapshot spike). NEUTRAL entries are never affected by this.
    # --- "Smart" velocity-cap qualifiers (2026-09-04, round 2) -- None =
    # max_velocity_pct_per_min (if set) applies unconditionally to every
    # entry, exactly as before. Setting any of these narrows WHEN the
    # cap applies, instead of a blanket cutoff -- e.g. only distrust a
    # fast mover when its trend is RISING specifically, or when its pool
    # is thin, or when volume is also unusually elevated for its size
    # (item 3's "الجمع بين velocity + liquidity + volume").
    velocity_cap_trends: tuple = None  # None = cap applies regardless of trend; otherwise only applies when evaluated["trend"] is one of these
    velocity_cap_max_liquidity_usd: float = None  # None = cap applies regardless of liquidity; otherwise only applies when liquidity is BELOW this (a thin pool moving fast is riskier than a deep one moving the same %)
    velocity_cap_min_relative_volume: float = None  # None = cap applies regardless of relative volume; otherwise only applies when relative_volume (volume/liquidity) is AT OR ABOVE this (a fast move on unusually heavy volume relative to its own pool, not just a fast move alone)
    # --- Velocity-spike cooldown (2026-09-04, round 2) -- a DIFFERENT
    # mechanism from max_velocity_pct_per_min's hard, permanent reject:
    # instead of turning down a fast-moving token forever, note the
    # moment it was seen moving faster than velocity_spike_threshold_
    # pct_per_min and simply refuse entry on that SAME token for the
    # next velocity_spike_cooldown_minutes -- "delay the entry a little
    # when the move looks extreme" (item 3) rather than skip it outright;
    # a token that cools off and still otherwise qualifies once the
    # cooldown lapses can still be entered. Mirrors stop_loss_cooldown_
    # minutes' existing per-token cooldown pattern exactly. Both fields
    # must be set together for this to do anything; independent of
    # max_velocity_pct_per_min/velocity_cap_* above (a strategy can use
    # either mechanism, both, or neither).
    velocity_spike_threshold_pct_per_min: float = None
    velocity_spike_cooldown_minutes: float = None
    # --- Round 3 (2026-09-04): cumulative buy_ratio (buys_24h/(buys_24h+
    # sells_24h) since the token's own first snapshot -- NOT the same
    # measure as trend's short-term flow delta) was the other
    # fold-consistent signal scripts/diagnose_paper_strategy.py found
    # (deliberately deferred at the time: "will test independently").
    # None = no floor (original behavior).
    min_buy_ratio: float = None


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
    peak_price: float = None  # highest price seen since entry -- what a trailing stop trails against
    trailing_stop_price: float = None  # the trailing stop's current level, once armed (None until then, or if trailing is disabled)
    # Extra entry-time context, purely for diagnostic breakdown (scripts/
    # diagnose_paper_strategy.py) -- never read by replay_token/_check_exit
    # itself, so adding these cannot change any backtest result.
    entry_base_score: int = None
    entry_momentum_score: int = None
    entry_liquidity: float = None
    entry_volume: float = None
    entry_buy_ratio: float = None
    entry_price_change_pct: float = None
    entry_velocity_pct_per_min: float = None
    entry_relative_volume: float = None
    entry_stage: str = None
    entry_liq_drawdown_pct: float = None
    discovery_to_entry_seconds: float = None  # alias of seconds_since_first_seen, kept for naming parity with the real system's paper_trader.py field


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

    buys = current.get("buys_24h") or 0
    sells = current.get("sells_24h") or 0
    volume = current.get("volume_24h") or 0
    # "Speed of movement": % price change since first-seen per minute of
    # age -- a token up 20% over 10 minutes and one up 20% over 200
    # minutes score identically on price_change_pct alone; this
    # separates them, for the diagnostic breakdown in
    # scripts/diagnose_paper_strategy.py (item 4's "سرعة الحركة").
    velocity_pct_per_min = (price_change_pct / age_minutes) if age_minutes else 0.0
    relative_volume = (volume / liquidity) if liquidity else 0.0

    return {
        "score": score,
        "base_score": base_score,
        "momentum_score": momentum_score,
        "trend": trend,
        "liquidity": current.get("liquidity_usd"),
        "volume": current.get("volume_24h"),
        "age": age_minutes,
        "stage": classify_stage(age_minutes),
        "buys": buys,
        "sells": sells,
        "buy_ratio": (buys / (buys + sells)) if (buys + sells) else None,
        "price_usd": price,
        "price_change_pct": price_change_pct,
        "velocity_pct_per_min": velocity_pct_per_min,
        "relative_volume": relative_volume,
        "liq_drawdown_pct": liq_drawdown_pct,
    }


_ELEVATED_TRENDS = ("STRONG", "RISING")


def _velocity_cap_applies(evaluated, strategy):
    """Whether strategy.max_velocity_pct_per_min's cap should even be
    checked for this candidate -- narrowed by velocity_cap_trends/
    velocity_cap_max_liquidity_usd/velocity_cap_min_relative_volume when
    those are set (see Strategy's docstring comments). With none of
    those three set, this is unconditionally True -- exactly the
    original blanket-cap behavior.
    """
    if strategy.velocity_cap_trends is not None and evaluated["trend"] not in strategy.velocity_cap_trends:
        return False
    if strategy.velocity_cap_max_liquidity_usd is not None and evaluated["liquidity"] >= strategy.velocity_cap_max_liquidity_usd:
        return False
    if strategy.velocity_cap_min_relative_volume is not None and evaluated["relative_volume"] < strategy.velocity_cap_min_relative_volume:
        return False
    return True


def _passes_entry(evaluated, strategy, previous_evaluated=None):
    if evaluated["score"] < strategy.min_score:
        return False
    if evaluated["trend"] not in strategy.entry_trends:
        return False
    if strategy.trend_score_override and evaluated["trend"] in strategy.trend_score_override:
        if evaluated["score"] < strategy.trend_score_override[evaluated["trend"]]:
            return False
    if strategy.max_velocity_pct_per_min is not None and _velocity_cap_applies(evaluated, strategy):
        if evaluated["velocity_pct_per_min"] > strategy.max_velocity_pct_per_min:
            return False
    if strategy.require_trend_persistence and evaluated["trend"] in _ELEVATED_TRENDS:
        if previous_evaluated is None or previous_evaluated["trend"] not in _ELEVATED_TRENDS:
            return False
    if strategy.min_buy_ratio is not None:
        if evaluated["buy_ratio"] is None or evaluated["buy_ratio"] < strategy.min_buy_ratio:
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


def _update_trailing_stop(strategy, entry_price, peak_price, existing_trailing_stop):
    """Returns the (possibly updated) trailing_stop_price for a position
    given the peak price seen since entry -- mirrors
    src.stocks.risk_engine.update_trailing_stop's exact semantics: the
    trail only "arms" (starts being tracked at all) once the peak has
    risen strategy.trailing_arm_pct% above entry (0/None = armed
    immediately from entry), and then only ever moves UP, never down,
    same as a normal ratcheting trailing stop. Returns None if
    strategy.trailing_stop_pct is None (trailing disabled entirely) or
    the position hasn't armed yet.

    Approximation, disclosed like every other one in this file: this
    only sees one price per snapshot (no intra-cycle high), so the
    "peak" is the highest price OBSERVED at a snapshot, not the true
    highest price ever touched between snapshots -- same granularity
    limit every exit rule in this backtest already has.
    """
    if strategy.trailing_stop_pct is None:
        return None
    arm_level = entry_price * (1 + (strategy.trailing_arm_pct or 0.0) / 100)
    if peak_price < arm_level:
        return existing_trailing_stop  # not armed yet (usually None)
    candidate = peak_price * (1 - strategy.trailing_stop_pct / 100)
    if existing_trailing_stop is None:
        return candidate
    return max(existing_trailing_stop, candidate)


def _check_exit(entry_price, entry_ts, current_price, current_ts, strategy, trailing_stop_price=None):
    """Precedence mirrors src.stocks.risk_engine.check_exit exactly:
    hard stop-loss first (always active, even once a trailing stop has
    armed -- a trailing stop can only ever be tighter than or equal to
    letting the hard stop be the worst case, never looser), then the
    trailing stop (if armed), then take-profit (skipped entirely if
    strategy.take_profit_pct is None -- an uncapped design relying on
    the trailing stop/max-holding-time to ever close a big winner), then
    max-holding-time.
    """
    if current_price <= entry_price * (1 - strategy.stop_loss_pct / 100):
        return "stop_loss"
    if trailing_stop_price is not None and current_price <= trailing_stop_price:
        return "trailing_stop"
    if strategy.take_profit_pct is not None and current_price >= entry_price * (1 + strategy.take_profit_pct / 100):
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
    velocity_cooldown_until = None  # datetime | None -- rolling cooldown re-armed every time this token is seen moving faster than velocity_spike_threshold_pct_per_min (see Strategy's docstring)

    for idx in range(1, len(history)):
        current = history[idx]
        ts = _parse_ts(current["timestamp"])
        price = current.get("price_usd")

        if in_position is not None:
            in_position.peak_price = max(in_position.peak_price, price)
            in_position.trailing_stop_price = _update_trailing_stop(
                strategy, in_position.entry_price, in_position.peak_price, in_position.trailing_stop_price,
            )
            reason = _check_exit(
                in_position.entry_price, in_position.entry_ts, price, ts, strategy,
                trailing_stop_price=in_position.trailing_stop_price,
            )
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

        if strategy.velocity_spike_cooldown_minutes and strategy.velocity_spike_threshold_pct_per_min is not None:
            if evaluated["velocity_pct_per_min"] > strategy.velocity_spike_threshold_pct_per_min:
                velocity_cooldown_until = ts + timedelta(minutes=strategy.velocity_spike_cooldown_minutes)
            if velocity_cooldown_until is not None and ts < velocity_cooldown_until:
                continue

        previous_evaluated = _evaluate_point(history, idx - 1) if (strategy.require_trend_persistence and idx >= 2) else None
        if _passes_entry(evaluated, strategy, previous_evaluated=previous_evaluated):
            discovery_to_entry_s = (ts - _parse_ts(history[0]["timestamp"])).total_seconds()
            in_position = Trade(
                token=token, entry_idx=idx, entry_ts=ts, entry_price=price,
                entry_score=evaluated["score"], entry_trend=evaluated["trend"],
                entry_age_minutes=evaluated["age"],
                seconds_since_first_seen=discovery_to_entry_s,
                peak_price=price,
                entry_base_score=evaluated["base_score"], entry_momentum_score=evaluated["momentum_score"],
                entry_liquidity=evaluated["liquidity"], entry_volume=evaluated["volume"],
                entry_buy_ratio=evaluated["buy_ratio"], entry_price_change_pct=evaluated["price_change_pct"],
                entry_velocity_pct_per_min=evaluated["velocity_pct_per_min"],
                entry_relative_volume=evaluated["relative_volume"], entry_stage=evaluated["stage"],
                entry_liq_drawdown_pct=evaluated["liq_drawdown_pct"],
                discovery_to_entry_seconds=discovery_to_entry_s,
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
        empty = {
            "n": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0,
        }
        empty.update(compute_metrics([]))
        return empty

    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    total_pnl = sum(t.pnl_usd for t in trades)
    avg_win = statistics.mean(t.pnl_usd for t in wins) if wins else 0.0
    avg_loss = statistics.mean(t.pnl_usd for t in losses) if losses else 0.0
    expectancy = total_pnl / n  # avg $ PnL per trade -- comparable across strategies with different trade counts, unlike raw total_pnl

    # Profit factor / max drawdown / Sharpe / Sortino need a chronological
    # order (drawdown especially -- it is a running-sum peak-to-trough
    # measure) -- sort by exit time, not whatever order replay_token
    # happened to append trades in across different tokens.
    chronological = sorted(trades, key=lambda t: t.exit_ts)
    metrics = compute_metrics([t.pnl_pct for t in chronological])

    reason_counts = {}
    for t in trades:
        reason_counts[t.reason] = reason_counts.get(t.reason, 0) + 1

    if verbose:
        avg_age = statistics.mean(t.entry_age_minutes for t in trades)
        avg_discovery_to_entry_s = statistics.mean(t.seconds_since_first_seen for t in trades)
        print(f"wins: {len(wins)} | losses: {len(losses)} | win rate: {100*len(wins)/n:.1f}%")
        print(f"total PnL: ${total_pnl:+.2f} | expectancy: ${expectancy:+.3f}/trade | avg win: ${avg_win:+.2f} | avg loss: ${avg_loss:+.2f}")
        print(f"profit factor: {metrics['profit_factor']} | max drawdown: {metrics['max_drawdown_pct']}pp | Sharpe: {metrics['sharpe']} | Sortino: {metrics['sortino']}")
        print(f"avg age at entry: {avg_age:.1f}m | avg time from first-seen to entry: {avg_discovery_to_entry_s:.0f}s")
        print(f"exit reasons: {reason_counts}")
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "total_pnl": total_pnl,
        "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": expectancy,
        **metrics,  # trade_count, win_rate_pct, total_return_pct, profit_factor, expectancy_pct, sharpe, sortino, max_drawdown_pct
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


def compute_fold_boundaries(snapshots, n_folds):
    """n_folds-1 cutoff timestamps splitting the dataset into n_folds
    roughly equal-COUNT (by snapshot density, see compute_oos_cutoff's
    docstring for why not equal-calendar-time) chronological buckets.
    """
    all_ts = sorted(_all_snapshot_timestamps(snapshots))
    if not all_ts:
        raise ValueError("cannot compute fold boundaries from an empty dataset")
    return [all_ts[min(len(all_ts) - 1, int(len(all_ts) * i / n_folds))] for i in range(1, n_folds)]


def assign_fold_index(entry_ts, fold_boundaries):
    for i, boundary in enumerate(fold_boundaries):
        if entry_ts < boundary:
            return i
    return len(fold_boundaries)


def fold_stability_score(trades, fold_boundaries):
    """Fraction of ALL folds (not just the ones with trades in them)
    where this strategy had a positive expectancy AND profit_factor > 1
    -- mirrors src.stocks.research_pipeline._fold_stability_score
    exactly (same formula, same reasoning: a strategy that only "wins"
    in one lucky fold out of several, even with great aggregate numbers,
    is exactly the overfitting/one-off pattern this is meant to catch).
    """
    n_folds = len(fold_boundaries) + 1
    by_fold = {i: [] for i in range(n_folds)}
    for t in trades:
        by_fold[assign_fold_index(t.entry_ts, fold_boundaries)].append(t.pnl_pct)

    passing = 0
    for pnls in by_fold.values():
        if not pnls:
            continue
        m = compute_metrics(pnls)
        pf = m["profit_factor"]
        pf_value = 1e9 if pf == float("inf") else (pf or 0.0)
        if (m["expectancy_pct"] or 0) > 0 and pf_value > 1:
            passing += 1
    return round(passing / n_folds, 2)


def split_tokens_into_groups(snapshots, n_groups=2):
    """Deterministically partition the token universe into n_groups
    disjoint groups (by address hash, stable across runs -- via
    hashlib.md5, NOT Python's builtin hash(), which is randomized per
    process by default and would silently put a different set of tokens
    in each group every run) -- used to check whether a candidate's edge
    holds across more than one arbitrary slice of the coin universe, not
    just the one combined backtest (item 8's "more than one coin group"
    requirement). Not a replacement for the time-based fold/OOS split
    above -- a different axis of the same overfitting question.
    """
    groups = [{} for _ in range(n_groups)]
    for address, history in snapshots.items():
        digest = hashlib.md5(address.encode("utf-8")).hexdigest()
        groups[int(digest, 16) % n_groups][address] = history
    return groups


CURRENT = Strategy(
    name="CURRENT (deployed)",
    min_score=45, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=5, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=50, max_holding_minutes=240,
    max_liq_drawdown_pct=None, stop_loss_cooldown_minutes=0,
)

# The entry/exit rule set BEFORE the 2026-09-04 elevated-trend-score
# change below -- kept as its own object so every single-mechanism
# ENTRY_FILTER_VARIANT (via _candidate_variant()) is tested against this
# unchanged baseline, never accidentally stacked on top of a later
# decision. CANDIDATE itself (below) is what's actually deployed.
_PRE_ELEVATED_TREND_GATE_BASE = Strategy(
    name="pre-2026-09-04 baseline (not directly used as a comparison target)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=25, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
)

# CANDIDATE's rules after round 1 (ENTRY_SCORE_PENALTY_55 adopted) but
# BEFORE round 2 (the velocity-spike cooldown below) -- kept as its own
# object, same reasoning as _PRE_ELEVATED_TREND_GATE_BASE above, so
# round-2's RISING_VELOCITY_VARIANTS (via _current_candidate_variant())
# are tested against round 1's baseline, never accidentally stacked on
# top of round 2's own already-adopted change.
_PRE_VELOCITY_SPIKE_GATE_BASE = Strategy(
    name="pre-round-2 baseline (not directly used as a comparison target)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=25, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trend_score_override={"RISING": 55, "STRONG": 55},
)

CANDIDATE = Strategy(
    name="CANDIDATE (deployed, matches src/config.py's PAPER_* defaults)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=25, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    # Round 1 (2026-09-04): adopted after this exact comparison
    # qualified on out-of-sample/fold-stability/drawdown evidence -- see
    # PAPER_ELEVATED_TREND_MIN_SCORE's docstring in src/config.py for the
    # full evidence trail. Matches ENTRY_SCORE_PENALTY_55 below, kept as
    # a separate named variant (built off _PRE_ELEVATED_TREND_GATE_BASE,
    # not this CANDIDATE) so a future re-run still shows the same
    # apples-to-apples comparison this decision was based on.
    trend_score_override={"RISING": 55, "STRONG": 55},
    # Round 2 (2026-09-04): adopted after ENTRY_VELOCITY_SPIKE_COOLDOWN
    # was the only round-2 variant whose improvement held under every
    # robustness check (coin-group split, two OOS cutoffs, every
    # walk-forward fold count) -- see PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_
    # PER_MIN's docstring in src/config.py for the full evidence trail.
    # Matches ENTRY_VELOCITY_SPIKE_COOLDOWN below, kept as a separate
    # named variant (built off _PRE_VELOCITY_SPIKE_GATE_BASE, not this
    # CANDIDATE) for the same reason as round 1's split above.
    velocity_spike_threshold_pct_per_min=5.0, velocity_spike_cooldown_minutes=30,
)

# --- Trailing-stop variants (2026-09-04) -- all built on top of
# CANDIDATE's already-validated entry rules (min_score/age/liquidity-
# drawdown-guard/cooldown), varying ONLY the exit mechanism, so any
# difference in results reflects the exit-rule change specifically. See
# this file's module docstring for the precedence trailing_stop_pct
# introduces. Hard stop_loss_pct=25 (CANDIDATE's own validated floor) is
# kept in every variant -- a trailing stop only ever tightens the worst
# case, never loosens it.
TRAIL_IMMEDIATE = Strategy(
    name="TRAIL_IMMEDIATE (trail 15% from entry, no arm delay, uncapped)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=None, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trailing_arm_pct=0, trailing_stop_pct=15,
)

TRAIL_ARMED_TIGHT = Strategy(
    name="TRAIL_ARMED_TIGHT (arm at +15%, trail 10%, uncapped)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=None, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trailing_arm_pct=15, trailing_stop_pct=10,
)

TRAIL_ARMED_WIDE = Strategy(
    name="TRAIL_ARMED_WIDE (arm at +15%, trail 15%, uncapped)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=None, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trailing_arm_pct=15, trailing_stop_pct=15,
)

TRAIL_ARMED_LATE = Strategy(
    name="TRAIL_ARMED_LATE (arm at +25%, trail 12%, uncapped)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=None, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trailing_arm_pct=25, trailing_stop_pct=12,
)

TRAIL_CAPPED = Strategy(
    name="TRAIL_CAPPED (arm at +15%, trail 10%, capped at +150% take-profit)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=150, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trailing_arm_pct=15, trailing_stop_pct=10,
)

TRAILING_VARIANTS = (TRAIL_IMMEDIATE, TRAIL_ARMED_TIGHT, TRAIL_ARMED_WIDE, TRAIL_ARMED_LATE, TRAIL_CAPPED)


def _trail_on_candidate(name, **overrides):
    """A trailing-stop variant built from the CURRENTLY DEPLOYED
    CANDIDATE (round 1 + round 2's adopted entry rules), not the
    original pre-round-1 population TRAILING_VARIANTS above was tested
    against. Safe to build directly from CANDIDATE (no risk of
    comparing a winner against itself, unlike _candidate_variant/
    _current_candidate_variant/_round3_candidate_variant above) --
    CANDIDATE's own trailing_arm_pct/trailing_stop_pct are both None
    (trailing has never been adopted), so these overrides only ever add
    a new exit mechanic on top, never silently double up on one already
    baked into CANDIDATE itself.
    """
    from dataclasses import replace
    return replace(CANDIDATE, name=name, **overrides)


# Round 4 (2026-09-04): re-test trailing stops now that entry quality is
# measurably different (round 1+2 roughly halved trade volume and
# lifted full-dataset profit_factor above 1.0) -- the ORIGINAL rejection
# above was against a much noisier, unfiltered entry population; a
# genuinely different population is a legitimate reason to re-check a
# previously-rejected idea, not overfitting.
TRAIL_V2_ARMED_TIGHT = _trail_on_candidate(
    "TRAIL_V2_ARMED_TIGHT (on the round-1+2 CANDIDATE: arm at +15%, trail 10%, uncapped)",
    take_profit_pct=None, trailing_arm_pct=15, trailing_stop_pct=10,
)
TRAIL_V2_ARMED_WIDE = _trail_on_candidate(
    "TRAIL_V2_ARMED_WIDE (on the round-1+2 CANDIDATE: arm at +15%, trail 15%, uncapped)",
    take_profit_pct=None, trailing_arm_pct=15, trailing_stop_pct=15,
)
TRAIL_V2_CAPPED = _trail_on_candidate(
    "TRAIL_V2_CAPPED (on the round-1+2 CANDIDATE: arm at +15%, trail 10%, capped at +150%)",
    take_profit_pct=150, trailing_arm_pct=15, trailing_stop_pct=10,
)
TRAILING_VARIANTS_V2 = (TRAIL_V2_ARMED_TIGHT, TRAIL_V2_ARMED_WIDE, TRAIL_V2_CAPPED)


# Round 5 (2026-09-04): structural scoring/trend-gate recalibration.
# WEAK (src.observation.compute_trend's 4th classification -- price
# down >10% OR recent buy-flow <40% -- currently excluded from
# PAPER_ENTRY_TRENDS entirely) showed a striking result in an
# unrestricted (min_score=0, every trend) broad scan of 139 trades:
# win=58.6%/PF=1.48/expectancy=+7.55%, the best of any trend bucket
# including NEUTRAL. At the DEPLOYED min_score=40 floor specifically,
# that edge did not reproduce (WEAK_ONLY here underperforms CANDIDATE) --
# the broad-scan finding is likely driven by low-score WEAK candidates
# specifically, not WEAK in general. Tested anyway at the deployed score
# floor since it's cheap to check: modest full-dataset improvement,
# out-of-sample too thin (n=2-3) to trust. Not adopted -- flagged as the
# clearest lead for a future round once more live paper-trading data
# has accumulated (would need a low-score-WEAK-specific test, which
# needs a bigger dataset than exists right now to hold out an OOS split
# for it credibly).
ENTRY_ADD_WEAK = _trail_on_candidate("ENTRY_ADD_WEAK (allow WEAK trend alongside STRONG/RISING/NEUTRAL)", entry_trends=("STRONG", "RISING", "NEUTRAL", "WEAK"))
ENTRY_NEUTRAL_WEAK_ONLY = _trail_on_candidate("ENTRY_NEUTRAL_WEAK_ONLY (drop STRONG/RISING entirely, keep only NEUTRAL+WEAK)", entry_trends=("NEUTRAL", "WEAK"))
ENTRY_WEAK_ONLY = _trail_on_candidate("ENTRY_WEAK_ONLY (WEAK trend exclusively)", entry_trends=("WEAK",))
WEAK_TREND_VARIANTS = (ENTRY_ADD_WEAK, ENTRY_NEUTRAL_WEAK_ONLY, ENTRY_WEAK_ONLY)


def broad_scan_strategy(base_strategy=None):
    """A maximally-permissive Strategy (min_score=0, every trend
    including WEAK) for correlation/tercile analysis with real
    statistical power -- CANDIDATE's own score/trend gates already
    exclude most of the population, which is exactly why per-feature
    correlation analysis run only on CANDIDATE's own (thin) trade set
    is underpowered. Keeps every RISK protection (SL/TP/cooldown/
    liquidity-drawdown-guard/liquidity/volume/age floors) unchanged --
    only the score/trend SELECTION gate is removed, so the resulting
    trades are still realistic "would-have-been-taken-if-allowed-
    through" trades, not arbitrary noise. base_strategy defaults to
    CANDIDATE (keeps its SL/TP/cooldown/etc); pass a different Strategy
    to broaden a different rule set the same way.
    """
    from dataclasses import replace
    base_strategy = base_strategy or CANDIDATE
    return replace(
        base_strategy, name="broad-scan (min_score=0, every trend)",
        min_score=0, entry_trends=("STRONG", "RISING", "NEUTRAL", "WEAK"),
        trend_score_override=None, max_velocity_pct_per_min=None,
        velocity_spike_threshold_pct_per_min=None, velocity_spike_cooldown_minutes=None,
        require_trend_persistence=False, min_buy_ratio=None,
    )


def _candidate_variant(name, **overrides):
    """A Strategy identical to _PRE_ELEVATED_TREND_GATE_BASE (CANDIDATE's
    rules BEFORE the 2026-09-04 elevated-trend-score change) except for
    the given overrides -- deliberately NOT built from CANDIDATE itself,
    so a variant testing one mechanism (e.g. a velocity cap) is never
    silently stacked on top of a different, already-adopted mechanism
    (the trend_score_override CANDIDATE now carries). Every entry-filter
    variant below changes ONLY the momentum-quality mechanism being
    tested, never SL/TP/cooldown/liquidity-drawdown-guard/min_score/
    min_liquidity/min_volume/age window, per this session's explicit
    scope.
    """
    from dataclasses import replace
    return replace(_PRE_ELEVATED_TREND_GATE_BASE, name=name, **overrides)


# --- Entry-momentum-quality variants (2026-09-04) -- testing WHY the
# trend-classification gate (entry_trends) lets in RISING/STRONG
# candidates that scripts/diagnose_paper_strategy.py found are
# fold-consistently unprofitable, via four DIFFERENT mechanisms (not a
# blind numeric grid over one mechanism): full exclusion, partial
# exclusion, a stricter score bar for the elevated trends ("weight
# reduction"), an additional persistence confirmation, and a velocity
# cap. All built on CANDIDATE's unchanged SL/TP/cooldown/liquidity-guard/
# min_score/liquidity/volume/age settings.
ENTRY_EXCLUDE_ALL_ELEVATED = _candidate_variant(
    "ENTRY_EXCLUDE_ALL_ELEVATED (NEUTRAL only, drop RISING+STRONG entirely)",
    entry_trends=("NEUTRAL",),
)

ENTRY_EXCLUDE_STRONG_ONLY = _candidate_variant(
    "ENTRY_EXCLUDE_STRONG_ONLY (drop STRONG, keep RISING+NEUTRAL)",
    entry_trends=("RISING", "NEUTRAL"),
)

ENTRY_EXCLUDE_RISING_ONLY = _candidate_variant(
    "ENTRY_EXCLUDE_RISING_ONLY (drop RISING, keep STRONG+NEUTRAL)",
    entry_trends=("STRONG", "NEUTRAL"),
)

ENTRY_SCORE_PENALTY_55 = _candidate_variant(
    "ENTRY_SCORE_PENALTY_55 (RISING/STRONG need score>=55, NEUTRAL keeps 40)",
    trend_score_override={"RISING": 55, "STRONG": 55},
)

ENTRY_SCORE_PENALTY_60 = _candidate_variant(
    "ENTRY_SCORE_PENALTY_60 (RISING/STRONG need score>=60, NEUTRAL keeps 40)",
    trend_score_override={"RISING": 60, "STRONG": 60},
)

ENTRY_REQUIRE_PERSISTENCE = _candidate_variant(
    "ENTRY_REQUIRE_PERSISTENCE (RISING/STRONG must have also been RISING/STRONG one snapshot earlier)",
    require_trend_persistence=True,
)

ENTRY_VELOCITY_CAP_3 = _candidate_variant(
    "ENTRY_VELOCITY_CAP_3 (reject any entry moving >3%/min since first-seen)",
    max_velocity_pct_per_min=3.0,
)

ENTRY_VELOCITY_CAP_5 = _candidate_variant(
    "ENTRY_VELOCITY_CAP_5 (reject any entry moving >5%/min since first-seen)",
    max_velocity_pct_per_min=5.0,
)

ENTRY_FILTER_VARIANTS = (
    ENTRY_EXCLUDE_ALL_ELEVATED, ENTRY_EXCLUDE_STRONG_ONLY, ENTRY_EXCLUDE_RISING_ONLY,
    ENTRY_SCORE_PENALTY_55, ENTRY_SCORE_PENALTY_60, ENTRY_REQUIRE_PERSISTENCE,
    ENTRY_VELOCITY_CAP_3, ENTRY_VELOCITY_CAP_5,
)


def _current_candidate_variant(name, **overrides):
    """A Strategy identical to _PRE_VELOCITY_SPIKE_GATE_BASE (round 1's
    adopted rules -- ENTRY_SCORE_PENALTY_55's trend_score_override --
    but BEFORE round 2's own adopted change) except for the given
    overrides. Deliberately NOT built from CANDIDATE itself, which now
    already includes round 2's velocity-spike cooldown -- see
    _PRE_ELEVATED_TREND_GATE_BASE's docstring above for why (same
    reasoning, one round later). Round-2 (2026-09-04) RISING/high-
    velocity variants below are layered on top of round 1's adopted
    change, never replacing it -- ENTRY_SCORE_PENALTY_55 itself is not
    touched by anything below.
    """
    from dataclasses import replace
    return replace(_PRE_VELOCITY_SPIKE_GATE_BASE, name=name, **overrides)


# --- Round 2 (2026-09-04): CANDIDATE now includes ENTRY_SCORE_PENALTY_55
# (RISING/STRONG need score>=55), but scripts/diagnose_paper_strategy.py
# and round 1's own sub-metric reporting showed RISING itself barely
# improved even at the higher score bar, and high-"velocity" (already-
# fast-moved) entries stayed weak across every round-1 variant. These
# five variants each test a genuinely different way to distinguish
# healthy, sustainable momentum from a late/extreme spike, all layered
# on top of the already-adopted change (never replacing it):
ENTRY_VELOCITY_CAP_RISING_ONLY = _current_candidate_variant(
    "ENTRY_VELOCITY_CAP_RISING_ONLY (velocity cap >4%/min applies to RISING only)",
    max_velocity_pct_per_min=4.0, velocity_cap_trends=("RISING",),
)

ENTRY_VELOCITY_CAP_THIN_LIQUIDITY = _current_candidate_variant(
    "ENTRY_VELOCITY_CAP_THIN_LIQUIDITY (velocity cap >4%/min applies only when liquidity<$20k)",
    max_velocity_pct_per_min=4.0, velocity_cap_max_liquidity_usd=20000,
)

ENTRY_VELOCITY_CAP_HIGH_RELVOL = _current_candidate_variant(
    "ENTRY_VELOCITY_CAP_HIGH_RELVOL (velocity cap >4%/min applies only when relative_volume>=15)",
    max_velocity_pct_per_min=4.0, velocity_cap_min_relative_volume=15.0,
)

ENTRY_VELOCITY_SPIKE_COOLDOWN = _current_candidate_variant(
    "ENTRY_VELOCITY_SPIKE_COOLDOWN (seeing >5%/min triggers a 30-minute per-token entry delay, not a permanent reject)",
    velocity_spike_threshold_pct_per_min=5.0, velocity_spike_cooldown_minutes=30,
)

ENTRY_RISING_PERSISTENCE = _current_candidate_variant(
    "ENTRY_RISING_PERSISTENCE (RISING/STRONG must have also been elevated one snapshot earlier, re-tested on the new baseline)",
    require_trend_persistence=True,
)

RISING_VELOCITY_VARIANTS = (
    ENTRY_VELOCITY_CAP_RISING_ONLY, ENTRY_VELOCITY_CAP_THIN_LIQUIDITY, ENTRY_VELOCITY_CAP_HIGH_RELVOL,
    ENTRY_VELOCITY_SPIKE_COOLDOWN, ENTRY_RISING_PERSISTENCE,
)


# Round 2's fully-adopted rules (ENTRY_SCORE_PENALTY_55 +
# ENTRY_VELOCITY_SPIKE_COOLDOWN) -- round 3's own baseline, captured
# BEFORE round 3's own change so its variants (via
# _round3_candidate_variant()) are tested against it, not against
# whatever round 3 itself ends up adopting.
_PRE_BUY_RATIO_GATE_BASE = Strategy(
    name="pre-round-3 baseline (not directly used as a comparison target)",
    min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
    min_liquidity_usd=5000, min_volume_24h_usd=25000,
    min_age_minutes=15, max_age_minutes=180,
    stop_loss_pct=25, take_profit_pct=25, max_holding_minutes=240,
    max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    trend_score_override={"RISING": 55, "STRONG": 55},
    velocity_spike_threshold_pct_per_min=5.0, velocity_spike_cooldown_minutes=30,
)


def _round3_candidate_variant(name, **overrides):
    from dataclasses import replace
    return replace(_PRE_BUY_RATIO_GATE_BASE, name=name, **overrides)


# --- Round 3 (2026-09-04): the OTHER fold-consistent signal
# scripts/diagnose_paper_strategy.py found (buy_ratio -- cumulative
# buys_24h/(buys_24h+sells_24h) since first-seen, a DIFFERENT, longer-
# window measure than trend's short-term flow delta), deliberately
# deferred at the time ("will test independently"). Three floors tested.
ENTRY_BUY_RATIO_55 = _round3_candidate_variant("ENTRY_BUY_RATIO_55 (buy_ratio>=0.55 required for every entry)", min_buy_ratio=0.55)
ENTRY_BUY_RATIO_60 = _round3_candidate_variant("ENTRY_BUY_RATIO_60 (buy_ratio>=0.60 required for every entry)", min_buy_ratio=0.60)
ENTRY_BUY_RATIO_65 = _round3_candidate_variant("ENTRY_BUY_RATIO_65 (buy_ratio>=0.65 required for every entry)", min_buy_ratio=0.65)

BUY_RATIO_VARIANTS = (ENTRY_BUY_RATIO_55, ENTRY_BUY_RATIO_60, ENTRY_BUY_RATIO_65)


N_FOLDS = 4  # walk-forward folds -- kept smaller than the stocks side's 5 given ~4.6 days of data vs stocks' 10 years; see fold_stability_score's docstring
MIN_TRADES_FOR_SIGNIFICANCE = 30  # a strategy with fewer trades than this in a bucket is not judged on that bucket


def _row(label, s):
    """Formats one summary dict for display. Every field guarded against
    None -- an empty bucket (0 trades, e.g. a variant's out-of-sample
    slice with nothing in it) is a completely normal, expected outcome
    here, not a reason to crash the whole comparison run.
    """
    pf = s["profit_factor"]
    pf_str = "inf" if pf == float("inf") else ("n/a" if pf is None else f"{pf:.2f}")
    dd = s["max_drawdown_pct"]
    dd_str = "n/a" if dd is None else f"{dd:.1f}pp"
    exp_pct = s["expectancy_pct"]
    exp_pct_str = "n/a" if exp_pct is None else f"{exp_pct:+.2f}%"
    return f"{label:<12} n={s['n']:>4}  PF={pf_str:>5}  maxDD={dd_str:>8}  expectancy=${s['expectancy']:+.3f}/trade ({exp_pct_str})"


def full_report(strategy, snapshots, cutoff_ts, fold_boundaries, verbose=False):
    trades = run_backtest(strategy, snapshots)
    summary = summarize_with_oos(strategy.name, trades, cutoff_ts, verbose=verbose)
    summary["fold_stability"] = fold_stability_score(trades, fold_boundaries)
    summary["_trades"] = trades  # kept for sub-metric breakdowns below; never printed directly
    return summary


def qualifies(report, baseline):
    """Shared bar for adopting ANY variant (trailing-stop or entry-
    filter): must beat the baseline on out-of-sample expectancy, have an
    out-of-sample sample large enough to say anything at all, match or
    beat the baseline's fold_stability, and not blow out max drawdown --
    never a decision made on in-sample numbers alone. Returns a list of
    failure reasons (empty = qualifies).
    """
    oos = report["out_of_sample"]
    reasons_failed = []
    if oos["n"] < max(5, MIN_TRADES_FOR_SIGNIFICANCE // 4):
        reasons_failed.append(f"out-of-sample trade count too small ({oos['n']})")
    if oos["expectancy"] <= baseline["out_of_sample"]["expectancy"]:
        reasons_failed.append("out-of-sample expectancy does not beat the baseline")
    if report["fold_stability"] < baseline["fold_stability"]:
        reasons_failed.append(f"fold_stability {report['fold_stability']} < baseline's {baseline['fold_stability']}")
    if (report["max_drawdown_pct"] or 0) > (baseline["max_drawdown_pct"] or 0) * 1.5:
        reasons_failed.append("max drawdown meaningfully worse than the baseline")
    return reasons_failed


def sub_metric_row(label, trades, predicate):
    subset = [t for t in trades if predicate(t)]
    m = compute_metrics([t.pnl_pct for t in subset])
    pf = "inf" if m["profit_factor"] == float("inf") else ("n/a" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}")
    win = "n/a" if m["win_rate_pct"] is None else f"{m['win_rate_pct']}%"
    exp = "n/a" if m["expectancy_pct"] is None else f"{m['expectancy_pct']:+.2f}%"
    return f"    {label:<16} n={m['trade_count']:>4}  win_rate={win:>6}  PF={pf:>5}  expectancy={exp:>8}"


def compare_group(group_label, baseline, variants, snapshots, cutoff_ts, fold_boundaries, *, report_rising_strong_velocity=False):
    """Runs, reports, and applies qualifies() to one named group of
    variants against `baseline`. Returns the sorted list of qualifying
    (strategy, report) pairs, best first (empty if none qualify).
    """
    print(f"\n\n######## {group_label} ########")
    baseline_report = full_report(baseline, snapshots, cutoff_ts, fold_boundaries)
    print(f"Baseline ({baseline.name}): {_row('', baseline_report)}  fold_stability={baseline_report['fold_stability']}")

    qualifying = []
    for s in variants:
        r = full_report(s, snapshots, cutoff_ts, fold_boundaries)
        print(f"\n{s.name}")
        print("  " + _row("full", r))
        print("  " + _row("in-sample", r["in_sample"]))
        print("  " + _row("out-of-sample", r["out_of_sample"]))
        print(f"  fold_stability={r['fold_stability']}")

        if report_rising_strong_velocity:
            trades = r["_trades"]
            print(sub_metric_row("RISING trend", trades, lambda t: t.entry_trend == "RISING"))
            print(sub_metric_row("STRONG trend", trades, lambda t: t.entry_trend == "STRONG"))
            print(sub_metric_row("NEUTRAL trend", trades, lambda t: t.entry_trend == "NEUTRAL"))
            velocities = sorted(t.entry_velocity_pct_per_min for t in trades if t.entry_velocity_pct_per_min is not None)
            if len(velocities) >= 6:
                high_cut = velocities[2 * len(velocities) // 3]
                print(sub_metric_row("high-velocity third", trades, lambda t, c=high_cut: (t.entry_velocity_pct_per_min or 0) >= c))

        reasons_failed = qualifies(r, baseline_report)
        status = "QUALIFIES" if not reasons_failed else "does not qualify: " + "; ".join(reasons_failed)
        print(f"  -> {status}")
        if not reasons_failed:
            qualifying.append((s, r))

    qualifying.sort(key=lambda sr: (sr[1]["fold_stability"], sr[1]["out_of_sample"]["expectancy"]), reverse=True)
    if not qualifying:
        print(f"\nNo {group_label} variant beat CANDIDATE convincingly on out-of-sample evidence. Keeping CANDIDATE unchanged.")
    else:
        winner, _ = qualifying[0]
        print(f"\nBest qualifying {group_label} variant: {winner.name}")
        print("Cross-check: split by coin group (item 8) --")
        for i, group in enumerate(split_tokens_into_groups(snapshots, n_groups=2)):
            group_trades = run_backtest(winner, group)
            group_summary = summarize(f"group {i}", group_trades, verbose=False)
            print(f"  group {i}: n={group_summary['n']} expectancy=${group_summary['expectancy']:+.3f}/trade PF={group_summary['profit_factor']}")
    return qualifying


def main():
    snapshots = _load_snapshots()
    print(f"Loaded {len(snapshots)} tokens with >=2 usable snapshots from {SNAPSHOT_FILE}")

    cutoff_ts = compute_oos_cutoff(snapshots, in_sample_fraction=0.7)
    fold_boundaries = compute_fold_boundaries(snapshots, N_FOLDS)
    earliest, latest = dataset_time_bounds(snapshots)
    print(f"Dataset spans {earliest.isoformat()} .. {latest.isoformat()}")
    print(f"Out-of-sample cutoff (70% in-sample / 30% out-of-sample by time): {cutoff_ts.isoformat()}")
    print(f"Walk-forward folds: {N_FOLDS} (boundaries: {[b.isoformat() for b in fold_boundaries]})")

    compare_group("TRAILING-STOP VARIANTS", CANDIDATE, TRAILING_VARIANTS, snapshots, cutoff_ts, fold_boundaries)
    # Round 4: re-test trailing stops on the CURRENT (round 1+2) entry
    # population, not the original pre-round-1 one above -- a materially
    # different population is a legitimate reason to re-check a
    # previously-rejected idea.
    compare_group("TRAILING-STOP VARIANTS V2 (round 4, on round 1+2's CANDIDATE)", CANDIDATE, TRAILING_VARIANTS_V2, snapshots, cutoff_ts, fold_boundaries)
    # Compared against _PRE_ELEVATED_TREND_GATE_BASE (CANDIDATE's rules
    # BEFORE this session's adopted change), not the current CANDIDATE --
    # CANDIDATE now already includes ENTRY_SCORE_PENALTY_55's change, so
    # comparing these variants against it would compare that winner
    # against an identical copy of itself. This reproduces the exact
    # historical comparison the 2026-09-04 decision was based on.
    compare_group(
        "ENTRY-MOMENTUM-QUALITY VARIANTS", _PRE_ELEVATED_TREND_GATE_BASE, ENTRY_FILTER_VARIANTS,
        snapshots, cutoff_ts, fold_boundaries, report_rising_strong_velocity=True,
    )
    # Round 2: compared against _PRE_VELOCITY_SPIKE_GATE_BASE (round 1's
    # adopted rules, BEFORE round 2's own adopted change) -- CANDIDATE
    # now already includes ENTRY_VELOCITY_SPIKE_COOLDOWN's change, so
    # comparing these variants against it would compare that winner
    # against an identical copy of itself. This reproduces the exact
    # historical comparison the round-2 decision was based on.
    compare_group(
        "RISING/HIGH-VELOCITY VARIANTS (round 2, layered on round 1's adopted CANDIDATE)",
        _PRE_VELOCITY_SPIKE_GATE_BASE, RISING_VELOCITY_VARIANTS,
        snapshots, cutoff_ts, fold_boundaries, report_rising_strong_velocity=True,
    )
    compare_group(
        "BUY_RATIO VARIANTS (round 3, layered on round 1+2's adopted CANDIDATE)",
        _PRE_BUY_RATIO_GATE_BASE, BUY_RATIO_VARIANTS,
        snapshots, cutoff_ts, fold_boundaries, report_rising_strong_velocity=True,
    )
    # Round 5: safe to build directly from CANDIDATE (entry_trends is
    # orthogonal to CANDIDATE's own adopted fields, same reasoning as
    # _trail_on_candidate above).
    compare_group("WEAK-TREND VARIANTS (round 5)", CANDIDATE, WEAK_TREND_VARIANTS, snapshots, cutoff_ts, fold_boundaries)


if __name__ == "__main__":
    main()
