"""Paper trading: the exact same entry/exit decision rules as
src/live_trader.py, applied against simulated positions only.

No wallet, no kill switch check, no real order -- there is nothing here
that *could* move real funds even in principle (unlike live_trader.py,
this module never imports src.wallet at all). Position state lives in
data/paper_positions.json and decisions are logged to
data/paper_trade_log.jsonl -- both entirely separate from the live-
trading files, so a paper run can never be mistaken for, or interfere
with, a real one.

This exists to rehearse the whole pipeline (radar -> risk screening ->
sizing -> entry -> tracking -> stop-loss/take-profit exit) safely, as
many times as needed, before live trading is ever considered.
"""

import logging

from src.config import MAX_SLIPPAGE_BPS, MIN_LIVE_SCORE, SELLABILITY_PROBE_SOL
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

logger = logging.getLogger(__name__)

ACCEPTABLE_ENTRY_TRENDS = ("STRONG", "RISING")


def evaluate_entry(evaluated_pair, probe_check=None):
    """Same rules as live_trader.evaluate_entry(), minus the kill-switch
    gate (there is nothing to gate -- this can never place a real
    order). Returns {"action": "BUY" | "SKIP", "reason": str,
    "size_usd": float | None}.
    """
    symbol = evaluated_pair.get("symbol", "?")
    address = evaluated_pair.get("address", "?")

    state = load_state()
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

    reason = "passed score/trend/risk/sellability screening (paper)"
    log_decision(
        "BUY", symbol, address, reason,
        extra={"score": score, "trend": trend, "size_usd": size_usd},
    )
    return {"action": "BUY", "reason": reason, "size_usd": size_usd}


def evaluate_exit(position, current_price_usd):
    should_exit, reason = check_exit(position, current_price_usd)
    action = "SELL" if should_exit else "HOLD"
    if should_exit:
        log_decision(action, position["symbol"], position["token_address"], reason, extra={"current_price_usd": current_price_usd})
    return {"action": action, "reason": reason}


def run_paper_cycle(evaluated_pairs):
    """One pass over the radar's results: check the open paper position
    for an exit (using each pair's current price_usd when available),
    then look for one new paper entry among evaluated_pairs (already
    sorted by score). This is the function radar.py's `--paper` flag
    wires in as run_once()'s on_results callback.

    Returns the list of decisions made this cycle.
    """
    decisions = []
    current_prices = {p["address"]: p["price_usd"] for p in evaluated_pairs if p.get("price_usd")}

    state = load_state()
    for position in list(state.get("open_positions", [])):
        price = current_prices.get(position["token_address"])
        if price is None:
            logger.debug("No current price for open paper position %s this cycle -- skipping exit check", position["symbol"])
            continue
        exit_decision = evaluate_exit(position, price)
        decisions.append(exit_decision)
        if exit_decision["action"] == "SELL":
            close_position(position["token_address"], price, exit_decision["reason"])

    for pair in evaluated_pairs:
        entry_decision = evaluate_entry(pair)
        decisions.append(entry_decision)
        if entry_decision["action"] == "BUY":
            open_position(pair["address"], pair["symbol"], pair["price_usd"], entry_decision["size_usd"])
            break  # one new paper position per cycle, matching MAX_OPEN_POSITIONS=1 by default

    return decisions
