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

The `solders` (or `solana`) package is intentionally NOT in
requirements.txt (the discovery/analysis radar does not need it) -- it is
listed in requirements-live.txt and only imported lazily, inside the
functions that need it, so the rest of the project works without it
installed.
"""

import logging

import requests

from src.config import SOLANA_PRIVATE_KEY, SOLANA_RPC_URL, SOLANA_WALLET_PUBLIC_KEY

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


def build_and_send_swap(quote_response, priority_fee_lamports=None):
    """Build, sign and send a real swap transaction via Jupiter. THIS
    MOVES REAL FUNDS. It is gated by EXECUTION_ENABLED_IN_CODE (module
    constant, see docstring) as well as every check in
    src.kill_switch.trading_allowed() -- callers must check that gate
    themselves before calling this; this function checks the code-level
    gate itself and refuses regardless of any config.

    Not exercised in this environment: it needs the `solders` package
    (not installed here) and network access to a Solana RPC endpoint
    (not reachable from this sandbox). Review and test it against a
    throwaway wallet with a trivial amount before it is ever used for
    real, and only after flipping EXECUTION_ENABLED_IN_CODE deliberately.
    """
    if not EXECUTION_ENABLED_IN_CODE:
        raise RuntimeError(
            "wallet.EXECUTION_ENABLED_IN_CODE is False -- real swap execution is "
            "disabled at the source level, independent of any .env setting. "
            "This is intentional until the live-trading plan has been reviewed "
            "and explicitly approved."
        )

    try:
        from solders.transaction import VersionedTransaction  # noqa: F401  (lazy import)
    except ImportError as exc:
        raise WalletDependencyMissing(
            "The 'solders' package is required to send a live swap. "
            "Install it with: pip install -r requirements-live.txt"
        ) from exc

    keypair = load_keypair_from_env()

    swap_request = {
        "quoteResponse": quote_response,
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True,
    }
    if priority_fee_lamports is not None:
        swap_request["prioritizationFeeLamports"] = priority_fee_lamports

    # The remaining steps (POST to JUPITER_SWAP_URL, deserialize the
    # returned transaction, sign it with `keypair`, send it via
    # _rpc_call("sendTransaction", ...), and poll for confirmation) are
    # intentionally not implemented yet. Wire them up, test thoroughly
    # against a throwaway wallet with a trivial amount, and only then
    # consider flipping EXECUTION_ENABLED_IN_CODE.
    raise NotImplementedError(
        "Swap sending is scaffolded but not implemented. This is a deliberate "
        "stopping point -- finish and test it before it can ever run."
    )
