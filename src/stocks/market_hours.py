"""Is the US stock market's regular session open right now?

Models NYSE/NASDAQ's regular session (09:30-16:00 America/New_York,
Monday-Friday) plus a fixed list of full-day federal-market-holiday
dates. Deliberately does NOT model: early-close half-days (e.g. the day
after Thanksgiving), pre-market/after-hours sessions, or a holiday
calendar that extends indefinitely (the list below is generated a few
years out and needs extending eventually) -- documented limitations,
not silent gaps: a stale/missing holiday entry fails safe by treating
that day as a normal trading day, which only costs a few wasted (cheap,
free-tier) data calls, never a missed or wrongly-gated trade decision.

Used by src.stocks.engine.run_forever() to skip full scan cycles while
the market is closed instead of burning data-provider calls on data
that hasn't moved since the last close.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_PRE_MARKET_OPEN = time(4, 0)
_AFTER_HOURS_CLOSE = time(20, 0)

STATUS_OPEN = "OPEN"
STATUS_PRE_MARKET = "PRE_MARKET"
STATUS_AFTER_HOURS = "AFTER_HOURS"
STATUS_CLOSED = "CLOSED"

# Full-day NYSE holidays -- New Year's Day, MLK Day, Presidents' Day,
# Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
# Thanksgiving, Christmas -- 2025 through 2027. Extend this list as
# those years pass; see the module docstring for the fail-safe behavior
# if it isn't.
_HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def is_market_open(now=None):
    """True only during the regular Mon-Fri 09:30-16:00 ET session on a
    non-holiday date. `now` is a timezone-aware datetime for testing;
    defaults to the real current time.
    """
    now = now.astimezone(ET) if now is not None else datetime.now(ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if now.date() in _HOLIDAYS:
        return False
    return _REGULAR_OPEN <= now.time() < _REGULAR_CLOSE


def market_status(now=None):
    """A dashboard-friendly label -- OPEN / PRE_MARKET / AFTER_HOURS /
    CLOSED -- unlike is_market_open() (used to gate real trading
    cycles, regular session only), this also distinguishes the
    surrounding extended-hours windows purely for display: this
    project's own strategies only ever act during the regular session
    (is_market_open() is what engine.py actually gates on), so
    PRE_MARKET/AFTER_HOURS here means "the market isn't closed for the
    day, but the bot isn't scanning yet/anymore" -- not that a trade
    could happen in that window.
    """
    now = now.astimezone(ET) if now is not None else datetime.now(ET)
    if now.weekday() >= 5 or now.date() in _HOLIDAYS:
        return STATUS_CLOSED
    t = now.time()
    if _REGULAR_OPEN <= t < _REGULAR_CLOSE:
        return STATUS_OPEN
    if _PRE_MARKET_OPEN <= t < _REGULAR_OPEN:
        return STATUS_PRE_MARKET
    if _REGULAR_CLOSE <= t < _AFTER_HOURS_CLOSE:
        return STATUS_AFTER_HOURS
    return STATUS_CLOSED


def seconds_until_next_open(now=None):
    """How long to sleep before it's worth checking again. Returns a
    small number (near 0) if the market is already open right now --
    callers should check is_market_open() first, this is only for
    "how long do I wait" while it's closed.
    """
    now = now.astimezone(ET) if now is not None else datetime.now(ET)
    if is_market_open(now):
        return 0.0

    # Start from today if the market hasn't opened yet today, else tomorrow.
    candidate_date = now.date() if now.time() < _REGULAR_OPEN else now.date() + timedelta(days=1)
    for _ in range(10):  # at most ~10 calendar days ahead (covers any holiday run)
        if candidate_date.weekday() < 5 and candidate_date not in _HOLIDAYS:
            open_dt = datetime.combine(candidate_date, _REGULAR_OPEN, tzinfo=ET)
            if open_dt > now:
                return (open_dt - now).total_seconds()
        candidate_date += timedelta(days=1)

    return 24 * 3600.0  # fallback -- should be unreachable given the loop above
