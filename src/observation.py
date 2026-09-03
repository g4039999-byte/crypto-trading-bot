import logging

from src.snapshot import load_snapshots

logger = logging.getLogger(__name__)


def _safe_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_change(old, new):
    if old is None or new is None or old == 0:
        return 0.0
    return ((new - old) / old) * 100


def compute_trend(previous, current):
    """Pure function: given two consecutive snapshot dicts (each shaped
    like src/snapshot.py's save_snapshot() output), return the same dict
    analyze_observation() would for that pair. No I/O, no dependency on
    "now" -- safe to call on any two historical snapshots, which is what
    lets scripts/backtest_paper_strategy.py replay old data with the
    exact trend logic the live radar uses, not a hand-copied duplicate
    that could quietly drift out of sync with this function.
    """
    old_price = _safe_float(previous.get("price_usd"))
    new_price = _safe_float(current.get("price_usd"))
    price_change = _pct_change(old_price, new_price)

    old_liq = previous.get("liquidity_usd")
    new_liq = current.get("liquidity_usd")
    liquidity_change = _pct_change(old_liq, new_liq)

    buys = max(0, (current.get("buys_24h") or 0) - (previous.get("buys_24h") or 0))
    sells = max(0, (current.get("sells_24h") or 0) - (previous.get("sells_24h") or 0))

    total = buys + sells
    buy_pressure = buys / total if total else 0.0

    if price_change > 5 and buy_pressure >= 0.60:
        trend = "STRONG"
    elif price_change > 0 and buy_pressure >= 0.50:
        trend = "RISING"
    elif price_change < -10 or buy_pressure < 0.40:
        trend = "WEAK"
    else:
        trend = "NEUTRAL"

    return {
        "status": "OK",
        "trend": trend,
        "price_change_pct": round(price_change, 2),
        "liquidity_change_pct": round(liquidity_change, 2),
        "buy_pressure": round(buy_pressure, 3),
        "new_buys": buys,
        "new_sells": sells,
    }


def analyze_observation(token_address):
    """Compare the last two snapshots for a token to gauge short-term
    trend. Never raises -- any unexpected/missing data yields a status
    the caller can branch on instead of a crash.
    """
    try:
        snapshots = load_snapshots(token_address)
    except Exception as exc:  # defensive: snapshot storage should not crash the radar
        logger.error("Could not load snapshots for %s: %s", token_address, exc)
        return {"status": "ERROR"}

    if len(snapshots) < 2:
        return {"status": "INSUFFICIENT_DATA"}

    return compute_trend(snapshots[-2], snapshots[-1])
