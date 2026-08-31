"""Live-trading decision layer: entry and exit logic, wired to every
safety gate in this project.

IMPORTANT: run_live_cycle() below NEVER sends a real order. Even a "BUY"
decision only reaches trade_logger + (if every gate somehow passed)
src.wallet.build_and_send_swap(), which itself refuses to run unless its
own module-level EXECUTION_ENABLED_IN_CODE constant has been manually
flipped in source -- something this code does not do and nothing in .env
can trigger. See src/wallet.py's docstring and src/kill_switch.py.

Everything here operates on the result dicts produced by
radar.evaluate_pair() (score, liquidity, volume, age, trend, address, ...).
"""

import logging

from src.config import MAX_SLIPPAGE_BPS, MIN_LIVE_SCORE, SELLABILITY_PROBE_SOL
from src.jupiter_client import round_trip_check
from src.kill_switch import trading_allowed
from src.portfolio import can_open_new_position, check_exit, close_position, compute_position_size_usd, load_state, open_position
from src.risk import assess_token_safety
from src.trade_logger import log_decision

logger = logging.getLogger(__name__)

ACCEPTABLE_ENTRY_TRENDS = ("STRONG", "RISING")


def evaluate_entry(evaluated_pair, probe_check=None):
    """Decide whether to open a position in evaluated_pair right now.

    probe_check: inject a pre-computed round_trip_check() result (mainly
    for tests); if None, this calls Jupiter's public quote API itself.

    Returns a dict: {"action": "BUY" | "SKIP" | "BLOCKED", "reason": str,
    "size_usd": float | None}. Always logs the decision via trade_logger.
    """
    symbol = evaluated_pair.get("symbol", "?")
    address = evaluated_pair.get("address", "?")

    gate = trading_allowed()
    if not gate.allowed:
        decision = {"action": "BLOCKED", "reason": "; ".join(gate.reasons), "size_usd": None}
        log_decision("BLOCKED", symbol, address, decision["reason"])
        return decision

    state = load_state()
    room_ok, room_reason = can_open_new_position(state)
    if not room_ok:
        decision = {"action": "SKIP", "reason": room_reason, "size_usd": None}
        log_decision("SKIP", symbol, address, room_reason)
        return decision

    price_usd = evaluated_pair.get("price_usd")
    if not price_usd or price_usd <= 0:
        reason = "no usable price (price_usd missing or non-positive) -- cannot size a position"
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    score = evaluated_pair.get("score", 0)
    if score < MIN_LIVE_SCORE:
        reason = f"score {score} below live minimum {MIN_LIVE_SCORE}"
        log_decision("SKIP", symbol, address, reason, extra={"score": score})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    trend = evaluated_pair.get("trend")
    if trend not in ACCEPTABLE_ENTRY_TRENDS:
        reason = f"trend '{trend}' not in {ACCEPTABLE_ENTRY_TRENDS}"
        log_decision("SKIP", symbol, address, reason, extra={"trend": trend})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    if probe_check is None:
        probe_lamports = int(SELLABILITY_PROBE_SOL * 1_000_000_000)
        probe_check = round_trip_check(address, probe_lamports, MAX_SLIPPAGE_BPS)

    risk = assess_token_safety(evaluated_pair, probe_check)
    if not risk.passed:
        reason = "; ".join(risk.reasons)
        log_decision("SKIP", symbol, address, reason, extra={"risk_reasons": risk.reasons})
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    size_usd = compute_position_size_usd(state)
    if size_usd <= 0:
        reason = "no capital room left under the deployment cap"
        log_decision("SKIP", symbol, address, reason)
        return {"action": "SKIP", "reason": reason, "size_usd": None}

    reason = "passed score/trend/risk/sellability screening"
    log_decision(
        "BUY", symbol, address, reason,
        extra={"score": score, "trend": trend, "size_usd": size_usd, "simulated": True},
    )
    return {"action": "BUY", "reason": reason, "size_usd": size_usd}


def evaluate_exit(position, current_price_usd):
    """Decide whether to close an existing position. Pure decision logic
    over src.portfolio.check_exit -- does not place any order itself.
    """
    should_exit, reason = check_exit(position, current_price_usd)
    action = "SELL" if should_exit else "HOLD"
    if should_exit:
        log_decision(
            action, position["symbol"], position["token_address"], reason,
            extra={"current_price_usd": current_price_usd, "simulated": True},
        )
    return {"action": action, "reason": reason}


def run_live_cycle(evaluated_pairs, current_prices=None):
    """One pass: check exits for any open position, then look for a
    single new entry among evaluated_pairs (already sorted by score by
    radar.run_radar()).

    current_prices: optional {token_address: current_price_usd} map for
    exit checks (radar does not currently carry live price on its result
    dicts -- wire this up once a live price source is chosen).

    This never calls src.wallet -- it only produces and logs decisions.
    Actually executing a BUY/SELL decision is a separate, not-yet-wired
    step that would call src.wallet.build_and_send_swap(), which is
    itself hard-disabled (see that module). Returns a list of the
    decisions made this cycle, for logging/inspection.
    """
    decisions = []
    current_prices = current_prices or {}

    state = load_state()
    for position in list(state.get("open_positions", [])):
        price = current_prices.get(position["token_address"])
        if price is None:
            logger.warning("No current price available for open position %s -- skipping exit check", position["symbol"])
            continue
        exit_decision = evaluate_exit(position, price)
        decisions.append(exit_decision)
        if exit_decision["action"] == "SELL":
            # Simulated close only -- see module docstring. A real close
            # would additionally need to go through src.wallet, gated as
            # described there.
            close_position(position["token_address"], price, exit_decision["reason"])

    for pair in evaluated_pairs:
        entry_decision = evaluate_entry(pair)
        decisions.append(entry_decision)
        if entry_decision["action"] == "BUY":
            # Position sizing/bookkeeping only -- see module docstring.
            # Real execution never happens here.
            open_position(pair["address"], pair["symbol"], pair["price_usd"], entry_decision["size_usd"])
            break  # one new position per cycle is enough given MAX_OPEN_POSITIONS

    return decisions
