"""Pre-trade safety screening, shared by live and paper trading.

This is deliberately separate from and stricter than the general radar
filter in src/config.py (MIN_LIQUIDITY_USD etc.) -- those control what
gets scored and shown on screen; the liquidity/volume/age bars here
control what a trade (real or paper) is allowed to touch. The honeypot/
sellability check (round_trip_result) is never optional or weakened for
either mode -- only the liquidity/volume/age bars can be overridden by
the caller (src/paper_trader.py passes its own, more permissive PAPER_*
values from src/config.py; src/live_trader.py passes none and gets the
exact MIN_LIVE_* behavior it always has).

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


def assess_token_safety(
    evaluated_pair,
    round_trip_result=None,
    *,
    min_liquidity_usd=None,
    min_volume_24h_usd=None,
    min_pair_age_minutes=None,
    max_pair_age_minutes=None,
):
    """evaluated_pair is one result dict from radar.evaluate_pair().
    round_trip_result is the dict returned by
    jupiter_client.round_trip_check(), or None if it has not been run
    (treated as a failure -- we do not buy anything we have not checked
    for sellability). The honeypot/sellability check itself is never
    optional or overridable -- only the liquidity/volume/age bars are,
    so a caller can screen (e.g. src/paper_trader.py, with its own
    PAPER_MIN_* config) without touching src/live_trader.py's call,
    which passes none of these and gets the exact MIN_LIVE_* behavior it
    always has.
    """
    min_liquidity_usd = MIN_LIVE_LIQUIDITY_USD if min_liquidity_usd is None else min_liquidity_usd
    min_volume_24h_usd = MIN_LIVE_VOLUME_24H_USD if min_volume_24h_usd is None else min_volume_24h_usd
    min_pair_age_minutes = MIN_LIVE_PAIR_AGE_MINUTES if min_pair_age_minutes is None else min_pair_age_minutes
    max_pair_age_minutes = MAX_LIVE_PAIR_AGE_MINUTES if max_pair_age_minutes is None else max_pair_age_minutes

    reasons = []

    liquidity = evaluated_pair.get("liquidity")
    volume = evaluated_pair.get("volume")
    age = evaluated_pair.get("age")
    buys = evaluated_pair.get("buys") or 0
    sells = evaluated_pair.get("sells") or 0

    if liquidity is None or liquidity < min_liquidity_usd:
        reasons.append(f"liquidity {liquidity} below minimum {min_liquidity_usd}")

    if volume is None or volume < min_volume_24h_usd:
        reasons.append(f"24h volume {volume} below minimum {min_volume_24h_usd}")

    if age is None:
        reasons.append("pair age unknown")
    elif age < min_pair_age_minutes:
        reasons.append(
            f"pair is only {age:.1f}m old -- inside the "
            f"{min_pair_age_minutes}m minimum rug-risk window"
        )
    elif age > max_pair_age_minutes:
        reasons.append(f"pair is {age:.1f}m old -- past the {max_pair_age_minutes}m window")

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
            "Token %s failed safety screening: %s",
            evaluated_pair.get("symbol", "?"), "; ".join(reasons),
        )

    return RiskAssessment(passed=passed, reasons=reasons)
