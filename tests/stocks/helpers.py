"""Shared synthetic-data builders for tests/stocks/*.py -- no test in
this package ever hits yfinance/Alpaca over the network; everything
here builds a deterministic pandas DataFrame shaped like
src.stocks.data_provider's output.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd


def make_bars(closes, volumes=None, start=None, high_pad=0.5, low_pad=0.5):
    """closes: list of closing prices, oldest first. Builds a plausible
    OHLCV frame around them (open = previous close, high/low padded a
    bit around the day's own open/close range) -- good enough for
    feature/strategy/backtester tests, which mostly care about the
    close/volume series and directional patterns, not tick-perfect bars.
    """
    n = len(closes)
    volumes = volumes or [1_000_000] * n
    start = start or (datetime.now(timezone.utc) - timedelta(days=n))

    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + high_pad for o, c in zip(opens, closes)]
    lows = [min(o, c) - low_pad for o, c in zip(opens, closes)]
    index = pd.DatetimeIndex([start + timedelta(days=i) for i in range(n)])

    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=index)


def uptrend_bars(n=80, start_price=100.0, daily_gain_pct=0.6, volume=1_000_000):
    """A steady uptrend with a little day-to-day noise -- price above
    both SMA20/SMA50, RSI elevated but not pinned at exactly 100 (a
    perfectly monotonic synthetic series with zero down days ever
    trades is unrealistic and maxes RSI out trivially).
    """
    rng = np.random.default_rng(3)
    closes = [start_price]
    for _ in range(n - 1):
        # Noise wide enough that some individual days are actually
        # negative (a handful of down days along the way) -- otherwise
        # a perfectly monotonic series has zero average loss and RSI
        # pins at exactly 100, which is unrealistic.
        noise = rng.uniform(-1.2, 1.2)
        closes.append(closes[-1] * (1 + (daily_gain_pct + noise) / 100))
    return make_bars(closes, volumes=[volume] * n)


def flat_bars(n=80, price=100.0, volume=1_000_000, noise=0.1):
    rng = np.random.default_rng(42)
    closes = [price + rng.uniform(-noise, noise) for _ in range(n)]
    return make_bars(closes, volumes=[volume] * n)


def downtrend_bars(n=80, start_price=100.0, daily_loss_pct=0.6, volume=1_000_000):
    closes = [start_price * (1 - daily_loss_pct / 100) ** i for i in range(n)]
    return make_bars(closes, volumes=[volume] * n)


def breakout_bars(n=80, base_price=100.0, breakout_pct=8.0, breakout_volume_mult=3.0, volume=1_000_000):
    """Flat/range-bound for most of the window, then a sharp push to a
    new high on the last bar with elevated volume.
    """
    rng = np.random.default_rng(7)
    closes = [base_price + rng.uniform(-1.0, 1.0) for _ in range(n - 1)]
    closes.append(max(closes) * (1 + breakout_pct / 100))
    volumes = [volume] * (n - 1) + [int(volume * breakout_volume_mult)]
    return make_bars(closes, volumes=volumes)


def oversold_pullback_bars(n=80, start_price=100.0, uptrend_pct=0.5, pullback_pct=6.0, volume=1_000_000):
    """A steady uptrend (keeps price above SMA50) that dips sharply on
    the last bar (RSI oversold) without breaking the longer trend.
    """
    closes = [start_price * (1 + uptrend_pct / 100) ** i for i in range(n - 1)]
    closes.append(closes[-1] * (1 - pullback_pct / 100))
    return make_bars(closes, volumes=[volume] * n)
