"""News/event signal engine: turns RawNewsEvent objects (from any
src.news_providers.NewsProvider) into classified, de-duplicated,
TTL-expiring NewsSignal records in their own state file
(data/news_signals.json).

Standalone by design: this module is NEVER imported by, and never
imports, src/wallet.py, src/risk.py, src/live_trader.py,
src/paper_trader.py, src/portfolio.py, or src/paper_portfolio.py. It
holds no position/execution state and makes no trading decision --
calling anything here has zero effect on any real or paper trade. See
tests/test_news_signal_engine.py's isolation test.

Classification is a deterministic, keyword-based heuristic -- NOT a
machine-learning or LLM model. This is intentional: it is simple,
transparent, fully reproducible, needs no API key or network call of
its own, and (per this phase's explicit constraint) a sentiment score
or an LLM's opinion must never be the sole thing deciding a trade. This
engine does not decide anything -- it only labels text with
event_type/sentiment/confidence/affected_assets/directional_bias/urgency
for a human, or a future phase, to read. A future move to an ML/LLM-
based classifier is a separate, bigger decision (cost, reliability,
API key) explicitly out of scope here.

Linking to the rest of the project is deliberately indirect and
read-only: opportunity_watchlist.attach_news_signals() (Phase 7) reads
active_signals() once per radar cycle and matches by symbol -- this
module still has no knowledge of, and no dependency on,
opportunity_watchlist.py or radar.py; the dependency direction stays
one-way, from the watchlist into this engine, never the reverse.
signals_for_symbols() and group_signals_by_asset() below are the two
read-only entry points that make that possible: NOTHING in this engine
writes to the watchlist, to positions, or to any trading state, and
nothing in the watchlist integration is ever fed back into this
engine's own classification or storage.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from src.news_providers import RawNewsEvent

logger = logging.getLogger(__name__)

# Standalone settings: read directly from the environment rather than
# importing src.config, so this module has zero dependency on that
# file's exact contents (its own defaults match what config.py would
# otherwise have provided). Override via a real .env / real env vars
# exactly as before -- only the import path changed.
NEWS_SIGNAL_TTL_MINUTES = float(os.getenv("NEWS_SIGNAL_TTL_MINUTES", "240"))
NEWS_SIGNAL_MAX_STORED = int(os.getenv("NEWS_SIGNAL_MAX_STORED", "500"))

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "news_signals.json"

EVENT_TYPES = ("LISTING", "PARTNERSHIP", "REGULATORY", "HACK_EXPLOIT", "MACRO", "GENERIC")
SENTIMENTS = ("POSITIVE", "NEGATIVE", "NEUTRAL")
DIRECTIONAL_BIASES = ("BULLISH", "BEARISH", "NEUTRAL")
URGENCIES = ("LOW", "MEDIUM", "HIGH")

_EVENT_TYPE_KEYWORDS = {
    "HACK_EXPLOIT": ("hack", "exploit", "drained", "rug pull", "rugpull", "stolen", "vulnerabilit"),
    "REGULATORY": ("sec ", "regulat", "lawsuit", " ban", "banned", "investigat", "compliance"),
    "LISTING": ("listing", "listed on", "will list", "now trading on", "launches on"),
    "PARTNERSHIP": ("partnership", "partners with", "collaborat", "integrat"),
    "MACRO": ("federal reserve", "interest rate", "inflation", "cpi ", "fomc", "macro"),
}
# Checked in this order -- HACK_EXPLOIT/REGULATORY take priority over
# LISTING/PARTNERSHIP/MACRO when a text happens to match more than one
# category's keywords (e.g. "listing under regulatory investigation").
_EVENT_TYPE_PRIORITY = ("HACK_EXPLOIT", "REGULATORY", "LISTING", "PARTNERSHIP", "MACRO")

_POSITIVE_WORDS = ("surge", "soar", "rally", "bullish", "partnership", "adopt", "growth", "record high", "breakout", "gain")
_NEGATIVE_WORDS = ("crash", "plunge", "hack", "exploit", "bearish", "lawsuit", "ban", "sell-off", "selloff", "drop", "decline", "rug pull", "rugpull", "scam")
_URGENCY_KEYWORDS = ("breaking", "urgent", "just in", "alert", "developing")

_TICKER_PATTERN = re.compile(r"\$([A-Za-z]{2,10})\b")


@dataclass
class NewsSignal:
    event_id: str
    source: str
    text: str
    published_at: Optional[str]
    url: Optional[str]
    ingested_at: str
    expires_at: str
    event_type: str
    sentiment: str
    confidence: float
    affected_assets: List[str] = field(default_factory=list)
    directional_bias: str = "NEUTRAL"
    urgency: str = "LOW"


# ---------------------------------------------------------------------------
# Classification (pure functions, no I/O)
# ---------------------------------------------------------------------------

def _classify_event_type(text):
    lowered = (text or "").lower()
    for event_type in _EVENT_TYPE_PRIORITY:
        if any(keyword in lowered for keyword in _EVENT_TYPE_KEYWORDS[event_type]):
            return event_type
    return "GENERIC"


def _classify_sentiment(text):
    """Returns (sentiment, confidence). confidence is a simple,
    transparent heuristic in [0, 1] based on how many sentiment
    keywords matched and by how much one side outweighs the other --
    NOT a probability from any statistical or ML model.
    """
    lowered = (text or "").lower()
    positive_hits = sum(1 for word in _POSITIVE_WORDS if word in lowered)
    negative_hits = sum(1 for word in _NEGATIVE_WORDS if word in lowered)
    total_hits = positive_hits + negative_hits

    if total_hits == 0:
        return "NEUTRAL", 0.0

    if positive_hits > negative_hits:
        sentiment = "POSITIVE"
    elif negative_hits > positive_hits:
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"

    margin = abs(positive_hits - negative_hits)
    confidence = min(1.0, 0.2 * total_hits + 0.2 * margin)
    return sentiment, confidence


def _directional_bias(sentiment, event_type):
    if event_type == "HACK_EXPLOIT":
        return "BEARISH"  # an exploit is bad news regardless of incidental positive wording
    if sentiment == "POSITIVE":
        return "BULLISH"
    if sentiment == "NEGATIVE":
        return "BEARISH"
    return "NEUTRAL"


def _urgency(text, event_type):
    lowered = (text or "").lower()
    if any(word in lowered for word in _URGENCY_KEYWORDS):
        return "HIGH"
    if event_type in ("HACK_EXPLOIT", "REGULATORY"):
        return "MEDIUM"
    return "LOW"


def _extract_affected_assets(text):
    """Best-effort extraction of $TICKER-style mentions, de-duplicated,
    order preserved, upper-cased. Returns [] if none found or text is
    empty/malformed -- never raises.
    """
    if not text or not isinstance(text, str):
        return []
    seen = []
    for match in _TICKER_PATTERN.findall(text):
        symbol = match.upper()
        if symbol not in seen:
            seen.append(symbol)
    return seen


def classify_event(raw_event):
    """raw_event: a src.news_providers.RawNewsEvent. Returns a dict of
    just the classification fields (event_type, sentiment, confidence,
    affected_assets, directional_bias, urgency) -- pure function, no
    I/O, no side effects. Never raises: a malformed/empty text yields
    the same neutral defaults as no signal at all.
    """
    text = getattr(raw_event, "text", None) or ""
    event_type = _classify_event_type(text)
    sentiment, confidence = _classify_sentiment(text)
    return {
        "event_type": event_type,
        "sentiment": sentiment,
        "confidence": confidence,
        "affected_assets": _extract_affected_assets(text),
        "directional_bias": _directional_bias(sentiment, event_type),
        "urgency": _urgency(text, event_type),
    }


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

def _minute_bucket(published_at):
    """Floor an ISO timestamp to the minute, for stable hashing across
    near-simultaneous re-fetches of the same event. Falls back to the
    raw string (still deterministic, just less lenient) if it doesn't
    parse.
    """
    if not published_at:
        return ""
    try:
        dt = datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        return str(published_at)
    return dt.replace(second=0, microsecond=0).isoformat()


def derive_event_id(raw_event):
    """A stable de-duplication key for raw_event. Uses the provider's
    own event_id when given (namespaced by source, so two providers
    can't collide on the same raw id); otherwise derives one from
    source + normalized text + a minute-granularity timestamp bucket,
    so the exact same headline re-fetched moments later still maps to
    the same key without needing a native id at all.
    """
    if getattr(raw_event, "event_id", None):
        return f"{raw_event.source}:{raw_event.event_id}"
    basis = f"{raw_event.source}|{(raw_event.text or '').strip().lower()}|{_minute_bucket(raw_event.published_at)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Storage (read-only-safe defaults, never raises)
# ---------------------------------------------------------------------------

def _empty_state():
    return {"signals": {}}


def load_state():
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s -- treating as empty: %s", STATE_FILE, exc)
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("signals", {})
    if not isinstance(data["signals"], dict):
        data["signals"] = {}
    return data


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def is_expired(signal_dict, now=None):
    now = now or datetime.now(timezone.utc)
    expires_at = signal_dict.get("expires_at")
    try:
        return now >= datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True  # an unparseable expiry is treated as already expired -- safer default


def _prune_expired_and_overflow(state, now=None):
    """Remove expired signals, then (if still over NEWS_SIGNAL_MAX_STORED)
    drop the oldest-by-ingested_at until back under the cap. Mutates and
    returns `state`.
    """
    now = now or datetime.now(timezone.utc)
    signals = state.get("signals", {})

    for event_id in [eid for eid, sig in signals.items() if is_expired(sig, now)]:
        del signals[event_id]

    if len(signals) > NEWS_SIGNAL_MAX_STORED:
        ordered = sorted(signals.items(), key=lambda item: item[1].get("ingested_at", ""))
        overflow = len(signals) - NEWS_SIGNAL_MAX_STORED
        for event_id, _ in ordered[:overflow]:
            del signals[event_id]

    return state


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def ingest_raw_event(state, raw_event, now=None):
    """Classify and store one RawNewsEvent into `state` (mutated in
    place). If an event with the same derived id already exists, it is
    NOT re-added or re-classified (news events, unlike opportunity
    watchlist entries, don't get "updated" -- a headline is what it is)
    -- this is what prevents duplicates. Returns the NewsSignal dict
    that is now in `state` for this event (whether newly created or
    already present).
    """
    now = now or datetime.now(timezone.utc)
    event_id = derive_event_id(raw_event)
    signals = state.setdefault("signals", {})

    if event_id in signals:
        return signals[event_id]

    classification = classify_event(raw_event)
    ttl = timedelta(minutes=NEWS_SIGNAL_TTL_MINUTES)
    signal = NewsSignal(
        event_id=event_id,
        source=getattr(raw_event, "source", "unknown") or "unknown",
        text=getattr(raw_event, "text", "") or "",
        published_at=getattr(raw_event, "published_at", None),
        url=getattr(raw_event, "url", None),
        ingested_at=now.isoformat(),
        expires_at=(now + ttl).isoformat(),
        **classification,
    )
    signal_dict = asdict(signal)
    signals[event_id] = signal_dict
    return signal_dict


def ingest_events(providers, limit_per_provider=None, now=None):
    """providers: a list of src.news_providers.NewsProvider instances.
    Fetches from each in turn, classifies and stores every new event,
    prunes expired/overflow signals, and saves once at the end.

    A provider that raises on fetch_events() is caught, logged, and
    skipped -- every other provider still runs, and ingestion never
    raises. This is what keeps a failing news source from ever being
    able to affect the radar or anything else in the project (this
    engine isn't called from the radar cycle in this phase at all, but
    the same defensive contract as every other data source in this
    project -- dex_client, pumpfun_client -- is upheld here too).

    Returns the number of NEW signals ingested this call (already-seen
    events, per derive_event_id(), don't count).
    """
    now = now or datetime.now(timezone.utc)
    state = load_state()
    new_count = 0

    for provider in providers or []:
        provider_name = getattr(provider, "name", provider.__class__.__name__)
        try:
            raw_events = provider.fetch_events(limit=limit_per_provider) or []
        except Exception:
            logger.exception("News provider '%s' failed to fetch -- skipping it this round", provider_name)
            continue

        for raw_event in raw_events:
            if not isinstance(raw_event, RawNewsEvent):
                logger.warning("Skipping a malformed event from provider '%s' (not a RawNewsEvent)", provider_name)
                continue
            event_id = derive_event_id(raw_event)
            was_new = event_id not in state.get("signals", {})
            ingest_raw_event(state, raw_event, now=now)
            if was_new:
                new_count += 1

    _prune_expired_and_overflow(state, now=now)
    save_state(state)
    return new_count


# ---------------------------------------------------------------------------
# Read-only access (for a future phase, or a person, to consume)
# ---------------------------------------------------------------------------

def active_signals(now=None):
    """Every non-expired signal currently stored, most-recently-ingested
    first. Read-only -- never called from anywhere in this project's
    radar/trading cycle in this phase (see module docstring).
    """
    now = now or datetime.now(timezone.utc)
    signals = load_state().get("signals", {}).values()
    active = [sig for sig in signals if not is_expired(sig, now)]
    active.sort(key=lambda sig: sig.get("ingested_at", ""), reverse=True)
    return active


def signals_for_symbols(symbols, now=None):
    """Active signals whose affected_assets intersects `symbols`
    (case-insensitive). Deliberately NOT called from radar.py or
    opportunity_watchlist.py in this phase -- see module docstring's
    "Linking to the rest of the project" section. `symbols` can be any
    iterable of ticker strings (e.g. from a radar result's "symbol"
    field or an opportunity watchlist entry).
    """
    wanted = {s.upper() for s in (symbols or []) if isinstance(s, str)}
    if not wanted:
        return []
    return [
        sig for sig in active_signals(now=now)
        if wanted.intersection({a.upper() for a in sig.get("affected_assets", []) if isinstance(a, str)})
    ]


def group_signals_by_asset(signals):
    """Pure, no-I/O grouping of an already-fetched signal list (e.g.
    from active_signals()) into {SYMBOL: [signal, ...]}, keyed by each
    signal's affected_assets, upper-cased. A signal that mentions
    several assets appears under each of them. A signal with no
    affected_assets, or a malformed entry, contributes nothing --
    never raises.

    This exists so a caller (e.g. opportunity_watchlist.attach_news_signals())
    can read active_signals() ONCE per radar cycle and then do all its
    per-token matching in memory, instead of one file read per token --
    the same "batch, don't re-read per item" principle already used by
    opportunity_watchlist.update_from_results() and
    performance_analyzer.analyze_trades().
    """
    grouped = {}
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        assets = signal.get("affected_assets")
        if not isinstance(assets, list):
            continue  # guards against e.g. a malformed string value being iterated character-by-character
        for asset in assets:
            if not isinstance(asset, str):
                continue
            grouped.setdefault(asset.upper(), []).append(signal)
    return grouped
