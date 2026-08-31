from src.utils import safe_get


def calculate_momentum(pair):
    """Score 0-75 based on short-term momentum. Defensive against missing
    or explicitly-null fields in the pair payload (returns 0 rather than
    raising when there isn't enough data to judge momentum).
    """
    if not isinstance(pair, dict):
        return 0

    liquidity = safe_get(pair, "liquidity", "usd", default=0) or 0
    volume_24h = safe_get(pair, "volume", "h24", default=0) or 0
    change_24h = safe_get(pair, "priceChange", "h24", default=0) or 0

    buys = safe_get(pair, "txns", "h24", "buys", default=0) or 0
    sells = safe_get(pair, "txns", "h24", "sells", default=0) or 0

    if liquidity <= 0:
        return 0

    buy_ratio = buys / max(buys + sells, 1)
    volume_to_liquidity = volume_24h / liquidity

    score = 0

    if volume_to_liquidity >= 3:
        score += 15

    if volume_to_liquidity >= 10:
        score += 10

    if buy_ratio >= 0.50:
        score += 10

    if buy_ratio >= 0.60:
        score += 10

    if buy_ratio >= 0.70:
        score += 10

    if change_24h >= 25:
        score += 10

    if change_24h >= 100:
        score += 10

    if change_24h > 300:
        score -= 10

    return max(0, min(score, 75))
