"""Pre-trade safety screening for live trading.

This is deliberately separate from and stricter than the general radar
filter in src/config.py (MIN_LIQUIDITY_USD etc.) -- those control what
gets scored and shown on screen; MIN_LIVE_* here controls what real money
is allowed to touch.

None of this is a guarantee. Meme coins can still lose most or all of
their value even when every check here passes -- these are heuristics
that catch some common failure modes (razor-thin liquidity, brand-new
pairs in the highest-risk rug window, tokens that cannot be sold), not a
substitute for the fact that this is inherently high-risk speculation.
"""

import logging
from dataclasses import dataclass, field

from src.config import (
    MAX_LIVE_PAIR_AGE_MINUTES,
    MIN_LIVE_LIQUIDITY_USD,
    MIN_LIVE_PAIR_AGE_MINUTES,
    MIN_LIVE_VOLUME_24H_USD,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskAssessment:
    passed: bool
    reasons: list = field(default_factory=list)


def assess_token_safety(evaluated_pair, round_trip_result=None):
    """evaluated_pair is one result dict from radar.evaluate_pair().
    round_trip_result is the dict returned by
    jupiter_client.round_trip_check(), or None if it has not been run
    (treated as a failure -- we do not buy anything we have not checked
    for sellability).
    """
    reasons = []

    liquidity = evaluated_pair.get("liquidity")
    volume = evaluated_pair.get("volume")
    age = evaluated_pair.get("age")
    buys = evaluated_pair.get("buys") or 0
    sells = evaluated_pair.get("sells") or 0

    if liquidity is None or liquidity < MIN_LIVE_LIQUIDITY_USD:
        reasons.append(f"liquidity {liquidity} below live minimum {MIN_LIVE_LIQUIDITY_USD}")

    if volume is None or volume < MIN_LIVE_VOLUME_24H_USD:
        reasons.append(f"24h volume {volume} below live minimum {MIN_LIVE_VOLUME_24H_USD}")

    if age is None:
        reasons.append("pair age unknown")
    elif age < MIN_LIVE_PAIR_AGE_MINUTES:
        reasons.append(
            f"pair is only {age:.1f}m old -- inside the "
            f"{MIN_LIVE_PAIR_AGE_MINUTES}m minimum rug-risk window"
        )
    elif age > MAX_LIVE_PAIR_AGE_MINUTES:
        reasons.append(f"pair is {age:.1f}m old -- past the {MAX_LIVE_PAIR_AGE_MINUTES}m live window")

    if buys + sells == 0:
        reasons.append("no recorded trades in the last 24h")

    if round_trip_result is None:
        reasons.append("sellability was not checked (no round-trip quote result)")
    elif not round_trip_result.get("sellable"):
        reasons.append(f"sellability check failed: {round_trip_result.get('reason')}")
    elif round_trip_result.get("reason"):
        # sellable=True but still flagged (e.g. round-trip loss too high)
        reasons.append(round_trip_result["reason"])

    passed = not reasons
    if not passed:
        logger.info(
            "Token %s failed live safety screening: %s",
            evaluated_pair.get("symbol", "?"), "; ".join(reasons),
        )

    return RiskAssessment(passed=passed, reasons=reasons)
