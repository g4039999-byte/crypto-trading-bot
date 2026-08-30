def calculate_score(pair):
    score = 0

    liquidity = pair.get("liquidity", {}).get("usd") or 0
    volume = pair.get("volume", {}).get("h24") or 0
    change = pair.get("priceChange", {}).get("h24") or 0

    txns = pair.get("txns", {}).get("h24", {})
    buys = txns.get("buys", 0) or 0
    sells = txns.get("sells", 0) or 0

    if liquidity >= 5000:
        score += 25

    if volume >= 25000:
        score += 20

    if buys > sells:
        score += 20

    if buys >= 1000:
        score += 15

    if change > 0:
        score += 10

    if change >= 100:
        score += 10

    return min(score, 100)
