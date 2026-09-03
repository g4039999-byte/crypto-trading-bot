"""Orchestrates X social intelligence for the radar: polls X (rate-
limited, budget-capped, no-op unless configured -- see src.x_client),
folds results into trend clusters (src.x_signal_engine), and answers
"does this specific token correlate to an active trend right now"
(src.x_correlation) weighted by learned source reputation
(src.x_account_reputation).

This is the ONLY entry point src/radar.py should import from the X_*
modules -- every function here is a hard resilience boundary: nothing
X-related can ever raise out of this module. If X is unconfigured,
down, rate-limited, or returns garbage, every function below degrades
to "no signal" and the radar/paper-trading pipeline continues exactly
as it would with this feature absent entirely.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.config import X_POLL_INTERVAL_SECONDS, X_SCORE_MAX_BONUS, X_SEARCH_QUERIES, X_MAX_RESULTS_PER_QUERY
from src.x_account_reputation import get_weight, record_outcome
from src.x_client import is_configured, search_recent
from src.x_correlation import social_score_for_token
from src.x_signal_engine import active_trends, get_cluster_authors, prune_stale_clusters, update_signal_state

logger = logging.getLogger(__name__)

POLL_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "x_poll_state.json"


def _load_poll_state():
    if not POLL_STATE_FILE.exists():
        return {}
    try:
        return json.loads(POLL_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_poll_state(state):
    try:
        POLL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        POLL_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not persist X poll state: %s", exc)


def _seconds_since_last_poll():
    last = _load_poll_state().get("last_poll_at")
    if not last:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return None


def maybe_poll_and_update():
    """Call once per radar cycle. No-ops immediately (zero network
    calls, zero cost) if X isn't configured, or if X_POLL_INTERVAL_
    SECONDS hasn't elapsed since the last real poll yet. Returns the
    number of new entity clusters touched this call (0 if it no-opped
    or nothing new was found).

    Never raises: every internal step is wrapped, so a bug here can
    reduce this feature to "no signal this cycle" at worst, never take
    down the radar loop that calls it.
    """
    try:
        if not is_configured():
            return 0

        elapsed = _seconds_since_last_poll()
        if elapsed is not None and elapsed < X_POLL_INTERVAL_SECONDS:
            return 0

        touched_total = set()
        for query in X_SEARCH_QUERIES:
            try:
                posts = search_recent(query, max_results=X_MAX_RESULTS_PER_QUERY)
                if posts:
                    touched = update_signal_state(posts, query=query)
                    touched_total.update(touched)
            except Exception:
                logger.exception("X poll failed for query %r -- continuing with the remaining queries", query)

        try:
            removed = prune_stale_clusters()
            if removed:
                logger.debug("Pruned %s stale X entity cluster(s)", removed)
        except Exception:
            logger.exception("Pruning stale X clusters failed -- non-fatal")

        _save_poll_state({"last_poll_at": datetime.now(timezone.utc).isoformat()})

        if touched_total:
            logger.info("X poll: %s entity cluster(s) updated (%s)", len(touched_total), ", ".join(sorted(touched_total)[:10]))
        return len(touched_total)
    except Exception:
        logger.exception("X intelligence poll failed entirely -- radar continues without a social signal this cycle")
        return 0


def get_active_trends():
    """Every currently-active trend cluster, reputation-weighted,
    sorted by confidence. Empty list on any failure or when X isn't
    configured -- safe to call unconditionally.
    """
    try:
        return active_trends(reputation_lookup=get_weight)
    except Exception:
        logger.exception("Could not read active X trends -- treating as none")
        return []


def social_signal_for_token(address, candidate_tokens, trend_summaries=None):
    """The strongest active X signal correlating to this specific token
    address, or None. candidate_tokens is this cycle's full evaluated-
    pairs list (used for clone-detection context in src.x_correlation).
    Pass trend_summaries (from get_active_trends()) if the caller
    already computed it this cycle, to avoid recomputing it once per
    token; otherwise it's computed fresh (also safe, just slower for a
    whole cycle's worth of tokens).
    """
    try:
        summaries = trend_summaries if trend_summaries is not None else get_active_trends()
        if not summaries:
            return None
        return social_score_for_token(address, summaries, candidate_tokens)
    except Exception:
        logger.exception("Social signal lookup failed for %s -- treating as no signal", address)
        return None


def record_trade_outcome_for_entity(entity, was_useful, context=None):
    """Close the learning loop: called by src.paper_trader when a paper
    trade that was opened on an X-correlated entity closes, so every
    contributing account's reputation (src.x_account_reputation) moves
    toward "was this actually useful" based on real outcomes -- not
    follower count, not vibes. was_useful is True/False (or a graded
    float in [-1, 1], e.g. normalized PnL). Never raises: a learning-
    update failure must never affect the trade that already happened.
    """
    try:
        authors = get_cluster_authors(entity)
        for author_id in authors:
            record_outcome(author_id, was_useful, context=context)
        return len(authors)
    except Exception:
        logger.exception("Recording trade outcome for X entity %r failed -- reputation left unchanged", entity)
        return 0


def score_bonus_for_signal(signal):
    """Points to add to a token's base score for a given social_signal_
    for_token() result (or None). Scaled by confidence, capped at
    X_SCORE_MAX_BONUS, and zeroed out entirely for anything flagged as
    a possible clone -- riding someone else's name is not a reason to
    rank a token higher.
    """
    if not signal or signal.get("is_possible_clone"):
        return 0
    return round(signal.get("confidence", 0.0) * X_SCORE_MAX_BONUS)
