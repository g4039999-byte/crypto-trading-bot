"""Momentum: buy strength, confirmed by volume, while there is still
room left before it looks exhausted. Classic trend-following -- "the
trend is your friend" -- not a novel idea, a well-known, widely
documented style (see e.g. Jegadeesh & Titman's momentum literature,
and countless open-source momentum-scanner projects); this is this
project's own implementation of the general concept, not a copy of any
specific one.
"""

NAME = "momentum"
TIMEFRAME = "daily"  # backtestable on daily bars -- see src.stocks.backtester

MIN_PCT_CHANGE_20D = 5.0        # meaningful up-move over the last month
MAX_RSI = 78.0                  # avoid chasing a move already this overbought
MIN_RELATIVE_VOLUME = 1.1       # today's interest at least modestly above normal


def generate_signal(features, df=None):
    price = features.get("price")
    sma20 = features.get("sma20")
    sma50 = features.get("sma50")
    pct_20d = features.get("pct_change_20d")
    rsi14 = features.get("rsi14")
    rvol = features.get("relative_volume")

    if price is None or sma20 is None or sma50 is None or pct_20d is None or rsi14 is None:
        return {"action": "SKIP", "reason": "not enough history for momentum features", "confidence": 0.0}

    if not (price > sma20 > sma50):
        return {"action": "SKIP", "reason": "not in a confirmed uptrend (price > SMA20 > SMA50 required)", "confidence": 0.0}

    if pct_20d < MIN_PCT_CHANGE_20D:
        return {"action": "SKIP", "reason": f"20d momentum {pct_20d:.1f}% below minimum {MIN_PCT_CHANGE_20D}%", "confidence": 0.0}

    if rsi14 > MAX_RSI:
        return {"action": "SKIP", "reason": f"RSI {rsi14:.0f} too extended (>{MAX_RSI:.0f}) -- avoid chasing", "confidence": 0.0}

    if rvol is not None and rvol < MIN_RELATIVE_VOLUME:
        return {"action": "SKIP", "reason": f"relative volume {rvol:.2f}x too weak to confirm the move", "confidence": 0.0}

    # Confidence scales with how strong the move is (capped), moderated
    # down as RSI approaches the exhaustion ceiling.
    strength = min(1.0, pct_20d / 20.0)
    rsi_headroom = max(0.0, (MAX_RSI - rsi14) / MAX_RSI)
    confidence = round(min(1.0, 0.5 * strength + 0.5 * rsi_headroom), 3)

    return {
        "action": "BUY",
        "reason": f"uptrend confirmed, +{pct_20d:.1f}% over 20d, RSI {rsi14:.0f}, rel-vol {rvol:.2f}x" if rvol else f"uptrend confirmed, +{pct_20d:.1f}% over 20d, RSI {rsi14:.0f}",
        "confidence": confidence,
    }
