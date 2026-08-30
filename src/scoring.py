import time


def calculate_score(pair):
    liquidity = pair.get("liquidity", {}).get("usd")
    volume = pair.get("volume", {}).get("h24")
    change = pair.get("priceChange", {}).get("h24")

    txns = pair.get("txns", {}).get("h24", {})
    buys = txns.get("buys", 0) or 0
    sells = txns.get("sells", 0) or 0

    pair_created = pair.get("pairCreatedAt")
    age_minutes = (
        (time.time() * 1000 - pair_created) / 60000
        if pair_created
        else None
    )

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