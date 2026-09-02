"""Read-only performance analysis over CLOSED trades (paper and/or
live) -- a Phase 5 add-on that answers "how has this actually
performed" after the fact.

This module NEVER writes anything, NEVER runs on its own decision
cadence, NEVER opens or closes a position, and cannot change what
live_trader.py/paper_trader.py decide to do next: it is not imported by
any of those, or by risk.py or wallet.py, and calling anything in here
has zero effect on any future trade -- it only reads already-recorded
state/log files and reports statistics for a human (or a future
dashboard/report command) to read. See tests/test_performance_analyzer.py's
isolation test.

Data sources (all read-only):
  - src.paper_portfolio.load_state()["closed_trades"] and/or
    src.portfolio.load_state()["closed_trades"] -- the structured
    record of every closed trade (entry/exit price, size, PnL, exit
    reason, timestamps). This is the primary source for every PnL-based
    statistic below. Reading these files is a one-way dependency: this
    module reads them, but never writes to them, and neither of those
    modules (nor live_trader.py/paper_trader.py) ever imports this one.
  - data/paper_trade_log.jsonl and/or data/trade_log.jsonl (via
    src.paper_logger.LOG_FILE / src.trade_logger.LOG_FILE) -- the
    human-auditable decision log. Used ONLY to enrich a closed trade
    with the score/trend/stage/signals that were true AT ENTRY TIME (a
    closed_trades record itself does not carry those). This is
    best-effort correlation by token_address + nearest log timestamp to
    the trade's opened_at, since there is currently no shared trade ID
    between the two data models (see _entry_context_for()). A closed
    trade with no matching BUY log entry simply gets
    score/trend/stage/signals = None for its segmentation -- it still
    counts fully toward every PnL-based statistic; it is never dropped
    or excluded just because its entry context is missing.

Batch-level, not single-trade: the core function, analyze_trades(), is
a pure function over a LIST of closed trades -- callers decide what
batch that is (all-time, the last N trades, everything closed since a
given date, ...). See analyze_recent() for the file-reading convenience
wrapper around that.

Nothing here is fed back automatically into src/risk.py's thresholds,
src/config.py, or any live/paper decision. Every function returns a
plain, inert data structure -- there is no code path anywhere in this
module (or called from it) that writes to src/config.py, src/risk.py,
or any position/state file.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Fixed score-bucket edges: [0, 50) "<50", [50, 60) "50-59", ... [90, 101) "90-100".
_SCORE_BUCKET_EDGES = (0, 50, 60, 70, 80, 90, 101)
NO_CONTEXT = "NO_CONTEXT"  # bucket label when there's no matching BUY log entry at all


def _parse_ts(value):
    """Parse an ISO timestamp string into an aware datetime, or None on
    anything that isn't one. Never raises.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Loading closed trades (read-only)
# ---------------------------------------------------------------------------

def load_closed_trades(mode="paper"):
    """mode: "paper", "live", or "both". Returns a list of closed-trade
    dicts, each tagged with a "mode" key ("PAPER" or "LIVE") so a mixed
    "both" batch can still be told apart -- real and simulated PnL
    should never be silently blended without the caller being able to
    tell which is which.

    Never raises: a missing, empty, or corrupt state file yields no
    trades from that source (src.paper_portfolio.load_state() and
    src.portfolio.load_state() already degrade this way themselves).
    """
    trades = []

    if mode in ("paper", "both"):
        import src.paper_portfolio as paper_portfolio

        for trade in paper_portfolio.load_state().get("closed_trades", []):
            if isinstance(trade, dict):
                trades.append({**trade, "mode": "PAPER"})

    if mode in ("live", "both"):
        import src.portfolio as portfolio

        for trade in portfolio.load_state().get("closed_trades", []):
            if isinstance(trade, dict):
                trades.append({**trade, "mode": "LIVE"})

    return trades


def _load_buy_log_entries(mode):
    """Read BUY-action entries from the relevant decision log file(s),
    grouped by token_address, each entry a raw dict as logged (extra
    fields included). Skips any line that isn't valid JSON or isn't a
    dict, and returns {} entirely if the log file doesn't exist yet --
    never raises.
    """
    log_files = []
    if mode in ("paper", "both"):
        import src.paper_logger as paper_logger

        log_files.append(paper_logger.LOG_FILE)
    if mode in ("live", "both"):
        import src.trade_logger as trade_logger

        log_files.append(trade_logger.LOG_FILE)

    by_address = {}
    for log_file in log_files:
        path = Path(log_file)
        if not path.exists():
            continue
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.error("Could not read %s -- entry-context enrichment will be skipped for it: %s", path, exc)
            continue

        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("action") != "BUY":
                continue
            address = entry.get("token_address")
            if not address:
                continue
            by_address.setdefault(address, []).append(entry)

    return by_address


def _entry_context_for(trade, buy_entries_by_address):
    """Best-effort: find the BUY decision-log entry that most plausibly
    corresponds to `trade`'s entry, by token_address + the log entry
    whose timestamp is closest to the trade's opened_at. Returns {} if
    no BUY log entry exists for that address at all -- never guesses
    across different tokens, never raises.
    """
    address = trade.get("token_address")
    candidates = buy_entries_by_address.get(address) or []
    if not candidates:
        return {}

    opened_at = _parse_ts(trade.get("opened_at"))
    if opened_at is None:
        return candidates[-1]  # best effort: assume the most recent BUY logged for this address

    best_entry, best_delta = None, None
    for entry in candidates:
        ts = _parse_ts(entry.get("timestamp"))
        if ts is None:
            continue
        delta = abs((ts - opened_at).total_seconds())
        if best_delta is None or delta < best_delta:
            best_entry, best_delta = entry, delta

    return best_entry or {}


# ---------------------------------------------------------------------------
# Pure batch analysis (no file I/O below this point)
# ---------------------------------------------------------------------------

def _score_bucket(score):
    score = _safe_float(score)
    if score is None:
        return NO_CONTEXT
    for i in range(len(_SCORE_BUCKET_EDGES) - 1):
        lo, hi = _SCORE_BUCKET_EDGES[i], _SCORE_BUCKET_EDGES[i + 1]
        if lo <= score < hi:
            return f"{lo}-{hi - 1}" if hi - 1 != lo else str(lo)
    return NO_CONTEXT


def _signal_direction(value):
    value = _safe_float(value)
    if value is None:
        return NO_CONTEXT
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "flat"


def _holding_minutes(trade):
    opened = _parse_ts(trade.get("opened_at"))
    closed = _parse_ts(trade.get("closed_at"))
    if opened is None or closed is None:
        return None
    return (closed - opened).total_seconds() / 60


def _bucket_summary(trades_with_pnl):
    """trades_with_pnl: list of pnl_usd floats for one bucket. Returns
    the compact per-bucket stats shown in every by_* breakdown.
    """
    count = len(trades_with_pnl)
    if count == 0:
        return {"trades": 0, "win_rate": None, "average_pnl_usd": None, "total_pnl_usd": 0.0}
    wins = sum(1 for pnl in trades_with_pnl if pnl > 0)
    return {
        "trades": count,
        "win_rate": wins / count,
        "average_pnl_usd": sum(trades_with_pnl) / count,
        "total_pnl_usd": sum(trades_with_pnl),
    }


def _group_by_with_context(trades, contexts, key_fn):
    groups = {}
    for trade, context in zip(trades, contexts):
        label = key_fn(context)
        pnl = _safe_float(trade.get("pnl_usd")) or 0.0
        groups.setdefault(label, []).append(pnl)
    return {label: _bucket_summary(pnls) for label, pnls in groups.items()}


@dataclass
class PerformanceReport:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = None
    loss_rate: float = None
    average_win_usd: float = None
    average_loss_usd: float = None
    profit_factor: float = None
    expectancy_usd: float = None
    total_pnl_usd: float = 0.0
    average_holding_time_minutes: float = None
    by_score_bucket: dict = field(default_factory=dict)
    by_trend: dict = field(default_factory=dict)
    by_stage: dict = field(default_factory=dict)
    by_entry_reason: dict = field(default_factory=dict)
    by_volume_momentum_direction: dict = field(default_factory=dict)
    by_price_acceleration_direction: dict = field(default_factory=dict)


def analyze_trades(trades, top_n_recent=None):
    """The main entry point: `trades` is a list of closed-trade dicts
    (e.g. from load_closed_trades()). Returns a PerformanceReport with
    the aggregate stats plus every breakdown, computed over this batch
    only -- this function does no file I/O itself, which is what makes
    "batch-level, not one trade at a time" analysis testable in
    isolation from where the trades came from.

    top_n_recent: if given, only the top_n_recent most-recently-closed
    trades (by closed_at; trades with no parseable closed_at sort last
    and are excluded first if trimming is needed) are analyzed -- lets
    a caller ask "how have the last 20 trades gone" as its own batch.

    Never raises: malformed trade dicts, missing fields, and unparsable
    timestamps are all handled by falling back to None/excluding that
    trade from the specific statistic that needed the missing field --
    never a crash, and never silently dropped from the trades entirely
    unless top_n_recent explicitly asked for a subset.
    """
    if not isinstance(trades, list):
        trades = []
    trades = [t for t in trades if isinstance(t, dict)]

    if top_n_recent is not None:
        def _sort_key(trade):
            ts = _parse_ts(trade.get("closed_at"))
            return (ts is None, ts or datetime.min.replace(tzinfo=timezone.utc))

        trades = sorted(trades, key=_sort_key)[-top_n_recent:] if top_n_recent > 0 else []

    report = PerformanceReport(total_trades=len(trades))
    if not trades:
        return report

    pnls = [_safe_float(t.get("pnl_usd")) for t in trades]
    pnls = [p for p in pnls if p is not None]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]

    report.wins = len(wins)
    report.losses = len(losses)
    report.breakeven = len(breakeven)
    report.total_pnl_usd = sum(pnls)

    if pnls:
        report.win_rate = len(wins) / len(pnls)
        report.loss_rate = len(losses) / len(pnls)
        # Simple, always-well-defined expectancy: the mean PnL across
        # every trade in the batch (mathematically equivalent to the
        # classic win_rate*avg_win + loss_rate*avg_loss formula when
        # there are no breakevens; this form needs no special-casing
        # for when there are).
        report.expectancy_usd = report.total_pnl_usd / len(pnls)

    if wins:
        report.average_win_usd = sum(wins) / len(wins)
    if losses:
        report.average_loss_usd = sum(losses) / len(losses)
        loss_sum = abs(sum(losses))
        if loss_sum > 0:
            report.profit_factor = sum(wins) / loss_sum

    holding_times = [_holding_minutes(t) for t in trades]
    holding_times = [h for h in holding_times if h is not None]
    if holding_times:
        report.average_holding_time_minutes = sum(holding_times) / len(holding_times)

    # Entry-context enrichment (score/trend/stage/reason/signals at the
    # moment of entry) is best-effort -- see _entry_context_for()'s
    # docstring. Group trades that have a "mode" in common so paper and
    # live BUY logs are matched against the right trades only.
    modes_present = {t.get("mode") for t in trades if t.get("mode")}
    buy_entries_by_address = {}
    for m in modes_present:
        log_mode = "paper" if m == "PAPER" else "live" if m == "LIVE" else "both"
        buy_entries_by_address.update(_load_buy_log_entries(log_mode))
    if not modes_present:
        buy_entries_by_address = _load_buy_log_entries("both")

    contexts = [_entry_context_for(t, buy_entries_by_address) for t in trades]

    report.by_score_bucket = _group_by_with_context(trades, contexts, lambda ctx: _score_bucket(ctx.get("score")))
    report.by_trend = _group_by_with_context(trades, contexts, lambda ctx: ctx.get("trend") or NO_CONTEXT)
    report.by_stage = _group_by_with_context(trades, contexts, lambda ctx: ctx.get("stage") or NO_CONTEXT)
    report.by_entry_reason = _group_by_with_context(trades, contexts, lambda ctx: ctx.get("reason") or NO_CONTEXT)

    def _signal_value(ctx, key):
        signals = ctx.get("signals")
        if not isinstance(signals, dict):
            return None
        return signals.get(key)

    report.by_volume_momentum_direction = _group_by_with_context(
        trades, contexts, lambda ctx: _signal_direction(_signal_value(ctx, "volume_momentum"))
    )
    report.by_price_acceleration_direction = _group_by_with_context(
        trades, contexts, lambda ctx: _signal_direction(_signal_value(ctx, "price_acceleration"))
    )

    return report


# ---------------------------------------------------------------------------
# File-reading convenience wrapper
# ---------------------------------------------------------------------------

def analyze_recent(mode="paper", top_n_recent=None, since=None):
    """Convenience: load_closed_trades(mode) filtered by `since` (an ISO
    string or datetime -- only trades closed at or after this are kept,
    for a rolling/windowed batch view), then analyze_trades(...).
    """
    trades = load_closed_trades(mode)

    if since is not None:
        since_dt = _parse_ts(since) if not isinstance(since, datetime) else since
        if since_dt is not None:
            trades = [t for t in trades if (_parse_ts(t.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since_dt]

    return analyze_trades(trades, top_n_recent=top_n_recent)
