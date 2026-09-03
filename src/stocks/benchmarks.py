"""Baseline strategies every candidate strategy (src.stocks.backtester)
must be compared against -- a strategy is only worth adopting if it
beats these on the same historical data, not merely if it has a
positive return on its own.

- buy_and_hold: the simplest possible baseline -- if this beats the
  actual strategy, the strategy isn't adding value over doing nothing.
- simple_momentum_baseline: naive "price above its 50d average" trend
  following, no volume/RSI/confidence filtering -- what
  src.stocks.strategies.momentum needs to beat to justify its extra
  complexity.
- simple_volume_baseline: naive "volume spike -> hold N days", no
  trend/quality filtering at all -- what breakout.py needs to beat.
"""

import logging

from src.stocks.data_provider import get_provider
from src.stocks.features import compute_features, relative_volume, sma

logger = logging.getLogger(__name__)

HOLD_DAYS = 10
MIN_BARS = 55


def buy_and_hold(symbols, lookback_days=730):
    """One "trade" per symbol: hold the whole lookback window."""
    provider = get_provider()
    bars = provider.get_daily_bars_batch(list(symbols), lookback_days)
    pnl_pcts = []
    for symbol, df in bars.items():
        if df is None or len(df) < 2:
            continue
        first_close = float(df["close"].iloc[0])
        last_close = float(df["close"].iloc[-1])
        if first_close > 0:
            pnl_pcts.append((last_close - first_close) / first_close * 100)
    return pnl_pcts


def simple_momentum_baseline(symbols, lookback_days=730, hold_days=HOLD_DAYS):
    """Buy whenever price crosses above its 50d SMA (no other filter),
    hold a fixed number of days, sell -- deliberately naive.
    """
    provider = get_provider()
    bars = provider.get_daily_bars_batch(list(symbols), lookback_days)
    pnl_pcts = []
    for symbol, df in bars.items():
        if df is None or len(df) < MIN_BARS + hold_days:
            continue
        sma50 = sma(df["close"], 50)
        i = MIN_BARS
        while i < len(df) - hold_days - 1:
            if sma50.iloc[i] is not None and not _isna(sma50.iloc[i]) and not _isna(sma50.iloc[i - 1]):
                crossed_above = df["close"].iloc[i] > sma50.iloc[i] and df["close"].iloc[i - 1] <= sma50.iloc[i - 1]
                if crossed_above:
                    entry = float(df["open"].iloc[i + 1])
                    exit_price = float(df["close"].iloc[i + 1 + hold_days])
                    if entry > 0:
                        pnl_pcts.append((exit_price - entry) / entry * 100)
                    i += hold_days + 1
                    continue
            i += 1
    return pnl_pcts


def simple_volume_baseline(symbols, lookback_days=730, hold_days=HOLD_DAYS, rvol_threshold=2.0):
    """Buy whenever relative volume spikes above rvol_threshold (no
    trend/quality filter at all), hold a fixed number of days, sell.
    """
    provider = get_provider()
    bars = provider.get_daily_bars_batch(list(symbols), lookback_days)
    pnl_pcts = []
    for symbol, df in bars.items():
        if df is None or len(df) < MIN_BARS + hold_days:
            continue
        rvol = relative_volume(df, 20)
        i = MIN_BARS
        while i < len(df) - hold_days - 1:
            value = rvol.iloc[i]
            if value is not None and not _isna(value) and value >= rvol_threshold:
                entry = float(df["open"].iloc[i + 1])
                exit_price = float(df["close"].iloc[i + 1 + hold_days])
                if entry > 0:
                    pnl_pcts.append((exit_price - entry) / entry * 100)
                i += hold_days + 1
                continue
            i += 1
    return pnl_pcts


def _isna(value):
    import pandas as pd
    return pd.isna(value)
