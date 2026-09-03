"""Relative Volume: an unusual volume expansion (today's volume well
above its own 20-day average) with the close finishing in the upper
part of the day's range -- volume-led, not price-pattern-led. Distinct
from breakout.py: this fires on ANY strong-volume, strong-close day
regardless of whether price is anywhere near a 20-day high (breakout
requires that specifically); this one is closer to "something unusual
just happened here and buyers were in control by the close", a widely
documented standalone volume-based approach (e.g. "volume precedes
price" / unusual-volume scanners common in retail technical-analysis
practice) rather than a specific chart pattern.
"""

NAME = "relative_volume"
TIMEFRAME = "daily"

MIN_RELATIVE_VOLUME = 2.0  # meaningfully higher bar than breakout's 1.5x -- this strategy has no other filter
# Where today's close sits within today's own high-low range (1.0 = at
# the high, 0.0 = at the low) -- a volume spike that closes weak is far
# more likely distribution/a reversal than a genuine move up.
MIN_CLOSE_POSITION_IN_RANGE = 0.65


def generate_signal(features, df=None):
    rvol = features.get("relative_volume")
    price = features.get("price")
    atr_pct = features.get("atr_pct")

    if rvol is None or price is None or df is None or df.empty:
        return {"action": "SKIP", "reason": "not enough history for relative-volume features", "confidence": 0.0}

    if rvol < MIN_RELATIVE_VOLUME:
        return {"action": "SKIP", "reason": f"relative volume {rvol:.2f}x below the {MIN_RELATIVE_VOLUME}x floor", "confidence": 0.0}

    latest = df.iloc[-1]
    day_range = float(latest["high"]) - float(latest["low"])
    if day_range <= 0:
        return {"action": "SKIP", "reason": "no real intraday range on the latest bar -- can't judge close position", "confidence": 0.0}
    close_position = (float(latest["close"]) - float(latest["low"])) / day_range

    if close_position < MIN_CLOSE_POSITION_IN_RANGE:
        return {"action": "SKIP", "reason": f"closed only {close_position:.0%} up its own range on a volume spike -- looks like distribution, not accumulation", "confidence": 0.0}

    volume_strength = min(1.0, (rvol - MIN_RELATIVE_VOLUME) / 4.0 + 0.5)
    close_strength = min(1.0, (close_position - MIN_CLOSE_POSITION_IN_RANGE) / (1.0 - MIN_CLOSE_POSITION_IN_RANGE) * 0.3 + 0.7)
    volatility_penalty = min(0.3, max(0.0, (atr_pct - 8.0) / 20.0)) if atr_pct is not None else 0.0
    confidence = round(max(0.0, min(1.0, 0.6 * volume_strength + 0.4 * close_strength - volatility_penalty)), 3)

    return {
        "action": "BUY",
        "reason": f"unusual volume ({rvol:.2f}x average) closing {close_position:.0%} up its own range",
        "confidence": confidence,
    }
