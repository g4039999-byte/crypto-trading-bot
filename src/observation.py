from snapshot import load_snapshots


def analyze_observation(token_address):
    snapshots = load_snapshots(token_address)

    if len(snapshots) < 2:
        return {"status": "INSUFFICIENT_DATA"}

    previous = snapshots[-2]
    current = snapshots[-1]

    def pct_change(old, new):
        if old is None or new is None or old == 0:
            return 0.0
        return ((new - old) / old) * 100

    old_price = float(previous["price_usd"]) if previous["price_usd"] else None
    new_price = float(current["price_usd"]) if current["price_usd"] else None

    price_change = pct_change(old_price, new_price)

    old_liq = previous.get("liquidity_usd")
    new_liq = current.get("liquidity_usd")
    liquidity_change = pct_change(old_liq, new_liq)

    buys = max(
        0,
        (current.get("buys_24h") or 0) - (previous.get("buys_24h") or 0)
    )
    sells = max(
        0,
        (current.get("sells_24h") or 0) - (previous.get("sells_24h") or 0)
    )

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
