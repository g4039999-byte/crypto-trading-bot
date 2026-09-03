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
    PAPER_ENTRY_TRENDS,
    PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT,
    PAPER_MAX_PAIR_AGE_MINUTES,
    PAPER_MIN_LIQUIDITY_USD,
    PAPER_MIN_PAIR_AGE_MINUTES,
    PAPER_MIN_SCORE,
    PAPER_MIN_VOLUME_24H_USD,
    PAPER_STOP_LOSS_COOLDOWN_MINUTES,
    SELLABILITY_PROBE_SOL,
)
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

logger = logging.getLogger(__name__)


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

    reason = (
        f"score {score}>={PAPER_MIN_SCORE}, trend {trend}, "
        f"liquidity/volume/age/sellability screening passed (paper)"
    )
    log_decision(
        "BUY", symbol, address, reason,
        extra={
            "score": score, "trend": trend, "size_usd": size_usd,
            "age_minutes": age_minutes, "discovery_to_entry_seconds": discovery_to_entry_seconds,
        },
    )
    return {
        "action": "BUY", "reason": reason, "size_usd": size_usd,
        "entry_score": score, "entry_trend": trend, "entry_age_minutes": age_minutes,
        "discovery_to_entry_seconds": discovery_to_entry_seconds,
    }


def evaluate_exit(position, current_price_usd):
    should_exit, reason = check_exit(position, current_price_usd)
    action = "SELL" if should_exit else "HOLD"
    if should_exit:
        log_decision(action, position["symbol"], position["token_address"], reason, extra={"current_price_usd": current_price_usd})
    return {"action": action, "reason": reason}


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
    closed_this_cycle = set()
    for position in list(state.get("open_positions", [])):
        price = current_prices.get(position["token_address"])
        if price is None:
            logger.debug("No current price for open paper position %s this cycle -- skipping exit check", position["symbol"])
            continue
        exit_decision = evaluate_exit(position, price)
        decisions.append(exit_decision)
        if exit_decision["action"] == "SELL":
            close_position(position["token_address"], price, exit_decision["reason"])
            closed_this_cycle.add(position["token_address"])

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
            )

    return decisions
