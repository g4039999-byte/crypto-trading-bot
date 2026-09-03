import time

from src.utils import safe_get


def calculate_score(pair, age_minutes=None):
    """Score 0-100 based on liquidity, volume, buy pressure, momentum and
    pair age. Defensive against missing/null fields -- returns 0 when
    there isn't enough data (no liquidity or volume figure) to score.

    age_minutes is normally computed from pair["pairCreatedAt"] against
    the current wall-clock time (the live radar's case). Pass it
    explicitly to score a *historical* point in time instead -- e.g.
    scripts/backtest_paper_strategy.py replays old snapshots, where
    "now" must be the snapshot's own timestamp, not whenever the replay
    happens to run.
    """
    if not isinstance(pair, dict):
        return 0

    liquidity = safe_get(pair, "liquidity", "usd")
    volume = safe_get(pair, "volume", "h24")
    change = safe_get(pair, "priceChange", "h24")

    buys = safe_get(pair, "txns", "h24", "buys", default=0) or 0
    sells = safe_get(pair, "txns", "h24", "sells", default=0) or 0

    if age_minutes is None:
        pair_created = pair.get("pairCreatedAt")
        if isinstance(pair_created, (int, float)):
            age_minutes = (time.time() * 1000 - pair_created) / 60000

    if liquidity is None or volume is None:
        return 0

    score = 0

    # Liquidity
    if liquidity >= 5000:
        score += 20

    if liquidity >= 10000:
        score += 5

    if liquidity >= 25000:
        score += 5

    # Volume
    if volume >= 25000:
        score += 10

    if volume >= 100000:
        score += 5

    if volume >= 500000:
        score += 5

    # Buy pressure
    total_trades = buys + sells

    if total_trades:
        buy_ratio = buys / total_trades

        if buy_ratio >= 0.50:
            score += 10

        if buy_ratio >= 0.60:
            score += 10

        if buy_ratio >= 0.70:
            score += 5

    # Price momentum
    if change is not None:
        if change > 0:
            score += 5

        if change >= 25:
            score += 5

        if change >= 100:
            score += 5

        if change > 300:
            score -= 5

    # Early-stage bonus
    if age_minutes is not None:
        if age_minutes <= 15 and liquidity >= 5000:
            score += 10

        elif age_minutes <= 60 and liquidity >= 5000:
            score += 5

    return max(0, min(score, 100))
