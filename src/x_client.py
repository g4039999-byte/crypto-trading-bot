"""Thin, resilient client for X's (Twitter's) public API v2 recent-
search endpoint.

Mirrors src/dex_client.py's shape (one module owns the network calls,
everything else stays testable without touching the network), plus two
things DexScreener doesn't need: a configuration gate (disabled with
zero network calls unless X_BEARER_TOKEN is set) and a hard daily
spending guard (X_MAX_READS_PER_DAY -- see src/config.py's X_* block for
why: as of 2026 X bills per post read, there is no free tier for a new
project).

Never raises out of its public functions -- every failure mode (not
configured, rate-limited, network error, malformed response, daily
budget exhausted) returns an empty result and logs what happened. This
is deliberate: src/x_intelligence.py (and, through it, the radar loop)
must never be taken down by X being unavailable, rate-limited, or not
configured at all.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.config import (
    X_API_BASE_URL,
    X_BEARER_TOKEN,
    X_ENABLED,
    X_MAX_READS_PER_DAY,
    X_RATE_LIMIT_MAX_WAIT_SECONDS,
    X_REQUEST_MAX_RETRIES,
    X_REQUEST_RETRY_BACKOFF_SECONDS,
    X_REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

USAGE_FILE = Path(__file__).resolve().parent.parent / "data" / "x_usage.json"

TWEET_FIELDS = "created_at,public_metrics,author_id"
USER_FIELDS = "username,name,public_metrics"


def is_configured():
    """True only if a bearer token is set AND the feature isn't
    explicitly disabled. Every other function below checks this first
    and short-circuits to "no signal" without touching the network --
    the whole point being that this module costs literally nothing
    (network or money) in its default, unconfigured state.
    """
    return bool(X_BEARER_TOKEN) and X_ENABLED


def _today_key():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_usage():
    if not USAGE_FILE.exists():
        return {}
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_usage(usage):
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(usage, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not persist X API usage counter: %s", exc)


def reads_used_today():
    return _load_usage().get(_today_key(), 0)


def _record_reads(count):
    if count <= 0:
        return
    usage = _load_usage()
    today = _today_key()
    usage[today] = usage.get(today, 0) + count
    # Trim to the last 7 days so this file never grows unbounded.
    usage = {k: v for k, v in usage.items() if k >= _today_key()[:7]}
    _save_usage(usage)


def _budget_remaining():
    return max(0, X_MAX_READS_PER_DAY - reads_used_today())


def _request(url, params):
    """One GET, with retries, honoring a 429's Retry-After / rate-limit-
    reset header up to X_RATE_LIMIT_MAX_WAIT_SECONDS. Returns the parsed
    JSON body, or None if every attempt failed -- never raises.
    """
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    last_error = None

    for attempt in range(1, X_REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=X_REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 429:
                wait_s = _rate_limit_wait_seconds(response)
                logger.warning(
                    "X API rate-limited (attempt %s/%s) -- waiting %.0fs before retrying",
                    attempt, X_REQUEST_MAX_RETRIES, wait_s,
                )
                if attempt < X_REQUEST_MAX_RETRIES:
                    time.sleep(wait_s)
                last_error = "rate limited"
                continue
            if response.status_code == 401:
                # Never retryable -- the token is wrong/expired. Fail
                # fast and loud rather than burning retries on it.
                logger.error("X API returned 401 Unauthorized -- check X_BEARER_TOKEN. Not retrying.")
                return None
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("X API request failed on attempt %s/%s: %s", attempt, X_REQUEST_MAX_RETRIES, exc)
            if attempt < X_REQUEST_MAX_RETRIES:
                time.sleep(X_REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    logger.error("X API request to %s failed after %s attempt(s): %s", url, X_REQUEST_MAX_RETRIES, last_error)
    return None


def _rate_limit_wait_seconds(response):
    """Prefer the server's own reset time (x-rate-limit-reset, a Unix
    timestamp) over a fixed guess, capped so one rate-limited call can
    never stall the caller much past X_RATE_LIMIT_MAX_WAIT_SECONDS --
    the next radar cycle will simply try again rather than this one
    call blocking indefinitely.
    """
    reset_header = response.headers.get("x-rate-limit-reset")
    if reset_header:
        try:
            wait = float(reset_header) - time.time()
            if wait > 0:
                return min(wait, X_RATE_LIMIT_MAX_WAIT_SECONDS)
        except ValueError:
            pass
    return min(X_REQUEST_RETRY_BACKOFF_SECONDS * 5, X_RATE_LIMIT_MAX_WAIT_SECONDS)


def search_recent(query, max_results=30):
    """Recent posts matching `query`. Returns a list of dicts:
        {id, text, created_at, author_id, author_username, like_count,
         retweet_count, reply_count}
    Always a list, never None -- empty on any failure, on a genuinely
    empty result, when not configured, or when today's read budget
    (X_MAX_READS_PER_DAY) is already spent.
    """
    if not is_configured():
        logger.debug("X client not configured (no X_BEARER_TOKEN or X_ENABLED=false) -- skipping search")
        return []

    remaining = _budget_remaining()
    if remaining <= 0:
        logger.warning("X daily read budget (%s) exhausted -- skipping search until UTC midnight", X_MAX_READS_PER_DAY)
        return []

    max_results = max(10, min(max_results, remaining, 100))  # API requires >=10

    payload = _request(
        f"{X_API_BASE_URL}/tweets/search/recent",
        {
            "query": query,
            "max_results": max_results,
            "tweet.fields": TWEET_FIELDS,
            "expansions": "author_id",
            "user.fields": USER_FIELDS,
        },
    )
    if not payload:
        return []

    posts = payload.get("data") or []
    users_by_id = {u["id"]: u for u in (payload.get("includes") or {}).get("users") or [] if isinstance(u, dict) and u.get("id")}

    _record_reads(len(posts))

    results = []
    for post in posts:
        if not isinstance(post, dict) or not post.get("id"):
            continue
        author = users_by_id.get(post.get("author_id"), {})
        metrics = post.get("public_metrics") or {}
        results.append({
            "id": post["id"],
            "text": post.get("text") or "",
            "created_at": post.get("created_at"),
            "author_id": post.get("author_id"),
            "author_username": author.get("username"),
            "like_count": metrics.get("like_count", 0),
            "retweet_count": metrics.get("retweet_count", 0),
            "reply_count": metrics.get("reply_count", 0),
        })
    return results
