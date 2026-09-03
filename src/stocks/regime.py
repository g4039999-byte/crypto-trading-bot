"""Market regime classification -- rule-based (transparent,
reproducible, no ML model to overfit or go stale silently), based on
the broad-market reference symbol (MARKET_REGIME_SYMBOL, default SPY).

Used by src.stocks.engine to adjust filters/risk per cycle (e.g. wider
stops and fewer new entries in a high-volatility risk-off regime)
instead of applying the same rules regardless of market conditions.
"""

import logging

import pandas as pd

from src.stocks.features import compute_features, pct_change_over, sma, true_range
from src.stocks.data_provider import get_provider

logger = logging.getLogger(__name__)

REGIME_BULLISH = "BULLISH"
REGIME_BEARISH = "BEARISH"
REGIME_SIDEWAYS = "SIDEWAYS"

VOLATILITY_HIGH = "HIGH"
VOLATILITY_LOW = "LOW"

# ATR% (of price) on the regime symbol above this = HIGH volatility
# regime; SPY's own ATR% is normally low single digits, so this
# threshold is deliberately much lower than a single meme-coin-grade
# stock's own ATR% thresholds in src/stocks/config.py.
_HIGH_VOLATILITY_ATR_PCT = 1.5


def classify_regime(features):
    """features is compute_features()'s output for the regime symbol.
    Returns {"trend": BULLISH|BEARISH|SIDEWAYS, "volatility": HIGH|LOW,
    "risk_appetite": "risk-on"|"risk-off"}. Falls back to the most
    conservative reading (SIDEWAYS/HIGH volatility/risk-off) whenever
    there isn't enough data to judge -- an unknown market is treated as
    a cautious one, not a permissive one.
    """
    pct_change_20d = features.get("pct_change_20d")
    above_sma50 = features.get("above_sma50")
    atr_pct = features.get("atr_pct")

    if pct_change_20d is None or above_sma50 is None:
        trend = REGIME_SIDEWAYS
    elif pct_change_20d > 3 and above_sma50:
        trend = REGIME_BULLISH
    elif pct_change_20d < -3 and not above_sma50:
        trend = REGIME_BEARISH
    else:
        trend = REGIME_SIDEWAYS

    if atr_pct is None:
        volatility = VOLATILITY_HIGH  # unknown -- assume the cautious case
    else:
        volatility = VOLATILITY_HIGH if atr_pct >= _HIGH_VOLATILITY_ATR_PCT else VOLATILITY_LOW

    risk_appetite = "risk-off" if (trend == REGIME_BEARISH or volatility == VOLATILITY_HIGH) else "risk-on"

    return {"trend": trend, "volatility": volatility, "risk_appetite": risk_appetite,
            "pct_change_20d": pct_change_20d, "atr_pct": atr_pct}


def compute_regime_series(df):
    """The same classify_regime() logic as a per-date Series over an
    ENTIRE historical DataFrame at once, computed with plain vectorized
    pandas operations rather than calling compute_features()/
    classify_regime() once per day (which would redo an O(n) rolling
    computation n times -- O(n^2) total -- for no benefit, since every
    input classify_regime() needs is itself already a simple rolling
    series). Used by src.stocks.research_pipeline to tag thousands of
    historical backtest trades with "what was the market regime on this
    trade's entry date" cheaply, as one precomputation per research run
    rather than per trade.

    Returns a DataFrame indexed like `df`, columns: trend, volatility,
    risk_appetite -- identical values to what classify_regime(features)
    would produce for a features dict computed as of that same date.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["trend", "volatility", "risk_appetite"])

    close = df["close"]
    pct_20d = pct_change_over(df, 20)
    sma50 = sma(close, 50)
    above_sma50 = close > sma50
    atr14 = true_range(df).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    atr_pct = (atr14 / close * 100)

    trend = pd.Series(REGIME_SIDEWAYS, index=df.index)
    trend = trend.where(~((pct_20d > 3) & above_sma50), REGIME_BULLISH)
    trend = trend.where(~((pct_20d < -3) & ~above_sma50), REGIME_BEARISH)
    # Rows with no signal yet (NaN pct_20d/sma50) fall back to SIDEWAYS,
    # matching classify_regime()'s own "not enough data -> SIDEWAYS" rule.
    trend = trend.where(pct_20d.notna() & above_sma50.notna(), REGIME_SIDEWAYS)

    volatility = pd.Series(VOLATILITY_HIGH, index=df.index)
    volatility = volatility.where(~(atr_pct < _HIGH_VOLATILITY_ATR_PCT), VOLATILITY_LOW)
    volatility = volatility.where(atr_pct.notna(), VOLATILITY_HIGH)  # unknown -- cautious, same as classify_regime()

    risk_appetite = pd.Series("risk-on", index=df.index)
    risk_off_mask = (trend == REGIME_BEARISH) | (volatility == VOLATILITY_HIGH)
    risk_appetite = risk_appetite.where(~risk_off_mask, "risk-off")

    return pd.DataFrame({"trend": trend, "volatility": volatility, "risk_appetite": risk_appetite})


def current_regime(symbol=None):
    """Live regime read using the configured data provider. Never
    raises: a data-fetch failure yields the same conservative fallback
    classify_regime() itself returns for missing data, not an
    exception -- the caller (src.stocks.engine) never has to special-
    case "regime detection is down".
    """
    from src.stocks.config import MARKET_REGIME_SYMBOL

    symbol = symbol or MARKET_REGIME_SYMBOL
    try:
        df = get_provider().get_daily_bars(symbol, lookback_days=120)
        features = compute_features(df)
        regime = classify_regime(features)
        regime["symbol"] = symbol
        return regime
    except Exception:
        logger.exception("Regime detection failed for %s -- defaulting to cautious", symbol)
        fallback = classify_regime({})
        fallback["symbol"] = symbol
        return fallback


def risk_multiplier(regime):
    """A single scaling factor (0-1) src.stocks.risk_engine applies to
    position sizing based on regime -- smaller size in a risk-off
    regime, full size in risk-on. Deliberately simple (2 tiers) rather
    than a continuous function that would be harder to reason about.
    """
    return 0.5 if regime.get("risk_appetite") == "risk-off" else 1.0
