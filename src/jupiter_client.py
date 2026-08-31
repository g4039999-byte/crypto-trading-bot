"""Read-only client for Jupiter's public quote endpoint.

Getting a quote never touches a wallet and never signs or sends anything
-- it just asks "if I swapped X for Y right now, what would I get". This
module is used for two things: (1) pre-trade sellability screening
(round_trip_check), and (2) later, sizing an actual swap. Building the
signed transaction and sending it lives in src/wallet.py, gated far more
strictly (see src/kill_switch.py).

No API key is required for Jupiter's public quote endpoint.
"""

import logging
import time

import requests

from src.config import (
    JUPITER_QUOTE_URL,
    MAX_ROUND_TRIP_LOSS_PCT,
    REQUEST_MAX_RETRIES,
    REQUEST_RETRY_BACKOFF_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SOL_MINT_ADDRESS,
    USDC_MINT_ADDRESS,
)

logger = logging.getLogger(__name__)


class JupiterClientError(Exception):
    """Raised when a quote cannot be obtained after retries."""


def get_quote(input_mint, output_mint, amount, slippage_bps):
    """Fetch a swap quote. `amount` is in the input token's smallest
    unit (lamports for SOL). Returns the parsed JSON quote, or None if
    no route exists / the request ultimately fails -- callers should
    treat None as "cannot verify, do not proceed" rather than retry
    themselves.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": int(amount),
        "slippageBps": int(slippage_bps),
    }

    last_error = None
    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.get(JUPITER_QUOTE_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 404:
                # Jupiter returns 404 when there is genuinely no route --
                # that's a real answer, not a transient failure.
                logger.info("No swap route %s -> %s", input_mint, output_mint)
                return None
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning(
                "Jupiter quote request failed on attempt %s/%s: %s",
                attempt, REQUEST_MAX_RETRIES, exc,
            )
            if attempt < REQUEST_MAX_RETRIES:
                time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    logger.error("Could not get a Jupiter quote for %s -> %s: %s", input_mint, output_mint, last_error)
    return None


def get_sol_usd_price():
    """Best-effort SOL/USD price, implied from a live 1-SOL -> USDC
    Jupiter quote. Used only to convert a USD position-size cap into a
    lamport amount for a real order -- not a general price feed. Returns
    None if a quote cannot be obtained (callers must treat that as
    "cannot size a real order right now", not retry with a guess).
    """
    quote = get_quote(SOL_MINT_ADDRESS, USDC_MINT_ADDRESS, 1_000_000_000, 50)
    if not quote or not quote.get("outAmount"):
        return None
    try:
        usdc_out = int(quote["outAmount"]) / 1_000_000  # USDC has 6 decimals
    except (TypeError, ValueError):
        return None
    return usdc_out if usdc_out > 0 else None


def round_trip_check(token_mint, probe_lamports, slippage_bps):
    """Simulate buying `probe_lamports` worth of SOL into token_mint,
    then immediately quoting selling that same amount back to SOL.

    This never sends a real transaction. It is a cheap, read-only way to
    catch the common "can buy but cannot sell" honeypot pattern and to
    estimate real-world round-trip cost (fees + price impact + any
    hidden sell tax) before ever risking real funds.

    Returns a dict:
        {
            "sellable": bool,
            "reason": str | None,
            "round_trip_loss_pct": float | None,
        }
    """
    buy_quote = get_quote(SOL_MINT_ADDRESS, token_mint, probe_lamports, slippage_bps)
    if not buy_quote or not buy_quote.get("outAmount"):
        return {"sellable": False, "reason": "no buy route available", "round_trip_loss_pct": None}

    token_amount = int(buy_quote["outAmount"])
    if token_amount <= 0:
        return {"sellable": False, "reason": "buy quote returned zero output", "round_trip_loss_pct": None}

    sell_quote = get_quote(token_mint, SOL_MINT_ADDRESS, token_amount, slippage_bps)
    if not sell_quote or not sell_quote.get("outAmount"):
        return {
            "sellable": False,
            "reason": "no sell route available -- possible honeypot",
            "round_trip_loss_pct": None,
        }

    sol_back = int(sell_quote["outAmount"])
    loss_pct = (1 - (sol_back / probe_lamports)) * 100 if probe_lamports else 100.0

    if loss_pct > MAX_ROUND_TRIP_LOSS_PCT:
        return {
            "sellable": True,
            "reason": f"round-trip loss {loss_pct:.1f}% exceeds the {MAX_ROUND_TRIP_LOSS_PCT}% limit",
            "round_trip_loss_pct": loss_pct,
        }

    return {"sellable": True, "reason": None, "round_trip_loss_pct": loss_pct}
