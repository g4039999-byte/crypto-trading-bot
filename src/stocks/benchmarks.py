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
  trend/quality filtering at all -- what relative_volume.py needs to beat.
- simple_breakout_baseline: naive "new N-day high -> hold N days", no
  volume confirmation at all -- what src.stocks.strategies.breakout
  needs to beat to justify requiring volume confirmation.

All go through src.stocks.bar_cache like src.stocks.backtester does, so
a research run comparing baselines against every strategy shares the
same cached, already-fetched bars rather than re-downloading them.
"""

import logging

from src.stocks import bar_cache
from src.stocks.data_provider import get_provider
from src.stocks.features import compute_features, relative_volume, sma

logger = logging.getLogger(__name__)

HOLD_DAYS = 10
MIN_BARS = 55


def _fetch_bars(symbols, lookback_days):
    provider = get_provider()
    return bar_cache.get_daily_bars_batch_cached(provider, list(symbols), lookback_days)


def buy_and_hold(symbols, lookback_days=730):
    """One "trade" per symbol: hold the whole lookback window."""
    bars = _fetch_bars(symbols, lookback_days)
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
    bars = _fetch_bars(symbols, lookback_days)
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
    bars = _fetch_bars(symbols, lookback_days)
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


def simple_breakout_baseline(symbols, lookback_days=730, hold_days=HOLD_DAYS, breakout_lookback=20):
    """Buy whenever price closes strictly above the PRIOR `breakout_
    lookback` days' high (no volume confirmation at all -- the
    deliberate omission that distinguishes this from src.stocks.
    strategies.breakout, and "prior days" rather than an inclusive
    rolling high so a single day's own high, which always sits above
    its own close by construction, can't make this condition
    unsatisfiable), hold a fixed number of days, sell.
    """
    bars = _fetch_bars(symbols, lookback_days)
    pnl_pcts = []
    for symbol, df in bars.items():
        if df is None or len(df) < MIN_BARS + hold_days:
            continue
        rolling_high = df["high"].shift(1).rolling(window=breakout_lookback, min_periods=breakout_lookback).max()
        i = MIN_BARS
        while i < len(df) - hold_days - 1:
            high_value = rolling_high.iloc[i]
            if high_value is not None and not _isna(high_value) and df["close"].iloc[i] > high_value:
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
