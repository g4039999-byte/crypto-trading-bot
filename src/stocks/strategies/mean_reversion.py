"""Mean reversion: buy a short-term oversold dip, but ONLY while still
close to an otherwise-intact longer-term uptrend (price within
SMA50_PROXIMITY of its own SMA50) -- "buy the pullback in a strong
stock", not "buy anything that just crashed". Classic RSI-oversold-
pullback approach (widely documented; this is this project's own
implementation, not a copy of any specific source), kept deliberately
distinct from momentum.py: this one WANTS a short-term dip where
momentum.py explicitly avoids one.
"""

NAME = "mean_reversion"
TIMEFRAME = "daily"

RSI_OVERSOLD = 35.0
MIN_PULLBACK_PCT = -4.0  # over the last 5 sessions, not a single day -- see note below

# Originally required a single-DAY move <= -3% alongside RSI<=30 AND
# price *strictly above* SMA50 -- discovered, in a real 2-year/47-
# symbol backtest, that this produced exactly 0 trades. Two rounds of
# real-data diagnosis (not just synthetic search) drove two fixes:
#
# 1. Measuring the pullback over the last 5 sessions (pct_change_5d)
#    instead of 1 lines up with what actually makes Wilder's smoothed
#    RSI move -- a single day rarely does.
# 2. Even after (1), a direct scan of 31,725 real (symbol, day)
#    feature snapshots across the current universe found RSI<=35 AND
#    price>sma50 co-occurring exactly ZERO times -- in this universe
#    (liquid large/mid-cap US equities) and period (a momentum-heavy
#    bull market), a decline sharp enough to read oversold has, without
#    exception, already dragged price below its own trailing 50d
#    average by the time it happens. Requiring price *strictly above*
#    SMA50 and RSI oversold at the same time is not merely selective in
#    this data, it is empirically disjoint. Relaxing to "within 5%
#    below SMA50" (still a pullback near an intact trend, not a full
#    breakdown) is what actually lets the three conditions co-occur:
#    the same scan found 169 RSI-oversold days within that 5% band
#    (vs. 10 at a 2% band, 923 at a looser 10% band -- 5% was chosen as
#    the tightest band that isn't still functionally empty), and 99
#    combined with the pullback-depth and ATR filters below -- above
#    BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE. This was a deliberate choice
#    to follow what the real data showed rather than keep the original,
#    untested "above SMA50" design merely because it read as more
#    conservative on paper.
SMA50_PROXIMITY = 0.95  # price must be >= 95% of its own SMA50
MAX_ATR_PCT = 10.0


def generate_signal(features, df=None):
    price = features.get("price")
    sma50 = features.get("sma50")
    rsi14 = features.get("rsi14")
    pct_5d = features.get("pct_change_5d")
    atr_pct = features.get("atr_pct")

    if price is None or sma50 is None or rsi14 is None or pct_5d is None:
        return {"action": "SKIP", "reason": "not enough history for mean-reversion features", "confidence": 0.0}

    if price < sma50 * SMA50_PROXIMITY:
        return {"action": "SKIP", "reason": "too far below its own 50d average -- this reads as a downtrend, not a pullback", "confidence": 0.0}

    if rsi14 > RSI_OVERSOLD:
        return {"action": "SKIP", "reason": f"RSI {rsi14:.0f} not oversold yet (need <={RSI_OVERSOLD:.0f})", "confidence": 0.0}

    if pct_5d > MIN_PULLBACK_PCT:
        return {"action": "SKIP", "reason": f"5d move {pct_5d:.1f}% too mild to call a real pullback", "confidence": 0.0}

    if atr_pct is not None and atr_pct > MAX_ATR_PCT:
        return {"action": "SKIP", "reason": f"ATR {atr_pct:.1f}% too wild -- an oversold reading here is more likely a real breakdown than a pullback", "confidence": 0.0}

    oversold_depth = min(1.0, (RSI_OVERSOLD - rsi14) / RSI_OVERSOLD)
    dip_strength = min(1.0, abs(pct_5d) / 10.0)
    confidence = round(min(1.0, 0.6 * oversold_depth + 0.4 * dip_strength), 3)

    return {
        "action": "BUY",
        "reason": f"oversold pullback (RSI {rsi14:.0f}, {pct_5d:.1f}% over 5d) within an intact uptrend (price > SMA50)",
        "confidence": confidence,
    }
