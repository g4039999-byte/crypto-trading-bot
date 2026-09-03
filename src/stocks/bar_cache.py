"""Local disk cache for daily bars, sitting in front of
src.stocks.data_provider. Historical research (src.stocks.
research_pipeline, scripts/research_stocks_strategies.py) re-runs the
same symbol/lookback combination repeatedly while iterating on
strategies/parameters -- without this, every run re-downloads years of
daily bars for dozens of symbols from yfinance, which is both slow and
an easy way to trip a rate limit for no benefit (the data hasn't
changed since this morning's close).

Design, deliberately simple over clever: one CSV per symbol under
data/stocks/cache/bars/, plus a tiny JSON sidecar recording how many
days were REQUESTED the last time this symbol was fetched (not how many
rows came back -- see why below). A cache hit requires BOTH "fresh
enough" (file mtime within STOCKS_BAR_CACHE_TTL_HOURS -- daily bars
only change once a day, at market close, so there's no reason to ever
refetch more than once a day) AND "the cached fetch asked for at least
as much history as this call needs" (via the sidecar, NOT the actual
row count).

That distinction matters for young listings: a stock that IPO'd 3 years
ago will always return fewer rows than a 10-year lookback request asks
for, no matter how many times you refetch it -- comparing actual row
count against the NEW request's lookback_days would treat that as a
permanent cache miss and refetch it on every single call, defeating the
cache for exactly the symbols in a mixed-age universe (large legacy
tickers alongside recent IPOs like the crypto miners/recent listings in
STOCKS_UNIVERSE) that most need it during a parameter-grid research run
hitting the same universe repeatedly. Comparing against what was
REQUESTED instead means a stock's genuinely-short history gets fetched
once, cached, and reused just like any other symbol.

A miss re-fetches that symbol's FULL requested lookback_days fresh (no
incremental merge with old data -- merge logic is where subtle
date-alignment bugs live; a clean overwrite is trivially correct) and
overwrites both the CSV and its sidecar. A failed/empty fetch is never
cached, so a transient data-provider hiccup gets retried next call
instead of being remembered as "this symbol has no data" forever.

Never raises: a cache read/write failure just falls back to fetching
fresh, exactly like every other resilience convention in this project.
"""

import json
import logging
import time
from pathlib import Path

import pandas as pd

from src.stocks.config import STOCKS_BAR_CACHE_TTL_HOURS

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "cache" / "bars"


def _cache_path(symbol):
    # Sanitize -- symbols are already validated tickers (letters/digits/.-)
    # from a fixed universe, but never trust a filename built from data.
    safe = "".join(c for c in symbol.upper() if c.isalnum() or c in ".-_")
    return CACHE_DIR / f"{safe}.csv"


def _meta_path(symbol):
    return _cache_path(symbol).with_suffix(".meta.json")


def _read_cached(symbol, lookback_days):
    path = _cache_path(symbol)
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    if age_hours > STOCKS_BAR_CACHE_TTL_HOURS:
        return None

    requested_at_cache_time = None
    meta_path = _meta_path(symbol)
    if meta_path.exists():
        try:
            requested_at_cache_time = json.loads(meta_path.read_text(encoding="utf-8")).get("requested_lookback_days")
        except (json.JSONDecodeError, OSError):
            pass

    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    except (OSError, ValueError, pd.errors.ParserError):
        logger.warning("Bar cache for %s is unreadable -- refetching", symbol)
        return None
    if df.empty:
        return None

    # A hit requires the PREVIOUS fetch to have asked for at least as
    # much history as this call needs -- not that it actually got that
    # many rows back (a young listing never will, no matter how many
    # times it's refetched; see the module docstring). Falling back to
    # comparing len(df) when there's no sidecar (e.g. an older cache
    # file from before this field existed) is the conservative default.
    if requested_at_cache_time is not None:
        if requested_at_cache_time < lookback_days:
            return None
    elif len(df) < lookback_days:
        return None

    return df.tail(lookback_days) if lookback_days else df


def _write_cache(symbol, df, requested_lookback_days):
    if df is None or df.empty:
        return  # never cache a failure/empty result -- see module docstring
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(_cache_path(symbol))
        _meta_path(symbol).write_text(json.dumps({"requested_lookback_days": requested_lookback_days}), encoding="utf-8")
    except OSError:
        logger.warning("Could not write bar cache for %s -- continuing uncached", symbol)


def get_daily_bars_batch_cached(provider, symbols, lookback_days):
    """Same contract as provider.get_daily_bars_batch(): {symbol: DataFrame}.
    Serves whatever symbols have a fresh-enough, long-enough-requested
    cache entry from disk; batch-fetches (and caches) only the rest.
    """
    symbols = list(symbols)
    result = {}
    missing = []

    for symbol in symbols:
        cached = _read_cached(symbol, lookback_days)
        if cached is not None:
            result[symbol] = cached
        else:
            missing.append(symbol)

    if missing:
        logger.info("Bar cache: %s/%s symbol(s) served from disk, fetching %s fresh", len(symbols) - len(missing), len(symbols), len(missing))
        fetched = provider.get_daily_bars_batch(missing, lookback_days)
        for symbol, df in fetched.items():
            result[symbol] = df
            _write_cache(symbol, df, lookback_days)

    return result


def clear_cache():
    """Delete every cached bar file (and its sidecar) -- used by tests
    and by an explicit "force a completely fresh research run"
    invocation. Never raises.
    """
    if not CACHE_DIR.exists():
        return
    for path in list(CACHE_DIR.glob("*.csv")) + list(CACHE_DIR.glob("*.meta.json")):
        try:
            path.unlink()
        except OSError:
            pass
