"""Link an X trend/meme entity (e.g. "PEPITO", from
src.x_signal_engine) to actual Solana tokens the radar already knows
about, and flag when a match looks like it's riding an established
name's wave rather than being that token.

Pure matching logic -- no network calls, no state of its own (it reads
whatever token list the caller already has, typically this cycle's
radar results or the opportunity watchlist). Uses difflib from the
standard library for fuzzy symbol similarity -- no new dependency.
"""

import logging
from difflib import SequenceMatcher

from src.config import X_CLONE_SYMBOL_SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)


def _symbol_similarity(a, b):
    return SequenceMatcher(None, a.upper(), b.upper()).ratio()


def correlate(entity, candidate_tokens):
    """entity is a normalized string from src.x_signal_engine (e.g.
    "PEPITO"). candidate_tokens is a list of dicts with at least
    "symbol" and "address"; "liquidity" and "age" (minutes) are used
    for clone-risk heuristics when present, optional otherwise.

    Returns a list of match dicts, best first:
        {address, symbol, match_type: "exact"|"fuzzy",
         similarity: float, is_possible_clone: bool}

    Clone heuristic: a fuzzy (not exact) match is flagged as a possible
    clone only when a *different*, exact-symbol match also exists for
    the same entity and is meaningfully older/more liquid -- i.e. there
    is a plausible "original" for it to be imitating. A lone fuzzy match
    with nothing to imitate is reported as a weaker correlation, not
    accused of being a clone on no evidence.
    """
    if not entity or not candidate_tokens:
        return []

    exact, fuzzy = [], []
    for token in candidate_tokens:
        symbol = (token.get("symbol") or "").strip()
        if not symbol or symbol == "?":
            continue
        similarity = _symbol_similarity(entity, symbol)
        if symbol.upper() == entity.upper():
            exact.append({"address": token.get("address"), "symbol": symbol, "match_type": "exact",
                           "similarity": 1.0, "liquidity": token.get("liquidity"), "age": token.get("age")})
        elif similarity >= X_CLONE_SYMBOL_SIMILARITY_THRESHOLD:
            fuzzy.append({"address": token.get("address"), "symbol": symbol, "match_type": "fuzzy",
                           "similarity": round(similarity, 3), "liquidity": token.get("liquidity"), "age": token.get("age")})

    best_exact = max(exact, key=lambda m: m.get("liquidity") or 0, default=None)

    results = []
    for match in exact:
        match = dict(match)
        match["is_possible_clone"] = False
        results.append(match)

    for match in fuzzy:
        match = dict(match)
        is_clone = False
        if best_exact is not None:
            older_original = (best_exact.get("age") or 0) > (match.get("age") or 0)
            more_liquid_original = (best_exact.get("liquidity") or 0) > (match.get("liquidity") or 0)
            if older_original or more_liquid_original:
                is_clone = True
        match["is_possible_clone"] = is_clone
        results.append(match)

    for r in results:
        r.pop("liquidity", None)
        r.pop("age", None)

    results.sort(key=lambda m: (m["match_type"] == "exact", m["similarity"]), reverse=True)
    return results


def social_score_for_token(address, trend_summaries, candidate_tokens):
    """Convenience for src/radar.py: given this token's address, every
    currently-active trend summary (src.x_signal_engine.active_trends())
    and the full candidate token list (for clone context), return the
    best-matching signal for THIS token, or None if none correlates to
    it. Shape: {entity, confidence, independent_mentions,
    velocity_per_minute, is_possible_clone}.
    """
    best = None
    for summary in trend_summaries:
        matches = correlate(summary["entity"], candidate_tokens)
        for match in matches:
            if match["address"] != address:
                continue
            candidate = {
                "entity": summary["entity"],
                "confidence": summary["confidence"],
                "independent_mentions": summary["independent_mentions"],
                "velocity_per_minute": summary["velocity_per_minute"],
                "source_quality": summary["avg_source_reputation"],
                "match_type": match["match_type"],
                "is_possible_clone": match["is_possible_clone"],
            }
            if best is None or candidate["confidence"] > best["confidence"]:
                best = candidate
    return best
