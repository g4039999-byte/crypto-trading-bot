"""Per-account reputation for X sources, learned from actual outcomes --
not follower count, not verification badge, not anything about the
account itself. An account starts neutral; every time one of its
mentions was part of a signal that later got a paper trade, its weight
moves up on a win and down on a loss (or on a signal that never led
anywhere). This is deliberately the *only* input: "did this account's
past signals actually precede real, profitable moves".

State lives in data/x_account_reputation.json, isolated from every
other project state file. Never raises out of its public functions --
a corrupt/missing state file is treated as "everyone starts neutral",
never a crash.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "x_account_reputation.json"

NEUTRAL_WEIGHT = 1.0
MIN_WEIGHT = 0.1
MAX_WEIGHT = 2.0

# Exponential-moving-average smoothing for each outcome -- recent
# results matter more than old ones (an account that used to be good
# but has gone stale/compromised should drift back toward neutral, not
# stay permanently trusted off old history), but one bad or good call
# doesn't swing the weight wildly either.
_EMA_ALPHA = 0.25


def _load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s -- treating as empty: %s", STATE_FILE, exc)
        return {}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_weight(author_id):
    """[MIN_WEIGHT, MAX_WEIGHT], NEUTRAL_WEIGHT (1.0) for any account
    with no recorded outcome yet -- an unknown account is not
    distrusted, and being unknown is never itself a negative signal
    (a brand-new-to-us account can still be the very first to spot a
    real trend).
    """
    if not author_id:
        return NEUTRAL_WEIGHT
    state = _load_state()
    record = state.get(author_id)
    return record["weight"] if record else NEUTRAL_WEIGHT


def record_outcome(author_id, was_useful, context=None):
    """Fold one outcome into author_id's running reputation. was_useful
    is a bool (or a float in [-1, 1] for a graded outcome, e.g. paper
    trade PnL normalized) -- True/1.0 means this account's signal
    preceded something real and good; False/-1.0 means it didn't
    (no real move, or a paper trade that lost). context is optional
    free-form info (e.g. {"entity": "PEPITO", "trade_pnl_usd": -1.4})
    kept purely for later inspection/debugging, not used in the math.
    """
    if not author_id:
        return NEUTRAL_WEIGHT

    signal = 1.0 if was_useful is True else (-1.0 if was_useful is False else float(was_useful))
    signal = max(-1.0, min(1.0, signal))

    state = _load_state()
    record = state.setdefault(author_id, {
        "weight": NEUTRAL_WEIGHT, "outcomes_recorded": 0, "history": [],
    })

    # Map the [-1, 1] signal onto a weight delta around NEUTRAL_WEIGHT,
    # then EMA-smooth it into the running weight.
    target = NEUTRAL_WEIGHT + signal * (MAX_WEIGHT - NEUTRAL_WEIGHT if signal > 0 else NEUTRAL_WEIGHT - MIN_WEIGHT)
    record["weight"] = round(max(MIN_WEIGHT, min(MAX_WEIGHT, (1 - _EMA_ALPHA) * record["weight"] + _EMA_ALPHA * target)), 4)
    record["outcomes_recorded"] += 1
    record["history"] = (record["history"] + [{
        "signal": signal, "context": context, "recorded_at": datetime.now(timezone.utc).isoformat(),
    }])[-50:]

    _save_state(state)
    return record["weight"]


def top_accounts(limit=10, min_outcomes=1):
    """Accounts with the highest learned weight, for a "who's actually
    useful" view -- e.g. the dashboard or a report. Excludes accounts
    with fewer than min_outcomes recorded results (a weight based on
    one data point isn't worth ranking yet).
    """
    state = _load_state()
    ranked = [
        {"author_id": aid, **rec} for aid, rec in state.items()
        if rec.get("outcomes_recorded", 0) >= min_outcomes
    ]
    ranked.sort(key=lambda r: r["weight"], reverse=True)
    return ranked[:limit]
