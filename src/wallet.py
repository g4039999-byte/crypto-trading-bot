"""Wallet integration -- the most sensitive module in this project.

Read carefully before touching anything below.

SECURITY RULES (non-negotiable):
  1. The private key is read ONLY from the SOLANA_PRIVATE_KEY environment
     variable (via src.config, which loads it from a local .env file that
     is git-ignored). It is never hard-coded, never logged, never printed,
     never included in an exception message, and never sent anywhere
     except to the `solders`/`solana` library call that signs a
     transaction locally, in memory, on the machine running this code.
  2. Never paste a private key or seed phrase into a chat with an AI
     assistant (including the one that wrote this file), an issue
     tracker, a screenshot, or a support ticket. If it ever leaves your
     own machine's environment, treat the wallet as compromised and move
     funds to a new one.
  3. Balance checks and the "connection test" only need the PUBLIC
     address (SOLANA_WALLET_PUBLIC_KEY) -- that is not secret and is safe
     to share. Prefer the public-key-only functions below whenever a
     private key is not strictly required.

HARD CODE-LEVEL SAFETY GATE:
  EXECUTION_ENABLED_IN_CODE below is a second gate, independent of any
  .env setting. build_and_send_swap() refuses to run unless a human
  changes that constant from False to True directly in this source file
  -- a deliberate, reviewed code change, not something a config typo can
  trigger. It stays False until the live-trading plan has been reviewed
  and explicitly approved.

STATUS: build_and_send_swap() is fully implemented (build via Jupiter,
sign locally, submit to the configured RPC, poll for confirmation) but
has never been exercised end to end -- the environment that wrote it has
no `solders` package installed and no network path to Jupiter/Solana RPC.
Nothing about EXECUTION_ENABLED_IN_CODE, LIVE_TRADING, or
CONFIRM_LIVE_TRADING has changed: all three still block it. Before it is
ever trusted with real funds, a human needs to: install
requirements-live.txt, configure a real .env locally, and run one real
swap themselves for a trivial amount (well under $1) -- then verify the
result against a Solana block explorer -- before flipping
EXECUTION_ENABLED_IN_CODE.

The `solders` (or `solana`) package is intentionally NOT in
requirements.txt (the discovery/analysis radar does not need it) -- it is
listed in requirements-live.txt and only imported lazily, inside the
functions that need it, so the rest of the project works without it
installed.
"""

import base64
import logging
import time

import requests

from src.config import (
    JUPITER_SWAP_URL,
    REQUEST_MAX_RETRIES,
    REQUEST_RETRY_BACKOFF_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SOLANA_PRIVATE_KEY,
    SOLANA_RPC_URL,
    SOLANA_WALLET_PUBLIC_KEY,
    SWAP_CONFIRMATION_POLL_SECONDS,
    SWAP_CONFIRMATION_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# See "HARD CODE-LEVEL SAFETY GATE" above. Do not flip this without the
# user's explicit, informed go-ahead on the full live-trading plan.
EXECUTION_ENABLED_IN_CODE = False


class WalletNotConfigured(Exception):
    pass


class WalletDependencyMissing(Exception):
    pass


class WalletKeyLooksInvalid(Exception):
    pass


class SwapExecutionError(Exception):
    """Raised when building, signing, sending, or confirming a real swap
    fails. Never includes the private key or raw signed-transaction bytes
    in its message.
    """


def _looks_like_seed_phrase(value):
    words = value.strip().split()
    return len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words)


def load_keypair_from_env():
    """Load a Solana keypair from SOLANA_PRIVATE_KEY (base58-encoded
    secret key, the format Phantom/Solflare export as a "private key").

    Never call this unless you are about to sign something -- prefer the
    public-key-only helpers below for anything read-only.
    """
    if not SOLANA_PRIVATE_KEY:
        raise WalletNotConfigured(
            "SOLANA_PRIVATE_KEY is not set. Add it to your local .env file -- "
            "never commit it, never paste it anywhere else."
        )

    if _looks_like_seed_phrase(SOLANA_PRIVATE_KEY):
        raise WalletKeyLooksInvalid(
            "SOLANA_PRIVATE_KEY looks like a 12/15/18/21/24-word seed phrase, "
            "not a base58 private key. Refusing to load it. If this really is "
            "your seed phrase: it should never be pasted anywhere, including "
            "into a .env file read by a bot. Export the wallet's base58 "
            "private key instead (Phantom: Settings > Security & Privacy > "
            "Export Private Key), and treat the seed phrase as compromised if "
            "it was ever typed outside your wallet app."
        )

    try:
        from solders.keypair import Keypair  # lazy import, see module docstring
    except ImportError as exc:
        raise WalletDependencyMissing(
            "The 'solders' package is required to load a wallet keypair. "
            "Install it with: pip install -r requirements-live.txt"
        ) from exc

    try:
        return Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
    except Exception as exc:
        # Deliberately not including SOLANA_PRIVATE_KEY or exc's raw args
        # in case the library ever echoes input back in its message.
        raise WalletKeyLooksInvalid("SOLANA_PRIVATE_KEY could not be parsed as a valid base58 secret key") from exc


def get_public_key_str():
    """Returns the wallet's public address without ever touching the
    private key, as long as SOLANA_WALLET_PUBLIC_KEY is configured
    (recommended -- set it even if you also set the private key).
    """
    if SOLANA_WALLET_PUBLIC_KEY:
        return SOLANA_WALLET_PUBLIC_KEY

    logger.warning("SOLANA_WALLET_PUBLIC_KEY is not set -- deriving it from the private key instead")
    keypair = load_keypair_from_env()
    return str(keypair.pubkey())


def _rpc_call(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = requests.post(SOLANA_RPC_URL, json=payload, timeout=15)
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"Solana RPC error calling {method}: {body['error']}")
    return body["result"]


def get_sol_balance_lamports(public_key=None):
    """Read-only balance check via the Solana JSON-RPC API. Only needs a
    PUBLIC address -- safe to use as a connectivity test before any
    private key is ever configured.
    """
    public_key = public_key or get_public_key_str()
    result = _rpc_call("getBalance", [public_key])
    return result["value"]


def get_spl_token_balance_raw(owner_public_key, mint_address):
    """Read-only: the raw (smallest-unit) balance of `mint_address` held
    by `owner_public_key`, summed across every token account that owner
    has for this mint (normally just one). Returns 0 if none is found.

    Used to size a real sell as "whatever is actually held right now"
    rather than separately tracking each SPL token's decimal count --
    the RPC already returns the amount in the token's own smallest unit,
    ready to pass straight into a Jupiter quote.
    """
    result = _rpc_call(
        "getTokenAccountsByOwner",
        [owner_public_key, {"mint": mint_address}, {"encoding": "jsonParsed"}],
    )
    total = 0
    for account in (result or {}).get("value", []):
        try:
            amount_str = account["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
            total += int(amount_str)
        except (KeyError, TypeError, ValueError):
            continue
    return total


def connection_test():
    """A safe, read-only check: confirms the configured RPC endpoint is
    reachable and, if a public key is configured, that it resolves to a
    real (fetchable) account. Never requires or touches the private key.
    Returns a dict describing the outcome -- never raises for a
    network/config problem, so this is safe to call from a status
    command or the live-trading plan report.
    """
    result = {"rpc_url": SOLANA_RPC_URL, "rpc_reachable": False, "public_key_set": bool(SOLANA_WALLET_PUBLIC_KEY)}

    try:
        # getHealth needs no params and no wallet -- pure connectivity check.
        _rpc_call("getHealth", [])
        result["rpc_reachable"] = True
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if SOLANA_WALLET_PUBLIC_KEY:
        try:
            lamports = get_sol_balance_lamports(SOLANA_WALLET_PUBLIC_KEY)
            result["balance_sol"] = lamports / 1_000_000_000
        except Exception as exc:
            result["balance_error"] = str(exc)

    return result


def _request_swap_transaction(quote_response, user_public_key, priority_fee_lamports=None):
    """Ask Jupiter to build an unsigned, ready-to-sign transaction for
    quote_response. Read/build-only -- does not touch a private key and
    does not submit anything on-chain, so it is safe to retry on
    transient failures.

    Returns the transaction as a base64 string (Jupiter's "swapTransaction").
    """
    swap_request = {
        "quoteResponse": quote_response,
        "userPublicKey": user_public_key,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": priority_fee_lamports if priority_fee_lamports is not None else "auto",
    }

    last_error = None
    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.post(JUPITER_SWAP_URL, json=swap_request, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            if not data.get("swapTransaction"):
                raise SwapExecutionError(f"Jupiter /swap response had no 'swapTransaction' field: {data}")
            return data["swapTransaction"]
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning(
                "Jupiter /swap request failed on attempt %s/%s: %s", attempt, REQUEST_MAX_RETRIES, exc
            )
            if attempt < REQUEST_MAX_RETRIES:
                time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    raise SwapExecutionError(f"Could not build the swap transaction via Jupiter: {last_error}")


def _sign_swap_transaction(swap_transaction_b64, keypair):
    """Deserialize the unsigned transaction Jupiter returned, sign it
    locally in memory with `keypair`, and return the signed transaction
    re-encoded as base64, ready to submit. Never logs the transaction
    bytes, the signature, or the key.
    """
    from solders.transaction import VersionedTransaction  # lazy import, see module docstring

    raw_bytes = base64.b64decode(swap_transaction_b64)
    unsigned_tx = VersionedTransaction.from_bytes(raw_bytes)
    signature = keypair.sign_message(bytes(unsigned_tx.message))
    signed_tx = VersionedTransaction.populate(unsigned_tx.message, [signature])
    return base64.b64encode(bytes(signed_tx)).decode("ascii")


def _send_raw_transaction(signed_tx_b64):
    """Submit an already-signed transaction to the configured RPC
    endpoint. This is the one call in this entire module that actually
    moves funds -- everything before it (quoting, building, signing) is
    either read-only or purely local. Returns the transaction signature
    (a public identifier, not secret) on success.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [signed_tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }
    response = requests.post(SOLANA_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise SwapExecutionError(f"sendTransaction was rejected by the RPC node: {data['error']}")
    return data["result"]


def _poll_confirmation(signature, timeout_seconds=None, poll_interval_seconds=None):
    """Poll the RPC endpoint until `signature` is confirmed/finalized,
    fails on-chain, or the timeout elapses.

    IMPORTANT: timed_out=True does NOT mean the trade failed -- the
    transaction may already be on-chain and simply not observed yet, or
    it may land moments later. Always check the signature on a Solana
    explorer (e.g. https://solscan.io/tx/<signature>) before assuming a
    timed-out swap did not execute; never blindly retry a whole
    build_and_send_swap() call after a timeout, since that could send
    the same trade twice.
    """
    timeout_seconds = SWAP_CONFIRMATION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    poll_interval_seconds = (
        SWAP_CONFIRMATION_POLL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    )

    deadline = time.monotonic() + timeout_seconds
    last_status = None
    while time.monotonic() < deadline:
        try:
            result = _rpc_call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        except Exception as exc:
            logger.warning("Could not poll confirmation status for %s: %s", signature, exc)
            time.sleep(poll_interval_seconds)
            continue

        statuses = (result or {}).get("value") or [None]
        status = statuses[0]
        last_status = status
        if status is not None:
            if status.get("err"):
                return {"confirmed": False, "signature": signature, "on_chain_error": status["err"]}
            confirmation_status = status.get("confirmationStatus")
            if confirmation_status in ("confirmed", "finalized"):
                return {"confirmed": True, "signature": signature, "confirmation_status": confirmation_status}

        time.sleep(poll_interval_seconds)

    return {
        "confirmed": False,
        "signature": signature,
        "timed_out": True,
        "last_status": last_status,
        "note": (
            "No confirmation observed within the timeout. This does NOT mean the "
            "trade failed -- check the signature on a Solana explorer before "
            "assuming it did not execute, and do not resend the same trade blindly."
        ),
    }


def build_and_send_swap(quote_response, priority_fee_lamports=None):
    """Build, sign and send a real swap transaction via Jupiter. THIS
    MOVES REAL FUNDS. It is gated by EXECUTION_ENABLED_IN_CODE (module
    constant, see docstring) as well as every check in
    src.kill_switch.trading_allowed() -- callers must check that gate
    themselves before calling this; this function checks the code-level
    gate itself, first, and refuses regardless of any config.

    Steps, in order: (1) ask Jupiter to build an unsigned transaction for
    quote_response, (2) sign it locally in memory with the keypair loaded
    from SOLANA_PRIVATE_KEY, (3) submit the signed transaction to the
    configured Solana RPC endpoint, (4) poll for on-chain confirmation.

    Returns a dict: {"signature": str, "confirmed": bool, ...}. Raises
    SwapExecutionError if a step before submission fails (nothing was
    sent) -- once _send_raw_transaction() has returned a signature,
    every subsequent problem is reported in the return value instead of
    raised, since the trade may already be on-chain at that point.

    NOT exercised end-to-end in the environment that wrote it: doing so
    needs the `solders` package (not installed there) and network access
    to Jupiter/Solana RPC (not reachable from that sandbox). Review this
    function and test it yourself against a trivial amount (a fraction
    of a dollar) before ever trusting it with a real position, and only
    after deliberately flipping EXECUTION_ENABLED_IN_CODE to True here.
    """
    if not EXECUTION_ENABLED_IN_CODE:
        raise RuntimeError(
            "wallet.EXECUTION_ENABLED_IN_CODE is False -- real swap execution is "
            "disabled at the source level, independent of any .env setting. "
            "This is intentional until the live-trading plan has been reviewed "
            "and explicitly approved."
        )

    try:
        import solders  # noqa: F401  (lazy import, see module docstring)
    except ImportError as exc:
        raise WalletDependencyMissing(
            "The 'solders' package is required to send a live swap. "
            "Install it with: pip install -r requirements-live.txt"
        ) from exc

    keypair = load_keypair_from_env()
    user_public_key = str(keypair.pubkey())

    swap_transaction_b64 = _request_swap_transaction(quote_response, user_public_key, priority_fee_lamports)
    signed_tx_b64 = _sign_swap_transaction(swap_transaction_b64, keypair)

    # Everything above this line can be retried freely -- nothing has
    # been submitted to the network yet. Everything from here on cannot:
    # a network error *after* the RPC node accepts the transaction does
    # not mean it didn't happen.
    signature = _send_raw_transaction(signed_tx_b64)
    logger.warning("Real swap transaction submitted: %s", signature)

    confirmation = _poll_confirmation(signature)
    result = {"signature": signature, **confirmation}
    if confirmation.get("confirmed"):
        logger.warning("Real swap transaction confirmed: %s", signature)
    else:
        logger.error("Real swap transaction not confirmed as successful: %s", result)
    return result
