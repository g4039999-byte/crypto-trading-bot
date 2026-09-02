"""Provider abstraction for the news/event signal engine
(src/news_signal_engine.py) -- keeps how events are fetched completely
separate from how they're classified/stored, so a real source can be
added later without touching the engine at all.

Why no real provider is wired in this phase: as of this writing (checked
before writing this file, not assumed):

  - X (Twitter)'s API has no free or stable read tier in 2026 -- as of
    February 2026 it moved to pay-per-use with no free allowance for new
    developers (reading a post costs money per call; there is no
    meaningful free path to pulling posts/search). Wiring X in here
    would mean silently requiring the project's owner to pay per
    request, which is not something to build in without an explicit,
    separate decision.
  - CryptoPanic (a plausible crypto-news alternative) has conflicting
    public information about its free "Developer" tier's current
    status -- some sources still advertise free access, at least one
    describes the free Developer plan as discontinued. That is exactly
    the kind of "unverified, possibly unstable" situation this phase
    was told not to build against without confirming current docs.

So this phase ships the abstraction + a deterministic MockNewsProvider
(for tests and for exercising the engine end-to-end without any network
dependency) only. Adding a real provider later is a small, isolated
change: implement NewsProvider's one method against a source whose
current terms have actually been checked, and register it -- nothing in
src/news_signal_engine.py needs to change.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RawNewsEvent:
    """One unprocessed event/post/headline as handed back by a provider,
    before any classification. All fields except `text` and `source`
    are optional -- a provider that can't supply a url or a native id
    still produces a usable event (see
    news_signal_engine._derive_event_id() for how de-duplication copes
    with a missing native id).
    """

    text: str
    source: str
    published_at: Optional[str] = None  # ISO 8601 string; None if the provider doesn't know
    event_id: Optional[str] = None      # provider-native id/url, used for de-duplication when present
    url: Optional[str] = None


class NewsProvider(ABC):
    """One external (or mock) source of news/event text. A provider's
    only job is fetching -- it never classifies, stores, or expires
    anything; that's news_signal_engine.py's job.

    Providers ARE allowed to raise on a fetch failure (a real network
    provider naturally will, e.g. on a timeout or a bad response) --
    news_signal_engine.py is what catches and logs that, per-provider,
    so one failing source can never affect another provider or stop
    ingestion entirely. See ingest_events()'s docstring.
    """

    name = "unnamed"

    @abstractmethod
    def fetch_events(self, limit=None):
        """Return a list of RawNewsEvent for whatever is newly available
        from this source. `limit`, if given, caps how many to return.
        May raise on a genuine fetch failure -- see class docstring.
        """
        raise NotImplementedError


class MockNewsProvider(NewsProvider):
    """A deterministic, in-memory provider for tests and for exercising
    the engine without any network dependency. Never touches the
    network or any file.
    """

    name = "mock"

    def __init__(self, events=None, raise_error=None):
        """events: a list of RawNewsEvent (or plain dicts with the same
        fields, for convenience) to return from fetch_events(). Defaults
        to a small built-in sample set if not given.
        raise_error: if set (an Exception instance or class), fetch_events()
        raises it instead of returning anything -- for testing provider-
        failure isolation.
        """
        self._raise_error = raise_error
        if events is None:
            events = self._default_sample_events()
        self._events = [self._coerce(e) for e in events]

    @staticmethod
    def _coerce(event):
        if isinstance(event, RawNewsEvent):
            return event
        if isinstance(event, dict):
            return RawNewsEvent(**event)
        raise TypeError(f"MockNewsProvider events must be RawNewsEvent or dict, got {type(event)!r}")

    @staticmethod
    def _default_sample_events():
        now = datetime.now(timezone.utc).isoformat()
        return [
            RawNewsEvent(
                text="Breaking: $SOL surges after major exchange listing announcement",
                source="mock", published_at=now, event_id="sample-1",
            ),
        ]

    def fetch_events(self, limit=None):
        if self._raise_error is not None:
            raise self._raise_error
        events = list(self._events)
        if limit is not None:
            events = events[:limit]
        return events
