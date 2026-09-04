"""Raw Alpaca LIVE (real-money) REST client -- the one module in
src/stocks that can reach api.alpaca.markets (the live-money base URL)
at all. Mirrors src/wallet.py's role and "most sensitive module in this
project" status on the crypto side.

HARD CODE-LEVEL SAFETY GATE (Layer 1 of 3 -- see src/stocks/config.py's
LIVE TRADING GATE block and STOCKS_LIVE_TRADING_GATE.md):
  STOCKS_EXECUTION_ENABLED_IN_CODE below is independent of any .env
  setting. submit_live_order() (and every function above it in the call
  chain) refuses to run unless a human changes that constant from False
  to True directly in THIS source file -- a deliberate, reviewed code
  change, not something a config typo can trigger. It stays False until
  a human has completed every step in STOCKS_LIVE_TRADING_GATE.md and
  explicitly approved going live.

STATUS: fully implemented and unit-tested against mocks only -- never
exercised against Alpaca's real live endpoint, and never will be by this
codebase on its own. Layers 2 and 3 (src.stocks.kill_switch.trading_allowed(),
checked by src.stocks.live_trader before this module is ever called) are
ALSO required, on top of Layer 1 here; this module additionally checks
Layer 1 itself, first, and refuses regardless of what live_trader
decided, so a bug in live_trader's own gate-checking can never be the
only thing standing between this codebase and a real order.

Duplicate-order protection: every order this module submits carries a
caller-supplied client_order_id (Alpaca de-duplicates on this within a
short window on its own end too); callers (src.stocks.live_trader) are
required to check list_live_open_orders()/live_ledger.has_open_position()
for the symbol immediately before calling submit_live_order(), and this
module never retries a submission that already reached Alpaca (see
submit_live_order()'s docstring) -- an ambiguous outcome is surfaced as
LiveOrderAmbiguous, never silently retried.

Alpaca's live and paper environments use DIFFERENT API key pairs --
ALPACA_LIVE_API_KEY/ALPACA_LIVE_API_SECRET, never ALPACA_API_KEY/
ALPACA_API_SECRET (which are paper-only, src.stocks.alpaca_client). A
paper key presented here would simply be rejected by Alpaca; nothing in
this codebase blends the two.
"""

import logging
import time

import requests

from src.stocks.config import (
    ALPACA_LIVE_API_KEY,
    ALPACA_LIVE_API_SECRET,
    ALPACA_LIVE_TRADING_BASE_URL,
    STOCKS_LIVE_ORDER_FILL_TIMEOUT_SECONDS,
    STOCKS_LIVE_ORDER_POLL_SECONDS,
    STOCKS_LIVE_ORDER_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# See "HARD CODE-LEVEL SAFETY GATE" above. Do not flip this without the
# user's explicit, informed go-ahead on the full live-trading plan in
# STOCKS_LIVE_TRADING_GATE.md.
STOCKS_EXECUTION_ENABLED_IN_CODE = False

# Terminal Alpaca order statuses -- once an order reaches one of these,
# polling stops. "filled"/"partially_filled" mean shares actually moved;
# the rest mean the order did not (fully) execute.
_FILLED_STATUSES = ("filled",)
_TERMINAL_NON_FILL_STATUSES = ("canceled", "expired", "rejected", "done_for_day", "replaced")


class LiveTradingDisabled(Exception):
    """Raised when STOCKS_EXECUTION_ENABLED_IN_CODE is False -- the
    expected, safe state in every configuration this project has ever
    run with.
    """


class LiveNotConfigured(Exception):
    """Raised when ALPACA_LIVE_API_KEY/ALPACA_LIVE_API_SECRET are not
    both set.
    """


class LiveOrderRejected(Exception):
    """Alpaca returned a definite 4xx for the order submission itself --
    safe to treat as "definitely not placed".
    """


class LiveOrderAmbiguous(Exception):
    """The order submission request failed at the network level
    (timeout/connection error) before a response was received. This
    does NOT mean the order was not placed -- Alpaca may have received
    and processed it anyway. Callers MUST check
    list_live_open_orders()/get_live_order() for the client_order_id
    used before assuming it did not happen, and must NEVER blindly
    resubmit the same intent after this.
    """


def is_live_configured():
    return bool(ALPACA_LIVE_API_KEY) and bool(ALPACA_LIVE_API_SECRET)


def _live_headers():
    return {"APCA-API-KEY-ID": ALPACA_LIVE_API_KEY, "APCA-API-SECRET-KEY": ALPACA_LIVE_API_SECRET}


def _require_gates_open():
    """Layer 1 check, done first, inside this module, regardless of what
    any caller believes it already checked. Raises rather than returning
    a sentinel -- every function below that moves toward a real order
    calls this before doing anything else network-related.
    """
    if not STOCKS_EXECUTION_ENABLED_IN_CODE:
        raise LiveTradingDisabled(
            "live_broker.STOCKS_EXECUTION_ENABLED_IN_CODE is False -- real stocks "
            "order execution is disabled at the source level, independent of any "
            ".env setting. This is intentional until STOCKS_LIVE_TRADING_GATE.md's "
            "full checklist has been completed and a human has explicitly approved "
            "going live."
        )
    if not is_live_configured():
        raise LiveNotConfigured(
            "ALPACA_LIVE_API_KEY / ALPACA_LIVE_API_SECRET are not both set -- "
            "cannot reach Alpaca's live endpoint without live account credentials."
        )


def _live_get(path, params=None):
    """Read-only GET against the live endpoint. Safe to retry freely --
    unlike order submission, a GET never changes state. Returns the
    parsed JSON body, or None on any failure (never raises for a
    read-only call, matching src.stocks.alpaca_client's convention).
    Still requires Layer 1 (STOCKS_EXECUTION_ENABLED_IN_CODE) -- even
    read-only calls against a real account are gated, since a balance
    read alone can leak account existence/size and is never needed while
    every gate is closed.
    """
    _require_gates_open()
    url = f"{ALPACA_LIVE_TRADING_BASE_URL}{path}"
    for attempt in range(1, 4):
        try:
            response = requests.get(url, headers=_live_headers(), params=params, timeout=STOCKS_LIVE_ORDER_TIMEOUT_SECONDS)
            if response.status_code == 429:
                logger.warning("Alpaca LIVE rate-limited (attempt %s/3) for %s", attempt, path)
                if attempt < 3:
                    time.sleep(1.5 * attempt * 2)
                continue
            if response.status_code in (401, 403):
                logger.error("Alpaca LIVE returned %s -- check ALPACA_LIVE_API_KEY/SECRET. Not retrying.", response.status_code)
                return None
            if 400 <= response.status_code < 500:
                logger.warning("Alpaca LIVE returned HTTP %s for %s (not retrying): %s", response.status_code, path, response.text[:200])
                return None
            response.raise_for_status()
            return response.json() if response.content else {}
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.warning("Alpaca LIVE GET %s failed on attempt %s/3: %s", path, attempt, exc)
            if attempt < 3:
                time.sleep(1.5 * attempt)
    logger.error("Alpaca LIVE GET %s failed after 3 attempt(s)", path)
    return None


def get_live_account():
    """Real account snapshot: equity, cash, buying_power, ... or None if
    unreachable/unconfigured/gated. Used to verify balance/buying power
    before every live buy -- see src.stocks.live_trader.
    """
    return _live_get("/v2/account")


def list_live_open_orders(symbol=None):
    """Currently open (unfilled) real orders, optionally filtered to one
    symbol -- the duplicate-order check src.stocks.live_trader runs
    immediately before every submission, on top of the local ledger
    (src.stocks.live_ledger.has_open_position()), so a stale/lost local
    write can never cause a second real order for a symbol that already
    has one working. Returns [] on any failure -- callers must treat
    that conservatively (as "cannot confirm no duplicate", i.e. do not
    proceed), not as "confirmed none".
    """
    params = {"status": "open"}
    if symbol:
        params["symbols"] = symbol
    result = _live_get("/v2/orders", params=params)
    return result if isinstance(result, list) else []


def get_live_order(order_id):
    return _live_get(f"/v2/orders/{order_id}")


def get_live_positions():
    result = _live_get("/v2/positions")
    return result if isinstance(result, list) else []


def submit_live_order(symbol, qty, side, *, order_type="market", time_in_force="day", client_order_id):
    """Submit ONE real order to Alpaca's live endpoint. THIS MOVES REAL
    MONEY once every gate is open. client_order_id is REQUIRED (not
    optional) -- callers must generate a fresh, unique one per real
    trade intent (src.stocks.live_trader does, via uuid4), both so
    Alpaca can de-duplicate on its own end and so a LiveOrderAmbiguous
    outcome here can be resolved afterward by looking that id up.

    Raises:
      LiveTradingDisabled / LiveNotConfigured -- Layer 1 gate closed or
        live credentials missing. The expected outcome today.
      LiveOrderRejected -- Alpaca returned a definite 4xx for THIS
        request; safe to treat as "definitely not placed".
      LiveOrderAmbiguous -- the request failed before a response was
        received (timeout/connection error). NOT retried automatically
        -- retrying here risks a double order if the first attempt
        actually landed. The caller must resolve this by checking
        list_live_open_orders(symbol) / get_live_positions() for
        client_order_id before doing anything else with this symbol.

    Returns the order dict Alpaca returned on a successful submission
    (this means "accepted", not necessarily "filled yet" -- callers
    should poll via poll_order_fill()).
    """
    _require_gates_open()
    if qty is None or qty <= 0:
        raise ValueError("qty must be positive")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")

    body = {
        "symbol": symbol, "qty": str(qty), "side": side, "type": order_type,
        "time_in_force": time_in_force, "client_order_id": client_order_id,
    }
    url = f"{ALPACA_LIVE_TRADING_BASE_URL}/v2/orders"
    try:
        response = requests.post(url, headers=_live_headers(), json=body, timeout=STOCKS_LIVE_ORDER_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as exc:
        logger.error(
            "Alpaca LIVE order submission for %s %s %s (client_order_id=%s) failed at the "
            "network level -- outcome is AMBIGUOUS, not retrying: %s",
            side, qty, symbol, client_order_id, exc,
        )
        raise LiveOrderAmbiguous(
            f"Network error submitting live order (client_order_id={client_order_id}): {exc}. "
            "Check list_live_open_orders()/get_live_positions() before doing anything else "
            "with this symbol -- do not resubmit."
        ) from exc

    if response.status_code >= 400:
        logger.warning("Alpaca LIVE rejected order for %s (client_order_id=%s): HTTP %s: %s", symbol, client_order_id, response.status_code, response.text[:300])
        raise LiveOrderRejected(f"Alpaca rejected the order: HTTP {response.status_code}: {response.text[:300]}")

    order = response.json()
    logger.warning("[STOCKS LIVE] Real order SUBMITTED: %s %s %s client_order_id=%s order_id=%s", side, qty, symbol, client_order_id, order.get("id"))
    return order


def poll_order_fill(order_id, timeout_seconds=None, poll_interval_seconds=None):
    """Poll a submitted order's status until it fills, reaches a
    terminal non-fill status, or the timeout elapses. Mirrors
    src.wallet._poll_confirmation's shape/philosophy: a timeout does NOT
    mean the order failed -- it may fill moments later or already be
    filled without this having observed it yet. Callers must reconcile
    against get_live_order(order_id)/live_ledger before assuming a
    timed-out order never happened, and must never resubmit.

    Returns a dict: {"filled": bool, "status": str, "filled_qty": ...,
    "filled_avg_price": ..., "order_id": ..., "timed_out": bool}.
    """
    timeout_seconds = STOCKS_LIVE_ORDER_FILL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    poll_interval_seconds = STOCKS_LIVE_ORDER_POLL_SECONDS if poll_interval_seconds is None else poll_interval_seconds

    deadline = time.monotonic() + timeout_seconds
    last_order = None
    while time.monotonic() < deadline:
        order = get_live_order(order_id)
        if order is None:
            time.sleep(poll_interval_seconds)
            continue
        last_order = order
        status = order.get("status")
        if status in _FILLED_STATUSES:
            return {
                "filled": True, "status": status, "order_id": order_id,
                "filled_qty": order.get("filled_qty"), "filled_avg_price": order.get("filled_avg_price"),
                "timed_out": False,
            }
        if status in _TERMINAL_NON_FILL_STATUSES:
            return {"filled": False, "status": status, "order_id": order_id, "timed_out": False}
        time.sleep(poll_interval_seconds)

    logger.error("Polling order %s for a fill timed out after %ss -- last known status: %s", order_id, timeout_seconds, last_order)
    return {
        "filled": False, "status": (last_order or {}).get("status"), "order_id": order_id,
        "timed_out": True,
        "note": (
            "No fill/terminal status observed within the timeout. This does NOT mean "
            "the order failed -- check get_live_order(order_id) before assuming it "
            "did not fill, and do not resubmit the same trade."
        ),
    }


def cancel_all_live_orders():
    """Emergency stop helper: cancel every open real order. Gated
    exactly like submit_live_order() (Layer 1 first). Best-effort --
    logs and returns False on failure rather than raising, since this is
    meant to be safe to call from a panic path.
    """
    _require_gates_open()
    url = f"{ALPACA_LIVE_TRADING_BASE_URL}/v2/orders"
    try:
        response = requests.delete(url, headers=_live_headers(), timeout=STOCKS_LIVE_ORDER_TIMEOUT_SECONDS)
        response.raise_for_status()
        logger.warning("[STOCKS LIVE] Emergency cancel-all-orders request sent (HTTP %s)", response.status_code)
        return True
    except requests.exceptions.RequestException as exc:
        logger.error("Emergency cancel-all-orders failed: %s", exc)
        return False
