"""Thin, resilient client for the DexScreener public endpoints.

This module owns every network call the radar makes. Keeping it separate
from radar.py means the scoring/analysis pipeline can be tested without
touching the network, and all retry/timeout/error handling for the
external API lives in exactly one place.

No API key is required by DexScreener today. If that changes, read the key
from src.config.DEXSCREENER_API_KEY (sourced from the environment / .env)
-- never hard-code one here.
"""

import logging
import time

import requests

from src.config import (
    MAX_ADDRESSES_PER_REQUEST,
    REQUEST_MAX_RETRIES,
    REQUEST_RETRY_BACKOFF_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"


class DexClientError(Exception):
    """Raised when the DexScreener API cannot be reached or returns
    something the radar cannot make sense of, after retries are exhausted.
    """


def _request_with_retries(url):
    """GET url, retrying transient failures with a short backoff.

    Raises DexClientError if every attempt fails. Never raises a raw
    requests exception -- callers only need to handle DexClientError.
    """
    last_error = None

    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            last_error = exc
            # Client errors (4xx) other than 429 will not fix themselves
            # on retry -- fail fast instead of hammering the API.
            if status is not None and 400 <= status < 500 and status != 429:
                logger.error("DexScreener returned HTTP %s for %s (not retrying)", status, url)
                break
            logger.warning(
                "DexScreener HTTP error on attempt %s/%s: %s",
                attempt, REQUEST_MAX_RETRIES, exc,
            )
        except (requests.exceptions.RequestException, ValueError) as exc:
            # ValueError covers response.json() failing on bad payloads.
            last_error = exc
            logger.warning(
                "DexScreener request failed on attempt %s/%s: %s",
                attempt, REQUEST_MAX_RETRIES, exc,
            )

        if attempt < REQUEST_MAX_RETRIES:
            time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    raise DexClientError(f"Failed to fetch {url} after {REQUEST_MAX_RETRIES} attempt(s): {last_error}")


def fetch_solana_token_addresses():
    """Return the list of Solana token addresses from the latest token
    profiles feed. Returns an empty list (never None) if the feed is
    empty or malformed in a way that yields no usable addresses.
    """
    try:
        profiles = _request_with_retries(PROFILES_URL)
    except DexClientError as exc:
        logger.error("Could not fetch token profiles: %s", exc)
        return []

    if not isinstance(profiles, list):
        logger.error("Unexpected token-profiles payload shape: %s", type(profiles))
        return []

    addresses = [
        item["tokenAddress"]
        for item in profiles
        if isinstance(item, dict)
        and item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]

    logger.info("Discovered %s Solana token address(es) from the profiles feed", len(addresses))
    return addresses


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_pairs(addresses):
    """Fetch market pair data for a list of token addresses.

    DexScreener's /tokens/v1 endpoint accepts a limited number of
    addresses per call (MAX_ADDRESSES_PER_REQUEST), so this batches the
    request transparently instead of silently dropping the rest. A single
    failed batch is logged and skipped -- it does not abort the others.
    """
    if not addresses:
        return []

    pairs = []

    for batch in _chunk(addresses, MAX_ADDRESSES_PER_REQUEST):
        url = TOKENS_URL.format(addresses=",".join(batch))
        try:
            batch_pairs = _request_with_retries(url)
        except DexClientError as exc:
            logger.error(
                "Skipping a batch of %s address(es) after repeated failures: %s",
                len(batch), exc,
            )
            continue

        if not isinstance(batch_pairs, list):
            logger.warning("Unexpected pairs payload shape for a batch: %s", type(batch_pairs))
            continue

        pairs.extend(batch_pairs)

    logger.info("Fetched %s market pair(s) across %s batch(es)", len(pairs), -(-len(addresses) // MAX_ADDRESSES_PER_REQUEST))
    return pairs
