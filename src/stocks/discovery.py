"""Universe scan + first-pass filter: which of STOCKS_UNIVERSE
(src/stocks/config.py) are even worth scoring/evaluating this cycle.

Mirrors src/radar.py's _passes_first_filter() role on the crypto side:
liquidity/volatility/volume floors decide what gets looked at further,
not what gets traded (src.stocks.risk_engine and the strategies decide
that).
"""

import logging

from src.stocks.config import (
    STOCKS_MAX_ATR_PCT,
    STOCKS_MAX_PRICE_USD,
    STOCKS_MAX_SPREAD_PCT,
    STOCKS_MIN_ATR_PCT,
    STOCKS_MIN_AVG_VOLUME,
    STOCKS_MIN_PRICE_USD,
    STOCKS_MIN_RELATIVE_VOLUME,
    STOCKS_UNIVERSE,
)
from src.stocks.data_provider import get_provider
from src.stocks.features import compute_features

logger = logging.getLogger(__name__)


def passes_first_filter(features):
    price = features.get("price")
    if price is None or not (STOCKS_MIN_PRICE_USD <= price <= STOCKS_MAX_PRICE_USD):
        return False, f"price {price} outside [{STOCKS_MIN_PRICE_USD}, {STOCKS_MAX_PRICE_USD}]"

    atr_pct = features.get("atr_pct")
    if atr_pct is None or not (STOCKS_MIN_ATR_PCT <= atr_pct <= STOCKS_MAX_ATR_PCT):
        return False, f"ATR% {atr_pct} outside [{STOCKS_MIN_ATR_PCT}, {STOCKS_MAX_ATR_PCT}] (too quiet or too wild)"

    spread_pct = features.get("spread_pct")
    if spread_pct is not None and spread_pct > STOCKS_MAX_SPREAD_PCT * 10:
        # A *huge* single-bar range (not the normal ATR-based volatility
        # check above) usually means a halt/reopen or bad print, not a
        # tradeable liquidity condition -- 10x the configured spread
        # guard as a sanity ceiling, not the primary filter.
        return False, f"single-bar spread {spread_pct:.1f}% looks like a data anomaly or halt"

    return True, None


def scan_universe(symbols=None, lookback_days=200):
    """Fetch bars + features for the whole universe in one batched call
    (src.stocks.data_provider.get_daily_bars_batch), apply the first-
    pass filter, and return {symbol: {"features": {...}, "df": DataFrame}}
    for every symbol that passed. A symbol with unavailable/malformed
    data is logged and skipped, never fatal to the scan.
    """
    symbols = list(symbols or STOCKS_UNIVERSE)
    provider = get_provider()

    try:
        bars_by_symbol = provider.get_daily_bars_batch(symbols, lookback_days=lookback_days)
    except Exception:
        logger.exception("Batch bar fetch failed entirely -- returning no candidates this cycle")
        return {}

    passed = {}
    for symbol in symbols:
        df = bars_by_symbol.get(symbol)
        if df is None or df.empty or len(df) < 55:  # need ~50 bars for SMA50/ATR14 to be meaningful
            continue
        try:
            features = compute_features(df)
        except Exception:
            logger.exception("Feature computation failed for %s -- skipping", symbol)
            continue

        ok, reason = passes_first_filter(features)
        if not ok:
            logger.debug("%s filtered out: %s", symbol, reason)
            continue

        passed[symbol] = {"features": features, "df": df}

    logger.info("Stocks universe scan: %s/%s symbol(s) passed the first-pass filter", len(passed), len(symbols))
    return passed
