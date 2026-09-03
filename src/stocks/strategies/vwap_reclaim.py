"""VWAP reclaim: an intraday pattern -- price dipped below the
session's volume-weighted average price and has just reclaimed it on
above-average volume. A well-known intraday day-trading concept (VWAP
as an intraday fair-value reference institutions trade around); this
project's own implementation of the general idea.

Needs `df` = TODAY's intraday bars (oldest first) -- src.stocks.engine
passes src.stocks.data_provider's get_intraday_bars() output here, not
the daily bars features.py otherwise works from. Backtesting coverage
for this one specifically is limited by how far back free intraday
history goes (see src/stocks/backtester.py's docstring) -- it is fully
usable live, just less exhaustively validated historically than the
three daily-bar strategies.
"""

from src.stocks.features import relative_volume, vwap

NAME = "vwap_reclaim"
TIMEFRAME = "intraday"  # NOT backtestable via src.stocks.backtester (daily bars) -- see module docstring

LOOKBACK_BARS = 6
MIN_RECLAIM_VOLUME_RATIO = 1.2


def generate_signal(features, df=None):
    if df is None or len(df) < LOOKBACK_BARS + 2:
        return {"action": "SKIP", "reason": "no (or too little) intraday data available for a VWAP read", "confidence": 0.0}

    try:
        session_vwap = vwap(df)
        rvol = relative_volume(df, lookback=min(20, len(df) - 1))
    except Exception:
        return {"action": "SKIP", "reason": "VWAP computation failed on this bar data", "confidence": 0.0}

    recent = df.tail(LOOKBACK_BARS + 1)
    recent_vwap = session_vwap.tail(LOOKBACK_BARS + 1)
    if recent_vwap.isna().any():
        return {"action": "SKIP", "reason": "VWAP not yet defined this early in the session", "confidence": 0.0}

    was_below = (recent["close"].iloc[:-1] < recent_vwap.iloc[:-1]).any()
    now_above = recent["close"].iloc[-1] > recent_vwap.iloc[-1]

    if not (was_below and now_above):
        return {"action": "SKIP", "reason": "no recent below-VWAP-to-above-VWAP reclaim pattern", "confidence": 0.0}

    latest_rvol = rvol.iloc[-1]
    if pd_isna(latest_rvol) or latest_rvol < MIN_RECLAIM_VOLUME_RATIO:
        return {"action": "SKIP", "reason": "reclaim not confirmed by above-average volume", "confidence": 0.0}

    distance_above = (recent["close"].iloc[-1] - recent_vwap.iloc[-1]) / recent_vwap.iloc[-1] * 100
    confidence = round(min(1.0, 0.5 + min(0.3, latest_rvol / 10.0) + min(0.2, distance_above / 5.0)), 3)

    return {
        "action": "BUY",
        "reason": f"reclaimed session VWAP on {latest_rvol:.2f}x volume, now {distance_above:.2f}% above it",
        "confidence": confidence,
    }


def pd_isna(value):
    import pandas as pd
    return pd.isna(value)
