"""Paper trading: an active, faster-to-act sibling of src/live_trader.py's
entry/exit rules, applied against simulated positions only.

No wallet, no kill switch check, no real order -- there is nothing here
that *could* move real funds even in principle (unlike live_trader.py,
this module never imports src.wallet at all). Position state lives in
data/paper_positions.json and decisions are logged to
data/paper_trade_log.jsonl -- both entirely separate from the live-
trading files, so a paper run can never be mistaken for, or interfere
with, a real one.

This exists to rehearse the whole pipeline (radar -> risk screening ->
sizing -> entry -> tracking -> stop-loss/take-profit exit) safely, as
many times as needed, before live trading is ever considered -- which
only works if it actually rehearses that pipeline by trading. It
intentionally uses its own, more permissive PAPER_* thresholds (see
src/config.py) rather than live_trader.py's MIN_LIVE_*/ACCEPTABLE_ENTRY_
TRENDS: those are close to the scoring formula's ceiling by design (real
money, maximum selectivity), and reusing them here left this module
skipping every single candidate, every cycle, indefinitely. Honeypot/
sellability screening (round_trip_check + risk.assess_token_safety) is
never weakened -- see that function's docstring.
"""

import logging
from datetime import datetime, timedelta, timezone

from src.config import (
    MAX_SLIPPAGE_BPS,
    PAPER_ELEVATED_TREND_MIN_SCORE,
    PAPER_ENTRY_TRENDS,
    PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT,
    PAPER_MAX_PAIR_AGE_MINUTES,
    PAPER_MIN_LIQUIDITY_USD,
    PAPER_MIN_PAIR_AGE_MINUTES,
    PAPER_MIN_SCORE,
    PAPER_MIN_VOLUME_24H_USD,
    PAPER_STOP_LOSS_COOLDOWN_MINUTES,
    PAPER_VELOCITY_SPIKE_COOLDOWN_MINUTES,
    PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_PER_MIN,
    SELLABILITY_PROBE_SOL,
)
from src.dex_client import fetch_pairs
from src.jupiter_client import round_trip_check
from src.paper_logger import log_decision
from src.paper_portfolio import (
    can_open_new_position,
    check_exit,
    close_position,
    compute_position_size_usd,
    load_state,
    open_position,
)
from src.risk import assess_token_safety
from src.snapshot import load_snapshots
from src.utils import safe_get
from src import x_intelligence

logger = logging.getLogger(__name__)

# trend values src.observation.compute_trend derives from short-term
# buy-FLOW delta (not the same as calculate_score's cumulative
# buy_ratio) -- see PAPER_ELEVATED_TREND_MIN_SCORE's docstring in
# src/config.py for why these two specifically need a higher score bar.
_ELEVATED_TRENDS = ("STRONG", "RISING")


def _liquidity_drawdown_pct(address, current_liquidity):
    """% drop from this token's own recent peak liquidity (across every
    snapshot the radar has recorded for it so far) to its current value.
    A pool that still clears PAPER_MIN_LIQUIDITY_USD can nonetheless be
    in the middle of being actively drained -- both real losing paper
    trades on 2026-09-03 (NEVER, Magachud) had already lost more than
    half their peak liquidity by the time they were bought; the static
    floor alone never caught that because what was left still cleared
    it. See PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT in src/config.py.
    """
    if not current_liquidity:
        return 0.0
    history = load_snapshots(address)
    liquidities = [h.get("liquidity_usd") for h in history if h.get("liquidity_usd")]
    peak = max(liquidities) if liquidities else current_liquidity
    if not peak:
        return 0.0
    return max(0.0, (peak - current_liquidity) / peak * 100)


def _discovery_to_entry_seconds(address):
    """Seconds between the radar's first-ever snapshot of this token and
    right now (i.e. how long between discovering it and buying it) --
    one of the fields requested for tracking "was the entry late?".
    None if there is no history yet (shouldn't happen: risk screening
    already requires a minimum pair age, which itself requires at least
    one prior snapshot to have been taken).
    """
    history = load_snapshots(address)
    if not history:
        return None
    first_ts = history[0].get("timestamp")
    if not first_ts:
        return None
    try:
        first_dt = datetime.fromisoformat(first_ts)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - first_dt).total_seconds()


def _recent_velocity_spike(address, now=None):
    """True if this token has been seen moving faster than
    PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_PER_MIN (%/minute since the
    radar's first-ever snapshot of it -- the SAME quantity scripts/
    backtest_paper_strategy.py's velocity_pct_per_min computes, kept
    faithful to what was actually backtested rather than a hand-
    approximated equivalent) at ANY point within the last
    PAPER_VELOCITY_SPIKE_COOLDOWN_MINUTES. Delays entry on a token that
    has recently pumped hard/fast -- a "buying the top" proxy -- for a
    while rather than rejecting it forever: a token that cools off and
    still otherwise qualifies once the window lapses can still be
    bought. See PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_PER_MIN's docstring
    in src/config.py for the backtest evidence this was validated
    against (fold_stability 0.5->0.75, confirmed across every
    walk-forward fold count and two different out-of-sample cutoffs
    tested, not just one lucky comparison).
    """
    if not PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_PER_MIN or not PAPER_VELOCITY_SPIKE_COOLDOWN_MINUTES:
        return False
    history = load_snapshots(address)
    if len(history) < 2:
        return False

    first = history[0]
    try:
        first_price = float(first.get("price_usd"))
    except (TypeError, ValueError):
        return False
    if not first_price:
        return False
    first_ts_raw = first.get("timestamp")
    if not first_ts_raw:
        return False
    try:
        first_dt = datetime.fromisoformat(first_ts_raw)
    except (ValueError, TypeError):
        return False

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=PAPER_VELOCITY_SPIKE_COOLDOWN_MINUTES)

    for point in history:
        point_ts_raw = point.get("timestamp")
        if not point_ts_raw:
            continue
        try:
            point_dt = datetime.fromisoformat(point_ts_raw)
            point_price = float(point.get("price_usd"))
        except (ValueError, TypeError):
            continue
        if point_dt < cutoff:
            continue  # too old to still count as a "recent" spike

        age_minutes = (point_dt - first_dt).total_seconds() / 60
        if age_minutes <= 0:
            continue
        velocity = (point_price - first_price) / first_price * 100 / age_minutes
        if velocity > PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_PER_MIN:
            return True

    return False


def _recent_stop_loss(state, address):
    """True if this exact token was closed via stop_loss within the
    last PAPER_STOP_LOSS_COOLDOWN_MINUTES. Observed live on 2026-09-03:
    Magachud stopped out, was bought again 5 minutes later while still
    in the same decline, and lost a second time -- nothing previously
    stopped the system from re-entering a token immediately after its
    own stop-loss just fired on it.
    """
    if not PAPER_STOP_LOSS_COOLDOWN_MINUTES:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PAPER_STOP_LOSS_COOLDOWN_MINUTES)
    for trade in state.get("closed_trades", []):
        if trade.get("token_address") != address or trade.get("reason") != "stop_loss":
            continue
        closed_at = trade.get("closed_at")
        try:
            closed_dt = datetime.fromisoformat(closed_at) if closed_at else None
        except (ValueError, TypeError):
            closed_dt = None
        if closed_dt is not None and closed_dt >= cutoff:
            return True
    return False


def evaluate_entry(evaluated_pair, probe_check=None):
    """Paper-mode entry screening: same shape as live_trader.evaluate_
    entry() (score -> trend -> honeypot/liquidity/age risk -> sizing),
    minus the kill-switch gate (there is nothing to gate -- this can
    never place a real order), and using PAPER_* thresholds throughout.
    Returns {"action": "BUY" | "SKIP", "reason": str,
    "size_usd": float | None}.
    """
    symbol = evaluated_pair.get("symbol", "?")
    address = evaluated_pair.get("address", "?")

    state = load_state()

    already_held = any(p.get("token_address") == address for p in state.get("open_positions", []))
    if already_held:
        reason = "already holding an open paper position in this token"
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    if _recent_stop_loss(state, address):
        reason = f"stop-loss cooldown active ({PAPER_STOP_LOSS_COOLDOWN_MINUTES:.0f}m since last stop_loss on this token)"
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    if _recent_velocity_spike(address):
        reason = (
            f"velocity-spike cooldown active (moved >{PAPER_VELOCITY_SPIKE_THRESHOLD_PCT_PER_MIN:.0f}%/min within "
            f"the last {PAPER_VELOCITY_SPIKE_COOLDOWN_MINUTES:.0f}m) -- see PAPER_VELOCITY_SPIKE_* in src/config.py"
        )
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    room_ok, room_reason = can_open_new_position(state)
    if not room_ok:
        log_decision("SKIP", symbol, address, room_reason)
        return {"action": "SKIP", "reason": room_reason, "size_usd": None}

    price_usd = evaluated_pair.get("price_usd")
    if not price_usd or price_usd <= 0:
        reason = "no usable price (price_usd missing or non-positive) -- cannot size a position"
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    score = evaluated_pair.get("score", 0)
    if score < PAPER_MIN_SCORE:
        reason = f"score {score} below paper minimum {PAPER_MIN_SCORE}"
        log_decision("SKIP", symbol, address, reason, extra={"score": score})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    trend = evaluated_pair.get("trend")
    if trend not in PAPER_ENTRY_TRENDS:
        reason = f"trend '{trend}' not in {PAPER_ENTRY_TRENDS}"
        log_decision("SKIP", symbol, address, reason, extra={"trend": trend})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    if trend in _ELEVATED_TRENDS and score < PAPER_ELEVATED_TREND_MIN_SCORE:
        reason = (
            f"trend '{trend}' requires the higher score bar {PAPER_ELEVATED_TREND_MIN_SCORE} "
            f"(has {score}) -- see PAPER_ELEVATED_TREND_MIN_SCORE in src/config.py"
        )
        log_decision("SKIP", symbol, address, reason, extra={"score": score, "trend": trend})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    liq_drawdown_pct = _liquidity_drawdown_pct(address, evaluated_pair.get("liquidity"))
    if liq_drawdown_pct > PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT:
        reason = (
            f"liquidity down {liq_drawdown_pct:.0f}% from its recent peak "
            f"(limit {PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT:.0f}%) -- looks like it's being drained"
        )
        log_decision("SKIP", symbol, address, reason, extra={"liquidity_drawdown_pct": round(liq_drawdown_pct, 1)})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    if probe_check is None:
        probe_lamports = int(SELLABILITY_PROBE_SOL * 1_000_000_000)
        probe_check = round_trip_check(address, probe_lamports, MAX_SLIPPAGE_BPS)

    risk = assess_token_safety(
        evaluated_pair,
        probe_check,
        min_liquidity_usd=PAPER_MIN_LIQUIDITY_USD,
        min_volume_24h_usd=PAPER_MIN_VOLUME_24H_USD,
        min_pair_age_minutes=PAPER_MIN_PAIR_AGE_MINUTES,
        max_pair_age_minutes=PAPER_MAX_PAIR_AGE_MINUTES,
    )
    if not risk.passed:
        reason = "; ".join(risk.reasons)
        log_decision("SKIP", symbol, address, reason, extra={"risk_reasons": risk.reasons})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    size_usd = compute_position_size_usd(state)
    if size_usd <= 0:
        reason = "no capital room left under the deployment cap"
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    age_minutes = evaluated_pair.get("age")
    discovery_to_entry_seconds = _discovery_to_entry_seconds(address)
    x_entity = evaluated_pair.get("x_entity") if evaluated_pair.get("x_trend_detected") else None

    reason = (
        f"score {score}>={PAPER_MIN_SCORE}, trend {trend}, "
        f"liquidity/volume/age/sellability screening passed (paper)"
    )
    if x_entity:
        reason += f", X signal: {x_entity} (confidence {evaluated_pair.get('social_confidence', 0):.2f})"
    log_decision(
        "BUY", symbol, address, reason,
        extra={
            "score": score, "trend": trend, "size_usd": size_usd,
            "age_minutes": age_minutes, "discovery_to_entry_seconds": discovery_to_entry_seconds,
            "x_entity": x_entity,
        },
    )
    return {
        "action": "BUY", "reason": reason, "size_usd": size_usd,
        "entry_score": score, "entry_trend": trend, "entry_age_minutes": age_minutes,
        "discovery_to_entry_seconds": discovery_to_entry_seconds, "x_entity": x_entity,
    }


def evaluate_exit(position, current_price_usd):
    should_exit, reason = check_exit(position, current_price_usd)
    action = "SELL" if should_exit else "HOLD"
    if should_exit:
        log_decision(action, position["symbol"], position["token_address"], reason, extra={"current_price_usd": current_price_usd})
    return {"action": action, "reason": reason}


def _skip_reason_bucket(reason):
    """Collapse a SKIP decision's free-text reason into a small, stable
    set of buckets for the per-cycle funnel summary below -- individual
    reasons (e.g. exact scores) still go to data/paper_trade_log.jsonl
    in full; this is just the aggregate view.
    """
    if "already holding" in reason:
        return "already_held"
    if "velocity-spike cooldown" in reason:
        return "velocity_spike_cooldown"
    if "cooldown" in reason:
        return "stop_loss_cooldown"
    if "already at the max" in reason:
        return "max_open_positions"
    if "daily (paper) loss cap" in reason:
        return "daily_loss_cap"
    if "no usable price" in reason:
        return "no_price"
    if "below paper minimum" in reason:
        return "score_too_low"
    if "requires the higher score bar" in reason:
        return "elevated_trend_needs_higher_score"
    if reason.startswith("trend "):
        return "trend_not_acceptable"
    if "drained" in reason:
        return "liquidity_draining"
    if "capital room" in reason:
        return "capital_deployment_cap"
    if "liquidity" in reason or "24h volume" in reason or "rug-risk window" in reason or "window" in reason or "no recorded trades" in reason:
        return "risk_screen"
    if "sellability" in reason or "honeypot" in reason:
        return "sellability_honeypot"
    return "other"


def _record_x_learning_outcome(close_result):
    """After a position closes, if it was opened on the strength of an
    X signal (position["x_entity"] set at open time), feed the outcome
    back to every contributing account's reputation
    (src.x_intelligence.record_trade_outcome_for_entity) -- this is the
    actual learning step: "was this account's signal followed by a
    real, profitable move, or not". Never raises; a learning-update
    failure must never affect the trade that already closed.
    """
    if not close_result:
        return
    position = close_result.get("position") or {}
    entity = position.get("x_entity")
    if not entity:
        return
    pnl_usd = close_result.get("pnl_usd") or 0.0
    try:
        x_intelligence.record_trade_outcome_for_entity(
            entity, was_useful=(pnl_usd > 0),
            context={"symbol": position.get("symbol"), "pnl_usd": round(pnl_usd, 2)},
        )
    except Exception:
        logger.exception("Failed to record X learning outcome for entity %r -- non-fatal", entity)


def _fill_missing_prices_for_open_positions(current_prices, open_positions):
    """Any open position whose token did NOT come back in this cycle's
    evaluated_pairs is looked up directly here, so its exit check is
    never silently skipped.

    2026-09-04, found live: src.snapshot.known_addresses() (the
    "watchlist" radar.py re-queries every cycle, on top of whatever
    DexScreener's "latest profiles" feed returns fresh) ranks addresses
    by most-recently-SNAPSHOTTED first and caps at RADAR_WATCHLIST_SIZE.
    A token whose pool DexScreener stops returning fresh data for (thin
    liquidity, migrated, delisted, or just temporarily flaky) stops
    getting new snapshots, so its rank keeps falling as other tokens get
    fresher snapshots -- eventually it ages out of the watchlist and is
    never queried again at all. Before this fix, that meant an open
    paper position on that token could never be price-checked again,
    ever -- not stop_loss, not take_profit, not even max_holding_time,
    since every one of those exit checks requires a fresh price this
    cycle. Observed live: a real position stuck open 17+ hours, more
    than 4x MAX_HOLDING_MINUTES, silently un-monitored the entire time.

    This makes one extra, targeted fetch_pairs() call (best-effort,
    never raises) for just the addresses still missing a price after the
    normal scan, so an open position can only ever go one cycle without
    a fresh price, not forever.
    """
    missing_addresses = [
        p["token_address"] for p in open_positions
        if p.get("token_address") and p["token_address"] not in current_prices
    ]
    if not missing_addresses:
        return current_prices

    logger.warning(
        "%s open paper position(s) missing a fresh price this cycle -- fetching directly "
        "so their exit check is never silently skipped: %s",
        len(missing_addresses), missing_addresses,
    )
    try:
        pairs = fetch_pairs(missing_addresses)
    except Exception:
        logger.exception("Fallback price fetch for stranded open position(s) failed -- will retry next cycle")
        return current_prices

    updated = dict(current_prices)
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        address = safe_get(pair, "baseToken", "address")
        price_raw = pair.get("priceUsd")
        if not address or price_raw is None:
            continue
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        if price > 0:
            updated[address] = price

    still_missing = [a for a in missing_addresses if a not in updated]
    if still_missing:
        logger.warning("Fallback fetch still returned no usable price for: %s", still_missing)

    return updated


def run_paper_cycle(evaluated_pairs):
    """One pass over the radar's results: check every open paper position
    for an exit (using each pair's current price_usd when available),
    then look for new paper entries among evaluated_pairs (already sorted
    by score, best first). This is the function radar.py's `--paper`
    flag wires in as run_once()'s on_results callback.

    Unlike a single-position system, this opens as many qualifying
    entries as PAPER_MAX_OPEN_POSITIONS/capital room allow in the same
    cycle (each still fully screened by evaluate_entry -- room and
    capital caps enforced per-pair via can_open_new_position/
    compute_position_size_usd re-reading state after each open), rather
    than stopping after the first. A token just sold this same cycle is
    never immediately re-bought in that same cycle, regardless of its
    trend/score -- it gets a full cycle to be re-evaluated fresh next
    time, avoiding same-cycle flip-flopping.

    Returns the list of decisions made this cycle.
    """
    decisions = []
    current_prices = {p["address"]: p["price_usd"] for p in evaluated_pairs if p.get("price_usd")}

    state = load_state()
    current_prices = _fill_missing_prices_for_open_positions(current_prices, state.get("open_positions", []))
    closed_this_cycle = set()
    for position in list(state.get("open_positions", [])):
        price = current_prices.get(position["token_address"])
        if price is None:
            logger.warning(
                "No current price for open paper position %s (%s) this cycle even after the direct fallback fetch "
                "-- exit check skipped again; see _fill_missing_prices_for_open_positions()'s docstring",
                position["symbol"], position["token_address"],
            )
            continue
        exit_decision = evaluate_exit(position, price)
        decisions.append(exit_decision)
        if exit_decision["action"] == "SELL":
            close_result = close_position(position["token_address"], price, exit_decision["reason"])
            closed_this_cycle.add(position["token_address"])
            _record_x_learning_outcome(close_result)

    for pair in evaluated_pairs:
        if pair.get("address") in closed_this_cycle:
            continue
        entry_decision = evaluate_entry(pair)
        decisions.append(entry_decision)
        if entry_decision["action"] == "BUY":
            open_position(
                pair["address"], pair["symbol"], pair["price_usd"], entry_decision["size_usd"],
                entry_score=entry_decision.get("entry_score"),
                entry_trend=entry_decision.get("entry_trend"),
                entry_reason=entry_decision.get("reason"),
                entry_age_minutes=entry_decision.get("entry_age_minutes"),
                discovery_to_entry_seconds=entry_decision.get("discovery_to_entry_seconds"),
                x_entity=entry_decision.get("x_entity"),
            )

    buys = sum(1 for d in decisions if d["action"] == "BUY")
    sells = sum(1 for d in decisions if d["action"] == "SELL")
    skip_buckets = {}
    for d in decisions:
        if d["action"] != "SKIP":
            continue
        bucket = _skip_reason_bucket(d["reason"])
        skip_buckets[bucket] = skip_buckets.get(bucket, 0) + 1
    logger.info(
        "Paper cycle funnel: %s candidate(s) evaluated -> %s BUY, %s SELL, skip reasons: %s",
        len(evaluated_pairs), buys, sells,
        dict(sorted(skip_buckets.items(), key=lambda kv: -kv[1])) or "none",
    )

    return decisions
