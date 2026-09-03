"""Thin, resilient client for Alpaca's paper-trading + market-data REST
API. Mirrors src/x_client.py's shape: one module owns the network
calls, every call is retried with backoff, and every public function
degrades to a safe default (None/[]) rather than raising, so a broker
outage can never take down the discovery/scoring/paper-trading loop
that calls it.

PAPER TRADING ONLY. ALPACA_TRADING_BASE_URL (src/stocks/config.py) is
hard-set to https://paper-api.alpaca.markets and is not configurable --
this module has no code path that can reach api.alpaca.markets (the
live-money base URL). Alpaca's paper account is free once you have one;
nothing here has ever been connected to a real account in this project.

Auth: APCA-API-KEY-ID / APCA-API-SECRET-KEY headers (Alpaca's own
convention) built from ALPACA_API_KEY / ALPACA_API_SECRET, read from
the environment only -- see src/stocks/config.py. Never logged, never
printed, never returned by any function here.
"""

import logging
import time

import requests

from src.stocks.config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE_URL,
    ALPACA_ENABLED,
    ALPACA_REQUEST_MAX_RETRIES,
    ALPACA_REQUEST_RETRY_BACKOFF_SECONDS,
    ALPACA_REQUEST_TIMEOUT_SECONDS,
    ALPACA_TRADING_BASE_URL,
)

logger = logging.getLogger(__name__)


def is_configured():
    """True only if both an API key and secret are set AND the client
    isn't explicitly disabled. Every function below checks this first
    and short-circuits to a safe default without touching the network.
    """
    return bool(ALPACA_API_KEY) and bool(ALPACA_API_SECRET) and ALPACA_ENABLED


def _headers():
    return {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}


def _request(method, url, params=None, json_body=None):
    """One HTTP call, retried with backoff. Returns the parsed JSON
    body, or None if every attempt failed, the endpoint returned no
    content, or a 4xx (other than 429) makes retrying pointless. Never
    raises.
    """
    last_error = None
    for attempt in range(1, ALPACA_REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.request(
                method, url, headers=_headers(), params=params, json=json_body,
                timeout=ALPACA_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 429:
                logger.warning("Alpaca rate-limited (attempt %s/%s) for %s", attempt, ALPACA_REQUEST_MAX_RETRIES, url)
                if attempt < ALPACA_REQUEST_MAX_RETRIES:
                    time.sleep(ALPACA_REQUEST_RETRY_BACKOFF_SECONDS * attempt * 2)
                last_error = "rate limited"
                continue
            if response.status_code == 401 or response.status_code == 403:
                logger.error("Alpaca returned %s -- check ALPACA_API_KEY/ALPACA_API_SECRET. Not retrying.", response.status_code)
                return None
            if 400 <= response.status_code < 500:
                logger.warning("Alpaca returned HTTP %s for %s (not retrying): %s", response.status_code, url, response.text[:200])
                return None
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("Alpaca request failed on attempt %s/%s: %s", attempt, ALPACA_REQUEST_MAX_RETRIES, exc)
            if attempt < ALPACA_REQUEST_MAX_RETRIES:
                time.sleep(ALPACA_REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    logger.error("Alpaca request to %s failed after %s attempt(s): %s", url, ALPACA_REQUEST_MAX_RETRIES, last_error)
    return None


# --- Account / trading (paper) -------------------------------------------

def get_account():
    """Paper account snapshot (equity, cash, buying_power, ...), or None."""
    if not is_configured():
        return None
    return _request("GET", f"{ALPACA_TRADING_BASE_URL}/v2/account")


def list_positions():
    """Currently open positions on the paper account, or [] if
    unconfigured/unavailable. This project tracks its own paper
    positions locally (src.stocks.paper_broker) regardless -- this is
    for cross-checking/mirroring against the real paper account, not a
    dependency of the core loop.
    """
    if not is_configured():
        return []
    result = _request("GET", f"{ALPACA_TRADING_BASE_URL}/v2/positions")
    return result if isinstance(result, list) else []


def submit_paper_order(symbol, qty, side, order_type="market", time_in_force="day"):
    """Submit an order to the PAPER account. side is "buy" or "sell".
    Returns the order dict, or None on failure/if unconfigured -- the
    caller (src.stocks.paper_broker) always has its own local
    simulation as the source of truth either way, so a failure here
    never blocks a paper "trade" from being recorded.
    """
    if not is_configured():
        return None
    body = {"symbol": symbol, "qty": str(qty), "side": side, "type": order_type, "time_in_force": time_in_force}
    return _request("POST", f"{ALPACA_TRADING_BASE_URL}/v2/orders", json_body=body)


# --- Market data -----------------------------------------------------------

def get_bars(symbol, timeframe="1Day", limit=200):
    """Recent OHLCV bars for one symbol. timeframe e.g. "1Day", "5Min".
    Returns a list of bar dicts (t, o, h, l, c, v), or [] on any
    failure/if unconfigured -- src.stocks.data_provider falls back to
    yfinance in that case, so this never blocks the pipeline.
    """
    if not is_configured():
        return []
    result = _request(
        "GET", f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/bars",
        params={"timeframe": timeframe, "limit": limit, "adjustment": "raw"},
    )
    if not result:
        return []
    return result.get("bars") or []


def get_snapshot(symbol):
    """Latest trade/quote/daily-bar snapshot for one symbol, or None."""
    if not is_configured():
        return None
    return _request("GET", f"{ALPACA_DATA_BASE_URL}/v2/stocks/{symbol}/snapshot")
