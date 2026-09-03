"""Multi-factor Opportunity Score, 0-100. Combines price action +
volume + relative volume + volatility fit + the best-matching entry
strategy's own confidence + market-regime alignment + liquidity, then
adds optional, capped, never-required bonuses from social (X) and news
signals -- exactly the same "additive, never a gate" pattern
src.scoring.calculate_score() (crypto side) already established for X.

Deliberately not a single indicator's opinion: no one factor here can
push the score to a qualifying level on its own (each has a bounded
maximum contribution), and a token with a genuinely strong entry setup
(a strategy at high confidence) needs no social/news signal to score
well -- those are amplifiers on top of a real setup, not a substitute
for one.
"""

from src.stocks.config import STOCKS_X_SCORE_MAX_BONUS
from src.stocks.regime import risk_multiplier
from src.stocks.strategies import evaluate_all


def best_strategy_signal(strategy_results, active_strategy=None):
    """The signal to act on this cycle. If `active_strategy` (see
    src.stocks.strategy_registry.get_active_strategy()) names a
    strategy that has actually been formally adopted -- backtested,
    recorded, and activated -- only that strategy's verdict counts,
    exactly matching the "Strategy Selection" pipeline stage: once a
    winner has been chosen from real data, the system trades on that
    choice, not on whichever of several candidates happens to be
    loudest this cycle. With no strategy activated yet (the default,
    fresh-install state), every registered strategy is considered and
    the highest-confidence BUY wins -- so the system is still fully
    functional pre-adoption, exploring rather than committed.
    Returns None if there's no BUY under whichever rule applies.
    """
    if active_strategy:
        signal = strategy_results.get(active_strategy)
        if not signal or signal["action"] != "BUY":
            return None
        return {"strategy": active_strategy, **signal}

    buys = [(name, sig) for name, sig in strategy_results.items() if sig["action"] == "BUY"]
    if not buys:
        return None
    name, sig = max(buys, key=lambda item: item[1]["confidence"])
    return {"strategy": name, **sig}


def _relative_volume_points(rvol):
    if rvol is None:
        return 0
    return round(min(20.0, max(0.0, (rvol - 1.0) / 3.0 * 20.0)), 1)


def _volatility_fit_points(atr_pct):
    # A gentle curve peaking around 3-5% ATR -- healthy, tradeable
    # movement -- and tapering off both toward "too quiet" and toward
    # "too wild" (discovery.py's hard filter already excludes the
    # extremes; this just scores the middle ground, not a hard cutoff).
    if atr_pct is None:
        return 0
    if atr_pct <= 5.0:
        return round(min(10.0, atr_pct / 5.0 * 10.0), 1)
    return round(max(0.0, 10.0 - (atr_pct - 5.0) / 10.0 * 10.0), 1)


def _momentum_points(features):
    pct_20d = features.get("pct_change_20d")
    above_sma20 = features.get("above_sma20")
    above_sma50 = features.get("above_sma50")
    if pct_20d is None:
        return 0
    trend_bonus = 5.0 if above_sma20 else 0.0
    trend_bonus += 5.0 if above_sma50 else 0.0
    momentum_bonus = min(10.0, max(-10.0, pct_20d / 2.0))
    return round(max(0.0, trend_bonus + momentum_bonus), 1)


def _liquidity_points(features):
    spread_pct = features.get("spread_pct")
    if spread_pct is None:
        return 5.0  # unknown -- neutral, not penalized
    return round(max(0.0, 10.0 - min(10.0, spread_pct * 3)), 1)


def _regime_points(regime):
    if regime.get("risk_appetite") == "risk-on" and regime.get("trend") != "BEARISH":
        return 10.0
    if regime.get("trend") == "BEARISH":
        return 0.0
    return 5.0


def calculate_score(features, df=None, regime=None, social_bonus=0, news_bonus=0, active_strategy=None):
    """Returns {"score": int 0-100, "best_strategy": str | None,
    "strategy_confidence": float, "strategy_reason": str | None,
    "strategy_signals": {name: signal}, "components": {...}} --
    components are kept for transparency/debuggability (shown on the
    dashboard as "ranking reason"). active_strategy: see
    best_strategy_signal()'s docstring.
    """
    regime = regime or {"trend": "SIDEWAYS", "risk_appetite": "risk-on"}
    strategy_signals = evaluate_all(features, df)
    best = best_strategy_signal(strategy_signals, active_strategy=active_strategy)

    strategy_points = round((best["confidence"] if best else 0.0) * 30.0, 1)
    rvol_points = _relative_volume_points(features.get("relative_volume"))
    volatility_points = _volatility_fit_points(features.get("atr_pct"))
    momentum_points = _momentum_points(features)
    liquidity_points = _liquidity_points(features)
    regime_points = _regime_points(regime)

    social_bonus = max(0, min(STOCKS_X_SCORE_MAX_BONUS, social_bonus))
    news_bonus = max(0, min(10, news_bonus))

    raw_score = (
        strategy_points + rvol_points + volatility_points
        + momentum_points + liquidity_points + regime_points
        + social_bonus + news_bonus
    )
    score = int(round(max(0.0, min(100.0, raw_score))))

    return {
        "score": score,
        "best_strategy": best["strategy"] if best else None,
        "strategy_confidence": best["confidence"] if best else 0.0,
        "strategy_reason": best["reason"] if best else None,
        "strategy_signals": strategy_signals,
        "components": {
            "strategy": strategy_points, "relative_volume": rvol_points,
            "volatility_fit": volatility_points, "momentum": momentum_points,
            "liquidity": liquidity_points, "regime": regime_points,
            "social_bonus": social_bonus, "news_bonus": news_bonus,
        },
    }
