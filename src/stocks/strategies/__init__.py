"""Pluggable entry strategies. Each module exposes:

    NAME: str
    generate_signal(features, df=None) -> {"action": "BUY"|"SKIP",
        "reason": str, "confidence": float in [0, 1]}

`features` is src.stocks.features.compute_features()'s output for the
symbol's latest bar; `df` (optional) is the full bars DataFrame, for
strategies that need more than the latest snapshot (e.g. VWAP reclaim
looking at the last few bars). A strategy never raises on missing data
-- None feature values just fail its own condition checks, resulting
in SKIP, same defensive convention as the rest of this project.

Nothing here is a gate on anything else: src.stocks.scoring runs every
registered strategy against every candidate and lets the data (see
src.stocks.backtester) say which one(s) actually work, rather than the
code hard-committing to a single "the" strategy.
"""

from src.stocks.strategies import breakout, mean_reversion, momentum, pullback, relative_volume, vwap_reclaim

STRATEGIES = {
    momentum.NAME: momentum,
    breakout.NAME: breakout,
    mean_reversion.NAME: mean_reversion,
    pullback.NAME: pullback,
    relative_volume.NAME: relative_volume,
    vwap_reclaim.NAME: vwap_reclaim,
}


def evaluate_all(features, df=None):
    """Every strategy's verdict for one candidate, as {name: signal}."""
    results = {}
    for name, module in STRATEGIES.items():
        try:
            results[name] = module.generate_signal(features, df)
        except Exception:
            results[name] = {"action": "SKIP", "reason": f"{name} raised while evaluating -- treated as no signal", "confidence": 0.0}
    return results
