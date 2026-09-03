"""Turn raw X posts into trend/meme "signal clusters": one entity
(cashtag, hashtag, or bare meme-looking name) with everyone who has
mentioned it recently, how fast that's growing, and how much it's worth
trusting.

Pure data processing -- no network calls (src/x_client.py owns those)
and no trading decisions (src/x_correlation.py links a cluster to an
actual token; scoring is src/scoring.py's business). State lives in
data/x_signals.json, isolated from every other state file in this
project the same way data/paper_positions.json is isolated from
data/positions.json.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import X_MIN_INDEPENDENT_MENTIONS, X_SIGNAL_TTL_MINUTES

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "x_signals.json"

# Cashtags ($PEPE), hashtags (#pepe), and bare all-caps tokens 2-10
# chars that look like a ticker mentioned without a prefix (e.g. "PEPE
# just launched") -- the last one is noisier (catches ordinary acronyms
# too) so it requires a nearby meme/coin keyword to count, checked in
# extract_entities() below rather than the regex itself.
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
_HASHTAG_RE = re.compile(r"#([A-Za-z][A-Za-z0-9]{1,19})\b")
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{2,10})\b")
_MEME_CONTEXT_RE = re.compile(r"\b(coin|token|meme|gem|launch|pump|moon|airdrop|sol|solana)\b", re.IGNORECASE)

# Loosely spam-shaped text (near-duplicate airdrop/giveaway bait) --
# down-weighted, never silently dropped, so a human reviewing
# data/x_signals.json can still see what was filtered and why.
_SPAM_MARKERS_RE = re.compile(
    r"\b(free\s+claim|airdrop\s+now|dm\s+me|click\s+link|guaranteed\s+profit|100x\s+guaranteed)\b",
    re.IGNORECASE,
)

MAX_MENTIONS_PER_ENTITY = 300  # trimmed like src/snapshot.py trims price history


def normalize_entity(raw):
    return raw.strip().lstrip("$#").upper()


def extract_entities(text):
    """Every candidate entity name mentioned in one post's text, as
    normalized (uppercase, no $/#) strings, deduplicated within that
    single post.
    """
    if not text:
        return []

    found = set()
    for m in _CASHTAG_RE.finditer(text):
        found.add(normalize_entity(m.group(1)))
    for m in _HASHTAG_RE.finditer(text):
        found.add(normalize_entity(m.group(1)))

    if _MEME_CONTEXT_RE.search(text):
        for m in _BARE_TICKER_RE.finditer(text):
            candidate = normalize_entity(m.group(1))
            # Common English acronyms/words that would otherwise flood
            # every cluster with noise -- a short, deliberately
            # maintainable denylist rather than a full dictionary check.
            if candidate not in _COMMON_WORD_DENYLIST:
                found.add(candidate)

    return sorted(found)


_COMMON_WORD_DENYLIST = {
    "THE", "AND", "FOR", "ARE", "NOT", "YOU", "ALL", "NEW", "CAN", "NOW",
    "GET", "OUT", "WHO", "HOW", "WHY", "USD", "USDC", "USDT", "SOL", "BTC", "ETH",
    "API", "RPC", "CEO", "CTO", "NFT", "DEX", "CEX", "ATH",
    "FREE", "CLAIM", "DM", "LINK", "GUARANTEED", "PROFIT", "AIRDROP",
}


def is_probable_spam(text):
    return bool(_SPAM_MARKERS_RE.search(text or ""))


def _load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s -- starting fresh: %s", STATE_FILE, exc)
        return {}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def update_signal_state(posts, query=None):
    """Fold a batch of posts (src.x_client.search_recent()'s output
    shape) into the persisted entity-cluster state. Never raises --
    a malformed post is skipped, not fatal to the batch.

    Returns the list of entity keys touched by this batch (for the
    caller to log/inspect without re-reading the whole file).
    """
    state = _load_state()
    touched = []

    for post in posts or []:
        try:
            text = post.get("text") or ""
            entities = extract_entities(text)
            if not entities:
                continue

            spam = is_probable_spam(text)
            record = {
                "post_id": post.get("id"),
                "author_id": post.get("author_id"),
                "author_username": post.get("author_username"),
                "created_at": post.get("created_at"),
                "text_snippet": text[:140],
                "engagement": (post.get("like_count") or 0) + (post.get("retweet_count") or 0) * 2,
                "query": query,
                "is_probable_spam": spam,
            }

            for entity in entities:
                cluster = state.setdefault(entity, {"first_seen_at": record["created_at"] or _now_iso(), "mentions": []})
                existing_ids = {m.get("post_id") for m in cluster["mentions"]}
                if record["post_id"] in existing_ids:
                    continue
                cluster["mentions"].append(record)
                cluster["mentions"] = cluster["mentions"][-MAX_MENTIONS_PER_ENTITY:]
                cluster["last_seen_at"] = record["created_at"] or _now_iso()
                touched.append(entity)
        except Exception:
            logger.exception("Skipping one malformed X post while updating signal state")

    _save_state(state)
    return sorted(set(touched))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _independent_author_mentions(cluster):
    """Non-spam mentions, deduplicated to at most one per author (a
    single account posting the same ticker 20 times is one opinion, not
    20 confirmations) -- this is what mention/velocity counts below are
    actually based on.
    """
    seen_authors = set()
    result = []
    for m in cluster.get("mentions", []):
        if m.get("is_probable_spam"):
            continue
        author = m.get("author_id")
        if author and author in seen_authors:
            continue
        if author:
            seen_authors.add(author)
        result.append(m)
    return result


def compute_velocity(cluster, window_minutes=15):
    """Independent mentions per minute in the most recent window --
    the core "is this accelerating right now" signal."""
    mentions = _independent_author_mentions(cluster)
    if not mentions:
        return 0.0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    recent = [m for m in mentions if (_parse_ts(m.get("created_at")) or cutoff) >= cutoff]
    return len(recent) / window_minutes


def cluster_summary(entity, cluster, reputation_lookup=None):
    """One entity's current stats, independent of any specific token
    correlation. reputation_lookup(author_id) -> float in [0, 2] (1.0 =
    neutral/unknown, see src/x_account_reputation.py); defaults to
    always-neutral if not given.
    """
    reputation_lookup = reputation_lookup or (lambda _author_id: 1.0)
    mentions = _independent_author_mentions(cluster)
    independent_authors = len({m.get("author_id") for m in mentions if m.get("author_id")})
    velocity = compute_velocity(cluster)
    avg_reputation = (
        sum(reputation_lookup(m.get("author_id")) for m in mentions) / len(mentions)
        if mentions else 1.0
    )
    total_spam = sum(1 for m in cluster.get("mentions", []) if m.get("is_probable_spam"))
    spam_ratio = total_spam / len(cluster["mentions"]) if cluster.get("mentions") else 0.0

    # Confidence in [0, 1]: rewards independent confirmation and
    # velocity, moderated by source quality, punished by a high spam
    # ratio. Deliberately simple and inspectable rather than a black box.
    confidence = 0.0
    if independent_authors >= X_MIN_INDEPENDENT_MENTIONS:
        confidence = min(1.0, (independent_authors / 8) * 0.5 + min(velocity, 2.0) / 2.0 * 0.3 + (avg_reputation / 2) * 0.2)
    confidence *= max(0.0, 1 - spam_ratio)

    return {
        "entity": entity,
        "independent_mentions": independent_authors,
        "total_mentions": len(cluster.get("mentions", [])),
        "velocity_per_minute": round(velocity, 3),
        "avg_source_reputation": round(avg_reputation, 3),
        "spam_ratio": round(spam_ratio, 3),
        "confidence": round(confidence, 3),
        "first_seen_at": cluster.get("first_seen_at"),
        "last_seen_at": cluster.get("last_seen_at"),
    }


def active_trends(reputation_lookup=None, min_confidence=0.0):
    """Every entity cluster still inside X_SIGNAL_TTL_MINUTES of its
    last mention, summarized and sorted by confidence descending. Does
    NOT mutate/prune the persisted state (that's the caller's call, via
    prune_stale_clusters(), so read-only callers -- e.g. the dashboard
    -- never accidentally delete history).
    """
    state = _load_state()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=X_SIGNAL_TTL_MINUTES)
    results = []
    for entity, cluster in state.items():
        last_seen = _parse_ts(cluster.get("last_seen_at"))
        if last_seen is None or last_seen < cutoff:
            continue
        summary = cluster_summary(entity, cluster, reputation_lookup)
        if summary["confidence"] >= min_confidence:
            results.append(summary)
    return sorted(results, key=lambda s: s["confidence"], reverse=True)


def get_cluster_authors(entity):
    """Every distinct, non-spam author_id who has mentioned `entity` --
    used to close the learning loop: when a paper trade tied to this
    entity closes, src.paper_trader records that outcome against each
    of these authors via src.x_account_reputation.record_outcome().
    """
    state = _load_state()
    cluster = state.get(entity)
    if not cluster:
        return []
    return sorted({
        m.get("author_id") for m in cluster.get("mentions", [])
        if m.get("author_id") and not m.get("is_probable_spam")
    })


def prune_stale_clusters():
    """Remove entity clusters that have had no mention in over
    X_SIGNAL_TTL_MINUTES * 4 (well past "active", kept a while longer in
    case correlation/learning wants recent history) -- keeps
    data/x_signals.json from growing forever. Safe to call any time;
    never touches an entity that's had a recent mention.
    """
    state = _load_state()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=X_SIGNAL_TTL_MINUTES * 4)
    kept = {}
    removed = 0
    for entity, cluster in state.items():
        last_seen = _parse_ts(cluster.get("last_seen_at"))
        if last_seen is not None and last_seen >= cutoff:
            kept[entity] = cluster
        else:
            removed += 1
    if removed:
        _save_state(kept)
    return removed
