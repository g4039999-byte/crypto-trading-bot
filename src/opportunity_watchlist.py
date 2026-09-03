"""Independent tracking of discovered opportunities across radar cycles.

Fully separate from execution/position state: this module owns its own
state file (data/opportunity_watchlist.json), never imports
src/portfolio.py, src/paper_portfolio.py, src/wallet.py,
src/live_trader.py, or src/paper_trader.py, and is never imported by any
of them either. It makes no trading decisions, holds no position, size,
or PnL data, and cannot open, close, or otherwise affect any real or
paper position. Its only job is to answer "what did we notice, when, and
how did it change" -- a monitoring/observability layer, like
src/snapshot.py, not a trading layer.

Phase 7: this module DOES import src.news_signal_engine (read-only --
see attach_news_signals() near the end of this file) to attach active
news signals onto a matching opportunity's entry, purely as displayable
information. This is a deliberate, one-way exception to the isolation
above, reviewed and scoped narrowly: attach_news_signals() cannot create
an opportunity, cannot change a status, cannot touch qualification
logic, and does not write a history entry -- see that function's own
docstring for the full guarantee. It remains true that no execution/
wallet/risk module is imported here, in either direction.

State machine (per token address):
    NEW       -- first time this address is ever recorded here.
    WATCHING  -- seen again; current data is not yet clearly good or bad.
    QUALIFIED -- current score/trend clears this module's OWN
                 qualification bar (config.OPPORTUNITY_QUALIFY_SCORE /
                 OPPORTUNITY_QUALIFY_TRENDS -- independent of
                 MIN_LIVE_SCORE / ACCEPTABLE_ENTRY_TRENDS, which govern
                 actual trading decisions elsewhere, not this).
    REJECTED  -- current score/trend/first-pass-filter result is clearly
                 bad. TERMINAL: once set, status never changes again for
                 that address, even if later data would otherwise
                 re-qualify it -- a rejection is a recorded judgement,
                 not something that should flicker cycle to cycle.
    EXPIRED   -- has not been updated (i.e. not discovered/watchlisted by
                 the radar) for OPPORTUNITY_EXPIRY_MINUTES. NOT terminal:
                 if the address is ever seen again, it re-enters the
                 active NEW/WATCHING/QUALIFIED funnel on that update
                 instead of staying stuck as EXPIRED forever.

NEW, WATCHING and QUALIFIED are the "active" funnel and can move freely
between each other on every update as fresh data arrives. That movement
-- plus every score/base_score/momentum_score/trend/stage value seen,
and (since Phase 4) the additional src.momentum_signals fields
(buy_sell_pressure/volume_momentum/price_acceleration/persistence_streak)
when available -- is recorded in each entry's `history` (capped at
config.OPPORTUNITY_HISTORY_LIMIT entries), which is the actual point of
this module: not just current status, but how it got there. These
signal fields are purely recorded, never used in the status
classification logic below.

Performance: one JSON read + one JSON write per radar cycle total
(batched across every result in that cycle, not one disk operation per
token) -- this is less disk I/O per cycle than src/snapshot.py already
does today, so this module adds no meaningful overhead to the radar.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.news_signal_engine import active_signals as _active_news_signals
from src.news_signal_engine import group_signals_by_asset

logger = logging.getLogger(__name__)

# Standalone settings: read directly from the environment rather than
# importing src.config, so this module has zero dependency on that
# file's exact contents (its own defaults match what config.py would
# otherwise have provided). Override via a real .env / real env vars
# exactly as before -- only the import path changed.
def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


OPPORTUNITY_QUALIFY_SCORE = _env_int("OPPORTUNITY_QUALIFY_SCORE", 75)
OPPORTUNITY_QUALIFY_TRENDS = tuple(
    t.strip() for t in os.getenv("OPPORTUNITY_QUALIFY_TRENDS", "STRONG,RISING").split(",") if t.strip()
)
OPPORTUNITY_REJECT_SCORE = _env_int("OPPORTUNITY_REJECT_SCORE", 20)
OPPORTUNITY_EXPIRY_MINUTES = _env_float("OPPORTUNITY_EXPIRY_MINUTES", 180)
OPPORTUNITY_HISTORY_LIMIT = _env_int("OPPORTUNITY_HISTORY_LIMIT", 60)
NEWS_SIGNAL_WATCHLIST_LINK_ENABLED = _env_bool("NEWS_SIGNAL_WATCHLIST_LINK_ENABLED", True)

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "opportunity_watchlist.json"

# REJECTED is the only true terminal status: EXPIRED can be revived (see
# module docstring), so it is deliberately not included here.
TERMINAL_STATUSES = ("REJECTED",)


def _empty_state():
    return {"opportunities": {}}


def load_state():
    if not STATE_FILE.exists():
        return _empty_state()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s -- treating as empty: %s", STATE_FILE, exc)
        return _empty_state()

    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("opportunities", {})
    if not isinstance(data["opportunities"], dict):
        data["opportunities"] = {}
    return data


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _classify_active(score, trend, ok):
    """Classify into one of the three ACTIVE statuses (NEW is handled by
    update_one() directly on first sighting; EXPIRED is handled by
    _apply_expiry() based on time, not this cycle's data) from this
    cycle's data alone.

    Reject takes priority over qualify if both conditions somehow held
    at once (e.g. a low score is never also a qualifying score given the
    two thresholds below, but ok=False alone is enough to reject
    regardless of score).
    """
    if not ok or (score is not None and score <= OPPORTUNITY_REJECT_SCORE):
        return "REJECTED"
    if score is not None and score >= OPPORTUNITY_QUALIFY_SCORE and trend in OPPORTUNITY_QUALIFY_TRENDS:
        return "QUALIFIED"
    return "WATCHING"


def _history_entry(now, score, base_score, momentum_score, trend, stage, status,
                    buy_sell_pressure=None, volume_momentum=None, price_acceleration=None,
                    persistence_streak=None, x_signal=None):
    return {
        "timestamp": now.isoformat(),
        "score": score,
        "base_score": base_score,
        "momentum_score": momentum_score,
        "trend": trend,
        "stage": stage,
        "status": status,
        # Additional multi-point signals from src.momentum_signals (see
        # radar.evaluate_pair()) -- purely informational, recorded here
        # the same way score/trend/momentum already are. None means "not
        # enough snapshot history yet," same convention as that module.
        "buy_sell_pressure": buy_sell_pressure,
        "volume_momentum": volume_momentum,
        "price_acceleration": price_acceleration,
        "persistence_streak": persistence_streak,
        # X social intelligence (src.x_intelligence, via radar.py's own
        # result dict) -- same "purely recorded, never used in status
        # classification" rule as the signals above. x_signal is None
        # when no X trend correlated to this token this cycle.
        "x_signal": x_signal,
    }


def update_one(state, *, address, symbol, score, base_score, momentum_score, trend, stage, ok, now=None,
                buy_sell_pressure=None, volume_momentum=None, price_acceleration=None, persistence_streak=None,
                x_signal=None):
    """Update (or create) the watchlist entry for one address from one
    radar result. Mutates and returns `state` in place -- callers batch
    many of these into a single load/save_state() pair (see
    update_from_results()) rather than doing disk I/O per token.

    buy_sell_pressure/volume_momentum/price_acceleration/persistence_streak
    are the optional src.momentum_signals fields (see radar.evaluate_pair()'s
    "signals" dict); x_signal is the optional X social-intelligence dict
    (entity/confidence/velocity/is_possible_clone -- update_from_results()
    below builds it from radar.evaluate_pair()'s flat x_trend_detected/
    x_entity/social_* fields). Both are purely recorded into this entry's
    history, same as score/trend/momentum already are. They play no part
    in the NEW/WATCHING/QUALIFIED/REJECTED classification below, which is
    unchanged from before either existed.

    A malformed/placeholder address ("?" or falsy) is a no-op: it can't
    identify a real opportunity to track.
    """
    if not address or address == "?":
        return state

    now = now or datetime.now(timezone.utc)
    opportunities = state.setdefault("opportunities", {})
    entry = opportunities.get(address)

    if entry is None:
        status = "NEW"
        entry = {
            "address": address,
            "symbol": symbol,
            "status": status,
            "first_seen_at": now.isoformat(),
            "last_updated_at": now.isoformat(),
            "history": [],
        }
        opportunities[address] = entry
    elif entry.get("status") in TERMINAL_STATUSES:
        status = entry["status"]  # terminal -- never re-evaluated from fresh data
    else:
        # WATCHING/QUALIFIED (re-evaluated fresh every update) or
        # reviving from EXPIRED (also re-evaluated fresh, not reset to NEW).
        status = _classify_active(score, trend, ok)

    entry["status"] = status
    entry["symbol"] = symbol or entry.get("symbol")
    entry["last_updated_at"] = now.isoformat()
    entry["history"].append(_history_entry(
        now, score, base_score, momentum_score, trend, stage, status,
        buy_sell_pressure=buy_sell_pressure, volume_momentum=volume_momentum,
        price_acceleration=price_acceleration, persistence_streak=persistence_streak,
        x_signal=x_signal,
    ))
    entry["history"] = entry["history"][-OPPORTUNITY_HISTORY_LIMIT:]

    return state


def _apply_expiry(state, touched_addresses, now=None):
    """Mark any non-terminal, non-EXPIRED entry NOT touched this cycle
    as EXPIRED once OPPORTUNITY_EXPIRY_MINUTES has passed since its
    last_updated_at. Never touches REJECTED (already terminal), already-
    EXPIRED entries, or anything touched this cycle.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = timedelta(minutes=OPPORTUNITY_EXPIRY_MINUTES)

    for address, entry in state.get("opportunities", {}).items():
        if address in touched_addresses:
            continue
        if entry.get("status") in TERMINAL_STATUSES or entry.get("status") == "EXPIRED":
            continue
        try:
            last_updated = datetime.fromisoformat(entry["last_updated_at"])
        except (TypeError, ValueError, KeyError):
            continue
        if now - last_updated >= cutoff:
            entry["status"] = "EXPIRED"
            entry["last_updated_at"] = now.isoformat()

    return state


def update_from_results(results, now=None):
    """Update the watchlist from one radar cycle's results list.

    This is what radar.run_radar() calls, unconditionally, every cycle
    -- never gated behind --paper/--live, since this is observational
    infrastructure (like save_snapshot()), not a trading mode.

    De-duplicates by address within this single call (defensive: even if
    `results` somehow contained the same address twice -- it shouldn't,
    since run_radar() already de-duplicates its own address list before
    fetching -- only the first occurrence would be used here).

    Never raises: any problem is logged and the on-disk state is left
    exactly as it was for this cycle, rather than risking a partial or
    corrupt write. (radar.run_radar() additionally wraps this call in
    its own try/except as defense in depth, the same pattern already
    used there for Pump.fun discovery.)
    """
    if not results:
        return

    try:
        state = load_state()
        now = now or datetime.now(timezone.utc)
        touched = set()

        for item in results:
            if not isinstance(item, dict):
                continue
            address = item.get("address")
            if not address or address == "?" or address in touched:
                continue
            touched.add(address)
            signals = item.get("signals") or {}
            if not isinstance(signals, dict):
                signals = {}
            x_signal = None
            if item.get("x_trend_detected"):
                x_signal = {
                    "entity": item.get("x_entity"),
                    "confidence": item.get("social_confidence"),
                    "velocity_per_minute": item.get("social_velocity"),
                    "source_quality": item.get("source_quality"),
                    "independent_mentions": item.get("independent_mentions"),
                    "score_bonus": item.get("social_score_bonus"),
                    "is_possible_clone": item.get("possible_clone"),
                }
            update_one(
                state,
                address=address,
                symbol=item.get("symbol"),
                score=item.get("score"),
                base_score=item.get("base_score"),
                momentum_score=item.get("momentum_score"),
                trend=item.get("trend"),
                stage=item.get("stage"),
                ok=bool(item.get("ok")),
                now=now,
                buy_sell_pressure=signals.get("buy_sell_pressure"),
                volume_momentum=signals.get("volume_momentum"),
                price_acceleration=signals.get("price_acceleration"),
                persistence_streak=signals.get("persistence_streak"),
                x_signal=x_signal,
            )

        _apply_expiry(state, touched, now=now)
        save_state(state)
    except Exception:
        logger.exception("Failed to update the opportunity watchlist -- leaving it as it was")


def get_opportunity(address):
    """Read-only lookup of one address's current watchlist entry, or
    None if it has never been tracked.
    """
    return load_state().get("opportunities", {}).get(address)


def list_by_status(status):
    """Read-only list of every tracked entry currently in `status`,
    most-recently-updated first.
    """
    entries = [
        entry for entry in load_state().get("opportunities", {}).values()
        if entry.get("status") == status
    ]
    entries.sort(key=lambda entry: entry.get("last_updated_at", ""), reverse=True)
    return entries


def list_all():
    """Read-only list of every tracked entry regardless of status,
    most-recently-updated first. Phase 8: this is what src.cli's
    `watchlist` command (with no --status filter) and "recent radar
    opportunities" both read from -- this project has no separate
    storage of "last radar results" apart from the opportunity
    watchlist itself, so this is the single source for both.
    """
    entries = list(load_state().get("opportunities", {}).values())
    entries.sort(key=lambda entry: entry.get("last_updated_at", ""), reverse=True)
    return entries


def attach_news_signals(results, now=None):
    """Phase 7: attach currently-active news signals (src.news_signal_engine)
    onto the EXISTING watchlist entries for the symbols seen in this
    cycle's `results` -- purely informational, for something (a future
    report/CLI view, or a person reading data/opportunity_watchlist.json)
    to display alongside a tracked opportunity. This is intentionally
    the least this integration could do:

      - Every entry's "news" field is a small, fixed-shape list of
        {event_id, event_type, sentiment, confidence, directional_bias,
        urgency} dicts -- NOT the raw headline text or url, to keep
        entries small. The full record is always available separately
        via news_signal_engine.active_signals()/signals_for_symbols(),
        keyed by event_id, if ever needed.
      - This field is OVERWRITTEN each cycle, never appended to
        `history` -- a news event is a standing fact ("this is
        currently active"), not a new per-cycle measurement like
        score/trend/momentum, so accumulating it into history would
        just repeat the same entries every cycle until the signal
        expires. When there is no longer a matching active signal, the
        field becomes an empty list -- it is never left stale.
      - NEVER creates a new opportunity: only addresses that already
        have a watchlist entry (created by update_from_results(), from
        an actual radar result) get a "news" field at all. A symbol
        that only exists in news, with no matching radar result ever
        seen, does not appear in the watchlist because of this
        function.
      - NEVER changes `status`, never touches `_classify_active()`,
        never writes a `history` entry. Qualification/rejection logic
        is completely unaware this function exists.
      - Reads active_signals() from src.news_signal_engine exactly ONCE
        per call (not once per token) and matches entirely in memory
        via group_signals_by_asset() -- one extra file read per radar
        cycle total, not one per token.
      - Never raises: a malformed signal, a missing symbol, or any
        other unexpected shape is handled by simply not enriching that
        entry, never by crashing. radar.py additionally wraps the call
        to this function in its own try/except as defense in depth,
        the same pattern already used for Pump.fun discovery and for
        update_from_results() itself.
      - Opt-out: set NEWS_SIGNAL_WATCHLIST_LINK_ENABLED=false to disable
        this specific link instantly -- the news engine itself, and
        every other part of the watchlist, keeps working exactly as
        before either way.
    """
    if not NEWS_SIGNAL_WATCHLIST_LINK_ENABLED or not results:
        return

    symbols_seen = {
        item.get("symbol") for item in (results or [])
        if isinstance(item, dict) and isinstance(item.get("symbol"), str)
    }
    if not symbols_seen:
        return

    try:
        signals_by_asset = group_signals_by_asset(_active_news_signals(now=now))
    except Exception:
        logger.exception("Could not read active news signals -- leaving watchlist entries' news untouched")
        return

    state = load_state()
    opportunities = state.get("opportunities", {})
    changed = False

    for item in results:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        symbol = item.get("symbol")
        entry = opportunities.get(address)
        if entry is None or not isinstance(symbol, str):
            continue

        matches = signals_by_asset.get(symbol.upper(), [])
        news_summary = [
            {
                "event_id": sig.get("event_id"),
                "event_type": sig.get("event_type"),
                "sentiment": sig.get("sentiment"),
                "confidence": sig.get("confidence"),
                "directional_bias": sig.get("directional_bias"),
                "urgency": sig.get("urgency"),
            }
            for sig in matches
        ]
        if entry.get("news") != news_summary:
            entry["news"] = news_summary
            changed = True

    if changed:
        save_state(state)
