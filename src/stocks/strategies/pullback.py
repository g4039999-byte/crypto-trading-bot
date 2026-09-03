"""Pullback: price pulls back to touch (or briefly dip just under) its
own rising 20-day EMA inside an intact uptrend, then buy that dip --
the classic "buy the pullback to the rising average" pattern, widely
documented in technical-analysis practice (Minervini/O'Neil-style
trend trading). Deliberately distinct from mean_reversion.py: this
wants a SHALLOW, short-lived dip to a still-rising short-term average
in an otherwise healthy uptrend, not an RSI-oversold, multi-day
capitulation -- the two strategies are meant to fire on genuinely
different setups, not just re-derive each other with different numbers.
"""

NAME = "pullback"
TIMEFRAME = "daily"

# How close to EMA20 counts as "at the pullback" -- a band, not a single
# price, since real bars rarely close exactly on the line. Negative
# values allow a brief dip slightly under the average without it
# reading as a trend break.
MAX_DISTANCE_BELOW_EMA20_PCT = -1.5
MAX_DISTANCE_ABOVE_EMA20_PCT = 1.0
MIN_EMA20_SLOPE_PCT = 0.3  # the average itself must still be rising, not flat/rolling over
MIN_RSI = 40.0  # not already oversold (that's mean_reversion's job)
MAX_RSI = 65.0  # not still overbought/extended either


def generate_signal(features, df=None):
    price = features.get("price")
    sma50 = features.get("sma50")
    pct_from_ema20 = features.get("pct_from_ema20")
    ema20_slope_pct = features.get("ema20_slope_pct")
    rsi14 = features.get("rsi14")

    if None in (price, sma50, pct_from_ema20, ema20_slope_pct, rsi14):
        return {"action": "SKIP", "reason": "not enough history for pullback features", "confidence": 0.0}

    if price <= sma50:
        return {"action": "SKIP", "reason": "below its own 50d average -- this is a downtrend, not a pullback", "confidence": 0.0}

    if ema20_slope_pct < MIN_EMA20_SLOPE_PCT:
        return {"action": "SKIP", "reason": f"20d EMA isn't clearly rising (slope {ema20_slope_pct:.2f}%) -- no healthy trend to buy the dip in", "confidence": 0.0}

    if not (MAX_DISTANCE_BELOW_EMA20_PCT <= pct_from_ema20 <= MAX_DISTANCE_ABOVE_EMA20_PCT):
        return {"action": "SKIP", "reason": f"{pct_from_ema20:.1f}% from the 20d EMA -- not at the pullback zone", "confidence": 0.0}

    if not (MIN_RSI <= rsi14 <= MAX_RSI):
        return {"action": "SKIP", "reason": f"RSI {rsi14:.0f} outside the {MIN_RSI:.0f}-{MAX_RSI:.0f} pullback band", "confidence": 0.0}

    # Confidence rewards being closer to the EMA (the "cleanest" pullback
    # entries) and a more clearly-rising trend line.
    proximity = 1.0 - min(1.0, abs(pct_from_ema20) / max(abs(MAX_DISTANCE_BELOW_EMA20_PCT), MAX_DISTANCE_ABOVE_EMA20_PCT))
    trend_strength = min(1.0, ema20_slope_pct / 2.0)
    confidence = round(min(1.0, 0.6 * proximity + 0.4 * trend_strength), 3)

    return {
        "action": "BUY",
        "reason": f"pullback to a rising 20d EMA ({pct_from_ema20:+.1f}% from it, slope {ema20_slope_pct:+.2f}%, RSI {rsi14:.0f}) in an intact uptrend",
        "confidence": confidence,
    }
