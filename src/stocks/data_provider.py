"""Market data abstraction: src/stocks/discovery.py, features.py,
backtester.py etc. call get_daily_bars()/get_daily_bars_batch() from
HERE, never yfinance or alpaca_client directly -- so the underlying
source can be swapped (per the explicit "design the broker/data layer
so Alpaca can be replaced later" requirement) without touching any
consumer.

Two providers:
- AlpacaProvider: broker-grade data, needs ALPACA_API_KEY/SECRET.
- YFinanceProvider: free, no account/key needed at all, used as the
  default and as the automatic fallback if Alpaca isn't configured or
  a call to it fails. This is what makes real historical backtesting
  possible in this project without anyone configuring a broker account
  first.

get_provider() picks one per STOCKS_DATA_PROVIDER ("auto" = Alpaca if
configured else yfinance). Every function returns an empty
DataFrame/None on failure rather than raising -- a bad/missing symbol,
a network hiccup, or a fully unavailable data source must never crash
the discovery/scoring loop that calls this.
"""

import logging

import pandas as pd

from src.stocks import alpaca_client
from src.stocks.config import STOCKS_DATA_PROVIDER

logger = logging.getLogger(__name__)

_EMPTY_BARS_COLUMNS = ["open", "high", "low", "close", "volume"]


def _empty_bars():
    return pd.DataFrame(columns=_EMPTY_BARS_COLUMNS)


class YFinanceProvider:
    """Free, no-credential historical + recent OHLCV data via yfinance.
    Not officially supported by Yahoo as a public API -- widely used in
    the open-source quant-research community for exactly this
    (read-only historical bars for personal backtesting), not for
    redistribution. If Yahoo ever blocks/changes this, every function
    here degrades to an empty result, not a crash (see the try/except
    in each method) -- AlpacaProvider is the supported, ToS-clean path
    once a broker account exists.
    """

    name = "yfinance"

    def get_daily_bars(self, symbol, lookback_days=730):
        batch = self.get_daily_bars_batch([symbol], lookback_days)
        return batch.get(symbol, _empty_bars())

    def get_daily_bars_batch(self, symbols, lookback_days=730):
        if not symbols:
            return {}
        import yfinance as yf

        try:
            period = f"{max(1, lookback_days)}d"
            raw = yf.download(
                list(symbols), period=period, interval="1d",
                group_by="ticker", progress=False, threads=True, auto_adjust=True,
            )
        except Exception:
            logger.exception("yfinance daily batch download failed for %s symbol(s)", len(symbols))
            return {s: _empty_bars() for s in symbols}

        return self._split_batch(raw, symbols)

    def get_intraday_bars(self, symbol, days=5, interval="5m"):
        import yfinance as yf

        try:
            df = yf.download(symbol, period=f"{days}d", interval=interval, progress=False, auto_adjust=True)
        except Exception:
            logger.exception("yfinance intraday download failed for %s", symbol)
            return _empty_bars()
        return self._normalize_single(df)

    def get_latest_price(self, symbol):
        bars = self.get_daily_bars(symbol, lookback_days=5)
        if bars.empty:
            return None
        return float(bars["close"].iloc[-1])

    @staticmethod
    def _normalize_single(df):
        if df is None or df.empty:
            return _empty_bars()
        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df.columns = [str(c).lower() for c in df.columns]
        missing = [c for c in _EMPTY_BARS_COLUMNS if c not in df.columns]
        if missing:
            return _empty_bars()
        return df[_EMPTY_BARS_COLUMNS].dropna()

    @classmethod
    def _split_batch(cls, raw, symbols):
        out = {}
        if raw is None or raw.empty:
            return {s: _empty_bars() for s in symbols}
        for symbol in symbols:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if symbol not in raw.columns.get_level_values(0):
                        out[symbol] = _empty_bars()
                        continue
                    sub = raw[symbol]
                else:
                    sub = raw  # single-symbol download doesn't get a MultiIndex
                out[symbol] = cls._normalize_single(sub)
            except Exception:
                logger.exception("Failed to extract bars for %s from batch download", symbol)
                out[symbol] = _empty_bars()
        return out


class AlpacaProvider:
    """Broker-grade data via src.stocks.alpaca_client. Falls back to an
    empty result (never raises) if unconfigured or a call fails --
    get_provider()'s "auto" mode wraps this with a YFinanceProvider
    fallback one level up for exactly that case.
    """

    name = "alpaca"

    def get_daily_bars(self, symbol, lookback_days=730):
        bars = alpaca_client.get_bars(symbol, timeframe="1Day", limit=min(lookback_days, 1000))
        return self._normalize(bars)

    def get_daily_bars_batch(self, symbols, lookback_days=730):
        return {s: self.get_daily_bars(s, lookback_days) for s in symbols}

    def get_intraday_bars(self, symbol, days=5, interval="5m"):
        timeframe = "5Min" if interval in ("5m", "5Min") else "1Min"
        bars = alpaca_client.get_bars(symbol, timeframe=timeframe, limit=500)
        return self._normalize(bars)

    def get_latest_price(self, symbol):
        snapshot = alpaca_client.get_snapshot(symbol)
        if not snapshot:
            return None
        latest_trade = snapshot.get("latestTrade") or {}
        price = latest_trade.get("p")
        return float(price) if price is not None else None

    @staticmethod
    def _normalize(bars):
        if not bars:
            return _empty_bars()
        try:
            df = pd.DataFrame(bars)
            df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"})
            if "timestamp" in df.columns:
                df = df.set_index(pd.to_datetime(df["timestamp"]))
            missing = [c for c in _EMPTY_BARS_COLUMNS if c not in df.columns]
            if missing:
                return _empty_bars()
            return df[_EMPTY_BARS_COLUMNS].dropna()
        except Exception:
            logger.exception("Failed to normalize Alpaca bars payload")
            return _empty_bars()


class _AutoProvider:
    """Alpaca if configured, else yfinance -- and falls back to
    yfinance mid-call if an Alpaca call comes back empty (e.g. a
    transient outage), so "auto" really does mean "never blocked by one
    source being down".
    """

    name = "auto"

    def __init__(self):
        self._alpaca = AlpacaProvider()
        self._yfinance = YFinanceProvider()

    def _primary(self):
        return self._alpaca if alpaca_client.is_configured() else self._yfinance

    def get_daily_bars(self, symbol, lookback_days=730):
        df = self._primary().get_daily_bars(symbol, lookback_days)
        if df.empty and self._primary() is self._alpaca:
            df = self._yfinance.get_daily_bars(symbol, lookback_days)
        return df

    def get_daily_bars_batch(self, symbols, lookback_days=730):
        out = self._primary().get_daily_bars_batch(symbols, lookback_days)
        if self._primary() is self._alpaca:
            missing = [s for s in symbols if out.get(s, _empty_bars()).empty]
            if missing:
                out.update(self._yfinance.get_daily_bars_batch(missing, lookback_days))
        return out

    def get_intraday_bars(self, symbol, days=5, interval="5m"):
        df = self._primary().get_intraday_bars(symbol, days, interval)
        if df.empty and self._primary() is self._alpaca:
            df = self._yfinance.get_intraday_bars(symbol, days, interval)
        return df

    def get_latest_price(self, symbol):
        price = self._primary().get_latest_price(symbol)
        if price is None and self._primary() is self._alpaca:
            price = self._yfinance.get_latest_price(symbol)
        return price


def get_provider():
    """The provider the rest of src/stocks should use -- respects
    STOCKS_DATA_PROVIDER ("auto"/"alpaca"/"yfinance"). A fresh, cheap
    object each call (no state worth caching at this layer).
    """
    if STOCKS_DATA_PROVIDER == "alpaca":
        return AlpacaProvider()
    if STOCKS_DATA_PROVIDER == "yfinance":
        return YFinanceProvider()
    return _AutoProvider()
