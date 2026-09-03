"""Technical/price-action feature engineering. Pure functions over a
pandas DataFrame shaped like src.stocks.data_provider's output (columns
open/high/low/close/volume, one row per bar, oldest first) -- no I/O,
no network, trivially unit-testable with a synthetic DataFrame.

compute_features() is the one entry point src.stocks.scoring and the
strategy modules actually use; the indicator functions above it are
exposed individually too (each strategy uses a handful directly, e.g.
breakout only needs the rolling high/low, not RSI).
"""

import numpy as np
import pandas as pd


def sma(series, period):
    return series.rolling(window=period, min_periods=period).mean()


def ema(series, period):
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series, period=14):
    """Wilder's RSI, 0-100. NaN wherever there isn't enough history yet."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.where(avg_loss != 0, 100.0)  # zero average loss -- purely up moves -- is RSI 100, not NaN/inf


def true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df, period=14):
    """Average True Range, Wilder-smoothed -- the standard volatility
    measure src.stocks.risk_engine sizes stops/targets off of, so exits
    scale with how much a stock actually moves instead of a fixed %.
    """
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def vwap(df):
    """Volume-weighted average price, cumulative from the start of
    `df` -- meaningful for a single intraday session's bars (reset the
    input to that session's bars before calling this), not a rolling
    multi-day figure.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_pv = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum().replace(0, np.nan)
    return cumulative_pv / cumulative_vol


def relative_volume(df, lookback=20):
    """Latest bar's volume vs. the average of the preceding `lookback`
    bars (excludes the latest bar itself, so a huge print today doesn't
    inflate its own baseline).
    """
    avg = df["volume"].shift(1).rolling(window=lookback, min_periods=lookback).mean()
    return df["volume"] / avg.replace(0, np.nan)


def pct_change_over(df, periods):
    return df["close"].pct_change(periods=periods) * 100


def compute_features(df):
    """Everything the scoring/strategy layer needs about the LATEST bar
    in `df`, as a flat dict of plain floats (None where there isn't
    enough history). df must have at least ~50 rows for every feature
    to be non-None; fewer rows just means some come back None -- this
    never raises on a short history, it degrades gracefully (a symbol
    that just IPO'd, or a truncated data fetch).
    """
    if df is None or df.empty or len(df) < 2:
        return _empty_features()

    close = df["close"]
    latest = df.iloc[-1]

    atr14 = atr(df, 14)
    rvol = relative_volume(df, 20)
    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    ema20 = ema(close, 20)
    rsi14 = rsi(close, 14)

    high_20 = df["high"].rolling(window=20, min_periods=20).max()
    low_20 = df["low"].rolling(window=20, min_periods=20).min()

    def _last_or_none(series):
        if series is None or series.empty:
            return None
        value = series.iloc[-1]
        return None if pd.isna(value) else float(value)

    price = float(latest["close"])
    atr_value = _last_or_none(atr14)

    return {
        "price": price,
        "volume": float(latest["volume"]),
        "atr": atr_value,
        "atr_pct": (atr_value / price * 100) if atr_value and price else None,
        "relative_volume": _last_or_none(rvol),
        "sma20": _last_or_none(sma20),
        "sma50": _last_or_none(sma50),
        "ema20": _last_or_none(ema20),
        "above_sma20": bool(price > sma20.iloc[-1]) if not sma20.empty and not pd.isna(sma20.iloc[-1]) else None,
        "above_sma50": bool(price > sma50.iloc[-1]) if not sma50.empty and not pd.isna(sma50.iloc[-1]) else None,
        # EMA20 slope over the last 3 bars, as a % of price -- positive
        # means the short-term trend line is still rising (a real
        # pullback happens *within* a rising trend, not after it's
        # already rolled over), not just "price is near the line".
        "ema20_slope_pct": float((ema20.iloc[-1] - ema20.iloc[-4]) / price * 100) if len(ema20) >= 4 and not pd.isna(ema20.iloc[-1]) and not pd.isna(ema20.iloc[-4]) and price else None,
        "pct_from_ema20": float((price - ema20.iloc[-1]) / ema20.iloc[-1] * 100) if not ema20.empty and not pd.isna(ema20.iloc[-1]) else None,
        "rsi14": _last_or_none(rsi14),
        "pct_change_1d": _last_or_none(pct_change_over(df, 1)),
        "pct_change_5d": _last_or_none(pct_change_over(df, 5)),
        "pct_change_20d": _last_or_none(pct_change_over(df, 20)),
        "high_20d": _last_or_none(high_20),
        "low_20d": _last_or_none(low_20),
        "pct_from_20d_high": float((price - high_20.iloc[-1]) / high_20.iloc[-1] * 100) if not high_20.empty and not pd.isna(high_20.iloc[-1]) else None,
        "pct_from_20d_low": float((price - low_20.iloc[-1]) / low_20.iloc[-1] * 100) if not low_20.empty and not pd.isna(low_20.iloc[-1]) else None,
        "spread_pct": float((latest["high"] - latest["low"]) / price * 100) if price else None,
    }


def _empty_features():
    keys = (
        "price", "volume", "atr", "atr_pct", "relative_volume", "sma20", "sma50", "ema20",
        "above_sma20", "above_sma50", "ema20_slope_pct", "pct_from_ema20", "rsi14", "pct_change_1d", "pct_change_5d",
        "pct_change_20d", "high_20d", "low_20d", "pct_from_20d_high", "pct_from_20d_low", "spread_pct",
    )
    return {k: None for k in keys}
