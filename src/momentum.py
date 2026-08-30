def calculate_momentum(pair):
    liquidity = pair.get("liquidity", {}).get("usd") or 0
    volume_24h = pair.get("volume", {}).get("h24") or 0
    change_24h = pair.get("priceChange", {}).get("h24") or 0

    txns = pair.get("txns", {}).get("h24", {})
    buys = txns.get("buys", 0) or 0
    sells = txns.get("sells", 0) or 0

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