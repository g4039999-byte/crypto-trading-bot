"""Breakout: price pushing to a new N-day high on above-average
volume. The volume confirmation is what separates this from just
"price is at a high" -- a breakout on light volume is much more likely
to fail/fade (a well-documented pattern in technical-analysis practice
going back to Darvas/O'Neil-style breakout trading), so this strategy
explicitly requires it rather than reacting to price alone.
"""

NAME = "breakout"
TIMEFRAME = "daily"

# How close to the rolling 20d high counts as "at" it (percent, so -0.5
# means within half a percent below the high, or the high itself).
NEAR_HIGH_PCT_THRESHOLD = -0.5
MIN_RELATIVE_VOLUME = 1.5


def generate_signal(features, df=None):
    price = features.get("price")
    pct_from_high = features.get("pct_from_20d_high")
    rvol = features.get("relative_volume")
    atr_pct = features.get("atr_pct")

    if price is None or pct_from_high is None or rvol is None:
        return {"action": "SKIP", "reason": "not enough history for breakout features", "confidence": 0.0}

    if pct_from_high < NEAR_HIGH_PCT_THRESHOLD:
        return {"action": "SKIP", "reason": f"{abs(pct_from_high):.1f}% below the 20d high -- not a breakout", "confidence": 0.0}

    if rvol < MIN_RELATIVE_VOLUME:
        return {"action": "SKIP", "reason": f"relative volume {rvol:.2f}x too low to confirm a real breakout (need >={MIN_RELATIVE_VOLUME}x)", "confidence": 0.0}

    # Confidence rewards a clean push through the high with strong,
    # not-insane, volume -- extremely wild volume/volatility can mean a
    # news-driven spike about to mean-revert violently, not a clean
    # continuation, so confidence is capped rather than scaling forever.
    volume_strength = min(1.0, (rvol - MIN_RELATIVE_VOLUME) / 3.0 + 0.5)
    volatility_penalty = 0.0
    if atr_pct is not None and atr_pct > 8.0:
        volatility_penalty = min(0.3, (atr_pct - 8.0) / 20.0)
    confidence = round(max(0.0, min(1.0, volume_strength - volatility_penalty)), 3)

    return {
        "action": "BUY",
        "reason": f"breaking out to a new 20d high, rel-vol {rvol:.2f}x",
        "confidence": confidence,
    }
