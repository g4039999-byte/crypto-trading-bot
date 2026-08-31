"""Live-trading decision AND real-execution layer, wired to every safety
gate in this project.

IMPORTANT: a BUY/SELL decision that clears every screening step below now
*attempts* a real swap, through _attempt_real_buy() / _attempt_real_sell()
calling src.wallet.build_and_send_swap(). That function still refuses to
run unless its own module-level EXECUTION_ENABLED_IN_CODE constant has
been manually edited to True directly in src/wallet.py's source -- a
deliberate code change, not something this file or any .env setting can
trigger. See src/wallet.py's docstring and src/kill_switch.py.

Practically: as long as EXECUTION_ENABLED_IN_CODE is False (the only
state this project has ever run with), every real-execution attempt below
fails at that gate before touching the network for anything beyond a
read-only quote, and portfolio.open_position()/close_position() are only
ever called after a real swap has actually been confirmed on-chain -- so
nothing here can make local bookkeeping claim a position exists (or was
closed) that doesn't. If execution fails or is refused, the decision is
logged (BLOCKED/ERROR/UNCONFIRMED) and local state is left untouched.

Everything here operates on the result dicts produced by
radar.evaluate_pair() (score, liquidity, volume, age, trend, address, ...).
"""

import logging

import src.wallet as wallet
from src.config import MAX_SLIPPAGE_BPS, MIN_LIVE_SCORE, SELLABILITY_PROBE_SOL, SOL_MINT_ADDRESS
from src.jupiter_client import get_quote, get_sol_usd_price, round_trip_check
from src.kill_switch import trading_allowed
from src.portfolio import can_open_new_position, check_exit, close_position, compute_position_size_usd, load_state, open_position
from src.risk import assess_token_safety
from src.trade_logger import log_decision

logger = logging.getLogger(__name__)

# Exceptions build_and_send_swap() can raise before ever submitting a
# transaction -- all treated as "not executed, nothing moved", just with
# different reasons worth telling apart in the trade log.
_WALLET_NOT_READY_ERRORS = (wallet.WalletNotConfigured, wallet.WalletKeyLooksInvalid, wallet.WalletDependencyMissing)

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


def _attempt_real_buy(pair, size_usd):
    """Try to actually execute a live BUY of `pair`, sized at `size_usd`.
    Only ever reached once evaluate_entry() has already returned "BUY"
    -- meaning trading_allowed() (LIVE_TRADING + confirm phrase + no kill
    switch) already passed. Still refuses unless wallet.EXECUTION_ENABLED_IN_CODE
    has ALSO been manually flipped in source -- see module docstring.

    Never raises: every failure mode is caught and logged, since a live
    loop must not crash on one bad cycle. Returns
    {"executed": bool, "reason": str, ...}.
    """
    symbol = pair.get("symbol", "?")
    address = pair.get("address", "?")

    sol_price_usd = get_sol_usd_price()
    if not sol_price_usd:
        reason = "could not get a live SOL/USD price -- cannot size a real order"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}

    lamports_in = int((size_usd / sol_price_usd) * 1_000_000_000)
    quote = get_quote(SOL_MINT_ADDRESS, address, lamports_in, MAX_SLIPPAGE_BPS)
    if not quote:
        reason = "could not get a live buy quote -- not buying"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}

    try:
        result = wallet.build_and_send_swap(quote)
    except RuntimeError as exc:
        # Expected in every configuration this project has ever run with
        # -- EXECUTION_ENABLED_IN_CODE is False until a human deliberately
        # flips it. This is the normal, safe outcome, not an alarm.
        reason = f"real execution is disabled: {exc}"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}
    except _WALLET_NOT_READY_ERRORS as exc:
        reason = f"wallet not ready: {exc}"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}
    except wallet.SwapExecutionError as exc:
        reason = f"swap could not be built/sent: {exc}"
        logger.error("Live BUY attempt for %s failed before submission: %s", symbol, exc)
        log_decision("ERROR", symbol, address, reason)
        return {"executed": False, "reason": reason}

    if not result.get("confirmed"):
        reason = (
            f"swap sent but not confirmed (signature {result.get('signature')}) -- "
            "check a Solana explorer manually before assuming it did not execute"
        )
        logger.error("Live BUY for %s not confirmed: %s", symbol, result)
        log_decision("UNCONFIRMED", symbol, address, reason, extra={"signature": result.get("signature")})
        return {"executed": False, "reason": reason, "signature": result.get("signature")}

    reason = f"real swap confirmed on-chain (signature {result['signature']})"
    log_decision("BUY", symbol, address, reason, extra={"signature": result["signature"], "simulated": False})
    return {"executed": True, "signature": result["signature"]}


def _attempt_real_sell(position, current_price_usd):
    """Try to actually execute a live SELL of the entire on-chain balance
    of `position`'s token. Reads the real wallet balance for this mint
    (via wallet.get_spl_token_balance_raw()) rather than trusting the
    locally-tracked amount_tokens, so this sells exactly what is actually
    held -- sidestepping the need to separately track each SPL token's
    decimal count. Never raises; see _attempt_real_buy()'s docstring.
    """
    symbol = position.get("symbol", "?")
    address = position.get("token_address", "?")

    try:
        owner = wallet.get_public_key_str()
        token_amount_raw = wallet.get_spl_token_balance_raw(owner, address)
    except _WALLET_NOT_READY_ERRORS as exc:
        reason = f"wallet not ready: {exc}"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}
    except Exception as exc:  # noqa: BLE001 -- a live loop must not crash on one bad cycle
        reason = f"could not read on-chain token balance: {exc}"
        logger.error("Live SELL attempt for %s failed reading balance: %s", symbol, exc)
        log_decision("ERROR", symbol, address, reason)
        return {"executed": False, "reason": reason}

    if token_amount_raw <= 0:
        reason = "on-chain balance for this token is zero -- nothing to sell for real"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}

    quote = get_quote(address, SOL_MINT_ADDRESS, token_amount_raw, MAX_SLIPPAGE_BPS)
    if not quote:
        reason = "could not get a live sell quote -- not selling"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}

    try:
        result = wallet.build_and_send_swap(quote)
    except RuntimeError as exc:
        reason = f"real execution is disabled: {exc}"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}
    except _WALLET_NOT_READY_ERRORS as exc:
        reason = f"wallet not ready: {exc}"
        log_decision("BLOCKED", symbol, address, reason)
        return {"executed": False, "reason": reason}
    except wallet.SwapExecutionError as exc:
        reason = f"swap could not be built/sent: {exc}"
        logger.error("Live SELL attempt for %s failed before submission: %s", symbol, exc)
        log_decision("ERROR", symbol, address, reason)
        return {"executed": False, "reason": reason}

    if not result.get("confirmed"):
        reason = (
            f"swap sent but not confirmed (signature {result.get('signature')}) -- "
            "check a Solana explorer manually before assuming it did not execute"
        )
        logger.error("Live SELL for %s not confirmed: %s", symbol, result)
        log_decision("UNCONFIRMED", symbol, address, reason, extra={"signature": result.get("signature")})
        return {"executed": False, "reason": reason, "signature": result.get("signature")}

    reason = f"real sell confirmed on-chain (signature {result['signature']})"
    log_decision("SELL", symbol, address, reason, extra={"signature": result["signature"], "simulated": False})
    return {"executed": True, "signature": result["signature"]}


def run_live_cycle(evaluated_pairs, current_prices=None):
    """One pass: check exits for any open position, then look for a
    single new entry among evaluated_pairs (already sorted by score by
    radar.run_radar()).

    current_prices: optional {token_address: current_price_usd} map for
    exit checks (radar does not currently carry live price on its result
    dicts -- wire this up once a live price source is chosen).

    Every BUY/SELL decision now attempts a real execution via
    _attempt_real_buy()/_attempt_real_sell() -- see module docstring for
    why that is still safe (EXECUTION_ENABLED_IN_CODE). Local bookkeeping
    (open_position/close_position) is only ever updated after a real
    swap is confirmed on-chain, so it always reflects reality, never
    intent. Returns a list of the decisions made this cycle, for
    logging/inspection.
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
            execution = _attempt_real_sell(position, price)
            if execution["executed"]:
                close_position(position["token_address"], price, exit_decision["reason"])
            else:
                logger.warning(
                    "Exit signal for %s (%s) was not executed for real (%s) -- position "
                    "stays open in local bookkeeping until a real sell succeeds",
                    position["symbol"], position["token_address"], execution["reason"],
                )

    for pair in evaluated_pairs:
        entry_decision = evaluate_entry(pair)
        decisions.append(entry_decision)
        if entry_decision["action"] == "BUY":
            execution = _attempt_real_buy(pair, entry_decision["size_usd"])
            if execution["executed"]:
                open_position(pair["address"], pair["symbol"], pair["price_usd"], entry_decision["size_usd"])
            break  # one entry attempt per cycle is enough given MAX_OPEN_POSITIONS

    return decisions
