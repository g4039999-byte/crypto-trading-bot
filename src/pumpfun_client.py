"""Additive, optional second discovery feed: newly-created Pump.fun
token addresses, via Solana Tracker's Data API (Pump.fun itself has no
free/official discovery API -- see src/config.py's PUMPFUN_* block).
Mirrors src/dex_client.py's resilience shape (retry/backoff/timeout, one
module owns every network call, every public function degrades to a
safe empty default rather than raising).

This module contributes ADDRESSES ONLY. src/dex_client.py remains the
sole source of the liquidity/volume/txns data actually used for scoring
-- src/radar.py merges this feed's addresses into the same list it
already builds from DexScreener's "latest profiles" feed and the
watchlist, then fetches real pair data for the combined list exactly as
before. A Pump.fun-discovered address DexScreener has not indexed yet
(e.g. still on its bonding curve, not yet migrated to a DEX pool) simply
returns no pair data this cycle -- fetch_pairs() already handles that
(a batch returning fewer rows than requested is normal, not an error).

Disabled by default in effect (zero network calls) until PUMPFUN_API_KEY
is set -- is_configured() gates every function here, exactly like
src.stocks.alpaca_client.is_configured() and src.x_client's own gate.

NOTE ON VERIFICATION: built against Solana Tracker's documented /search
endpoint (docs.solanatracker.io/data-api) as of 2026-09; this environment
has no outbound network access to independently exercise it against a
real API key, so the response-shape handling below is deliberately
defensive (accepts a bare list OR a dict wrapping the list under a
"data"/"results"/"tokens" key) rather than assuming one exact shape.
Before relying on this in a live radar run, set PUMPFUN_API_KEY and
watch one real cycle's logs for "Unexpected Pump.fun search payload
shape" -- if that appears, the wrapper key differs from what is handled
here and this function needs a one-line update to match it.
"""

import logging
import time

import requests

from src.config import (
    PUMPFUN_API_KEY,
    PUMPFUN_BASE_URL,
    PUMPFUN_DISCOVERY_LIMIT,
    PUMPFUN_ENABLED,
    REQUEST_MAX_RETRIES,
    REQUEST_RETRY_BACKOFF_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

SEARCH_URL = f"{PUMPFUN_BASE_URL}/search"


class PumpFunClientError(Exception):
    """Raised when the Solana Tracker API cannot be reached or returns
    something this module cannot make sense of, after retries are
    exhausted.
    """


def is_configured():
    return bool(PUMPFUN_API_KEY) and PUMPFUN_ENABLED


def _headers():
    return {"x-api-key": PUMPFUN_API_KEY}


def _request_with_retries(url, params):
    """GET url, retrying transient failures with a short backoff --
    same shape as src.dex_client._request_with_retries. Raises
    PumpFunClientError if every attempt fails; never raises a raw
    requests exception.
    """
    last_error = None

    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            last_error = exc
            if status is not None and 400 <= status < 500 and status != 429:
                logger.error("Solana Tracker (Pump.fun) returned HTTP %s for %s (not retrying)", status, url)
                break
            logger.warning("Solana Tracker (Pump.fun) HTTP error on attempt %s/%s: %s", attempt, REQUEST_MAX_RETRIES, exc)
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("Solana Tracker (Pump.fun) request failed on attempt %s/%s: %s", attempt, REQUEST_MAX_RETRIES, exc)

        if attempt < REQUEST_MAX_RETRIES:
            time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    raise PumpFunClientError(f"Failed to fetch {url} after {REQUEST_MAX_RETRIES} attempt(s): {last_error}")


def _extract_items(payload):
    """Defensive against the exact response wrapper shape -- see the
    module docstring's NOTE ON VERIFICATION. Accepts a bare list, or a
    dict wrapping the list under a common key.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "tokens", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    logger.error("Unexpected Pump.fun search payload shape: %s", type(payload))
    return []


def fetch_latest_launch_addresses(limit=None):
    """Return a list of recently-created Pump.fun token (mint) addresses,
    newest first. Returns [] (never None/raises) if unconfigured,
    unreachable, rate-limited past retries, or the payload is malformed
    -- src.radar.run_radar() treats this exactly like an empty watchlist,
    never as a reason to fail the cycle.
    """
    if not is_configured():
        return []

    limit = PUMPFUN_DISCOVERY_LIMIT if limit is None else limit
    params = {"sortBy": "createdAt", "sortOrder": "desc", "market": "pumpfun", "limit": limit}

    try:
        payload = _request_with_retries(SEARCH_URL, params)
    except PumpFunClientError as exc:
        logger.error("Could not fetch Pump.fun latest launches: %s", exc)
        return []

    items = _extract_items(payload)
    addresses = [item["mint"] for item in items if isinstance(item, dict) and item.get("mint")]

    logger.info("Discovered %s Pump.fun launch address(es)", len(addresses))
    return addresses
