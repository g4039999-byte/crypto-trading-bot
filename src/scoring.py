def calculate_score(pair):
    liquidity = pair.get("liquidity", {}).get("usd")
    volume = pair.get("volume", {}).get("h24")
    change = pair.get("priceChange", {}).get("h24")
    txns = pair.get("txns", {}).get("h24", {})
    buys = txns.get("buys", 0) or 0
    sells = txns.get("sells", 0) or 0

    if liquidity is None or volume is None:
        return 0

    score = 0

    if liquidity >= 5000:
        score += 20
    if liquidity >= 10000:
        score += 5
    if liquidity >= 25000:
        score += 5

    if volume >= 25000:
        score += 10
    if volume >= 100000:
        score += 5
    if volume >= 500000:
        score += 5

    total_trades = buys + sells
    if total_trades:
        buy_ratio = buys / total_trades
        if buy_ratio >= 0.50:
            score += 10
        if buy_ratio >= 0.60:
            score += 10
        if buy_ratio >= 0.70:
            score += 5

    if change is not None:
        if change > 0:
            score += 5
        if change >= 25:
            score += 5
        if change >= 100:
            score += 5
        if change > 300:
            score -= 5

    return max(0, min(score, 100))
