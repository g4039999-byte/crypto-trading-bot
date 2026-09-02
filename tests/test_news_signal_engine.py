"""Full coverage of src/news_signal_engine.py: classification, de-
duplication, TTL/expiry, provider-failure isolation, malformed/missing
data, and its isolation from wallet/risk/execution.

STATE_FILE is redirected to a temp directory for every test in this
file -- nothing here ever touches the real project's data/ directory.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.news_signal_engine as engine
from src.news_providers import MockNewsProvider, RawNewsEvent


def _event(text="Generic news text", source="mock", published_at="2026-01-01T00:00:00+00:00",
           event_id=None, url=None):
    return RawNewsEvent(text=text, source=source, published_at=published_at, event_id=event_id, url=url)


class IsolatedStateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_file = Path(self._tmp_dir.name) / "news_signals.json"
        self._patches = [
            mock.patch.object(engine, "STATE_FILE", tmp_file),
            mock.patch.object(engine, "NEWS_SIGNAL_TTL_MINUTES", 240),
            mock.patch.object(engine, "NEWS_SIGNAL_MAX_STORED", 500),
        ]
        for p in self._patches:
            p.start()
        self._state_file = tmp_file

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()


class TestClassifyEventType(unittest.TestCase):
    def test_listing(self):
        self.assertEqual(engine._classify_event_type("Token XYZ listing on major exchange"), "LISTING")

    def test_partnership(self):
        self.assertEqual(engine._classify_event_type("Project announces partnership with a major fund"), "PARTNERSHIP")

    def test_regulatory(self):
        self.assertEqual(engine._classify_event_type("SEC investigating the exchange for compliance issues"), "REGULATORY")

    def test_hack_exploit(self):
        self.assertEqual(engine._classify_event_type("Protocol hacked, funds drained in exploit"), "HACK_EXPLOIT")

    def test_macro(self):
        self.assertEqual(engine._classify_event_type("Federal Reserve signals interest rate cut"), "MACRO")

    def test_generic_when_nothing_matches(self):
        self.assertEqual(engine._classify_event_type("A cat sat on a mat"), "GENERIC")

    def test_generic_for_empty_or_none_text(self):
        self.assertEqual(engine._classify_event_type(""), "GENERIC")
        self.assertEqual(engine._classify_event_type(None), "GENERIC")

    def test_hack_exploit_takes_priority_over_listing(self):
        text = "Newly listed token hacked hours after listing"
        self.assertEqual(engine._classify_event_type(text), "HACK_EXPLOIT")

    def test_case_insensitive(self):
        self.assertEqual(engine._classify_event_type("BREAKING: TOKEN HACKED"), "HACK_EXPLOIT")


class TestClassifySentiment(unittest.TestCase):
    def test_positive(self):
        sentiment, confidence = engine._classify_sentiment("Token rallies and surges to new highs")
        self.assertEqual(sentiment, "POSITIVE")
        self.assertGreater(confidence, 0)

    def test_negative(self):
        sentiment, confidence = engine._classify_sentiment("Token crashes after exploit, sell-off intensifies")
        self.assertEqual(sentiment, "NEGATIVE")
        self.assertGreater(confidence, 0)

    def test_neutral_with_no_keywords(self):
        sentiment, confidence = engine._classify_sentiment("Token trades sideways today")
        self.assertEqual(sentiment, "NEUTRAL")
        self.assertEqual(confidence, 0.0)

    def test_neutral_with_equal_positive_and_negative(self):
        sentiment, confidence = engine._classify_sentiment("Surge followed by a crash")
        self.assertEqual(sentiment, "NEUTRAL")
        self.assertGreater(confidence, 0)  # still had keyword hits, just balanced

    def test_confidence_is_capped_at_one(self):
        text = " ".join(engine._POSITIVE_WORDS * 5)
        _, confidence = engine._classify_sentiment(text)
        self.assertLessEqual(confidence, 1.0)

    def test_empty_or_none_text_is_neutral_zero_confidence(self):
        self.assertEqual(engine._classify_sentiment(""), ("NEUTRAL", 0.0))
        self.assertEqual(engine._classify_sentiment(None), ("NEUTRAL", 0.0))


class TestDirectionalBias(unittest.TestCase):
    def test_positive_sentiment_is_bullish(self):
        self.assertEqual(engine._directional_bias("POSITIVE", "GENERIC"), "BULLISH")

    def test_negative_sentiment_is_bearish(self):
        self.assertEqual(engine._directional_bias("NEGATIVE", "GENERIC"), "BEARISH")

    def test_neutral_sentiment_is_neutral(self):
        self.assertEqual(engine._directional_bias("NEUTRAL", "GENERIC"), "NEUTRAL")

    def test_hack_exploit_is_always_bearish_even_with_positive_sentiment(self):
        # e.g. "Hacker returns stolen funds after growth of community pressure"
        # -- incidental positive words must not override a hack event.
        self.assertEqual(engine._directional_bias("POSITIVE", "HACK_EXPLOIT"), "BEARISH")


class TestUrgency(unittest.TestCase):
    def test_breaking_keyword_is_high(self):
        self.assertEqual(engine._urgency("BREAKING: something happened", "GENERIC"), "HIGH")

    def test_hack_exploit_without_urgency_keyword_is_medium(self):
        self.assertEqual(engine._urgency("Protocol was exploited yesterday", "HACK_EXPLOIT"), "MEDIUM")

    def test_regulatory_without_urgency_keyword_is_medium(self):
        self.assertEqual(engine._urgency("SEC opens an investigation", "REGULATORY"), "MEDIUM")

    def test_generic_without_urgency_keyword_is_low(self):
        self.assertEqual(engine._urgency("Token trades sideways", "GENERIC"), "LOW")

    def test_urgency_keyword_overrides_event_type(self):
        self.assertEqual(engine._urgency("URGENT update on the listing", "LISTING"), "HIGH")


class TestExtractAffectedAssets(unittest.TestCase):
    def test_extracts_dollar_tickers(self):
        self.assertEqual(engine._extract_affected_assets("$SOL surges while $btc holds steady"), ["SOL", "BTC"])

    def test_deduplicates_preserving_order(self):
        self.assertEqual(engine._extract_affected_assets("$SOL and $SOL again, also $sol"), ["SOL"])

    def test_no_tickers_returns_empty_list(self):
        self.assertEqual(engine._extract_affected_assets("no tickers mentioned here"), [])

    def test_empty_or_none_text_returns_empty_list(self):
        self.assertEqual(engine._extract_affected_assets(""), [])
        self.assertEqual(engine._extract_affected_assets(None), [])

    def test_non_string_text_returns_empty_list_not_a_crash(self):
        self.assertEqual(engine._extract_affected_assets(12345), [])


class TestClassifyEventEndToEnd(unittest.TestCase):
    def test_full_classification_shape(self):
        result = engine.classify_event(_event(text="Breaking: $SOL surges after exchange listing"))
        self.assertEqual(set(result.keys()), {
            "event_type", "sentiment", "confidence", "affected_assets", "directional_bias", "urgency",
        })
        self.assertEqual(result["event_type"], "LISTING")
        self.assertEqual(result["sentiment"], "POSITIVE")
        self.assertEqual(result["affected_assets"], ["SOL"])
        self.assertEqual(result["directional_bias"], "BULLISH")
        self.assertEqual(result["urgency"], "HIGH")

    def test_missing_text_does_not_crash(self):
        result = engine.classify_event(_event(text=None))
        self.assertEqual(result["event_type"], "GENERIC")
        self.assertEqual(result["sentiment"], "NEUTRAL")


class TestDeriveEventId(unittest.TestCase):
    def test_native_id_is_namespaced_by_source(self):
        event_id = engine.derive_event_id(_event(source="mock", event_id="abc"))
        self.assertEqual(event_id, "mock:abc")

    def test_different_sources_with_the_same_native_id_do_not_collide(self):
        id_a = engine.derive_event_id(_event(source="source-a", event_id="same-id"))
        id_b = engine.derive_event_id(_event(source="source-b", event_id="same-id"))
        self.assertNotEqual(id_a, id_b)

    def test_missing_native_id_derives_a_stable_hash(self):
        event = _event(text="Some headline", source="mock", published_at="2026-01-01T00:00:00+00:00", event_id=None)
        id_1 = engine.derive_event_id(event)
        id_2 = engine.derive_event_id(event)
        self.assertEqual(id_1, id_2)

    def test_the_same_headline_refetched_seconds_later_still_matches(self):
        event_a = _event(text="Some headline", source="mock", published_at="2026-01-01T00:00:05+00:00", event_id=None)
        event_b = _event(text="Some headline", source="mock", published_at="2026-01-01T00:00:45+00:00", event_id=None)
        self.assertEqual(engine.derive_event_id(event_a), engine.derive_event_id(event_b))

    def test_a_different_minute_produces_a_different_id(self):
        event_a = _event(text="Some headline", source="mock", published_at="2026-01-01T00:00:05+00:00", event_id=None)
        event_b = _event(text="Some headline", source="mock", published_at="2026-01-01T00:05:05+00:00", event_id=None)
        self.assertNotEqual(engine.derive_event_id(event_a), engine.derive_event_id(event_b))

    def test_unparseable_timestamp_still_produces_a_stable_id(self):
        event = _event(text="Some headline", source="mock", published_at="not-a-timestamp", event_id=None)
        self.assertEqual(engine.derive_event_id(event), engine.derive_event_id(event))

    def test_case_and_whitespace_insensitive_text_normalization(self):
        event_a = _event(text="Some Headline", source="mock", published_at="2026-01-01T00:00:00+00:00", event_id=None)
        event_b = _event(text="  some headline  ", source="mock", published_at="2026-01-01T00:00:00+00:00", event_id=None)
        self.assertEqual(engine.derive_event_id(event_a), engine.derive_event_id(event_b))


class TestIngestRawEvent(IsolatedStateTestCase):
    def test_stores_a_classified_signal(self):
        state = engine._empty_state()
        engine.ingest_raw_event(state, _event(text="$SOL surges after listing", event_id="e1"))
        self.assertEqual(len(state["signals"]), 1)
        signal = next(iter(state["signals"].values()))
        self.assertEqual(signal["event_type"], "LISTING")
        self.assertEqual(signal["affected_assets"], ["SOL"])

    def test_duplicate_event_is_not_re_added(self):
        state = engine._empty_state()
        engine.ingest_raw_event(state, _event(text="text A", event_id="e1"))
        engine.ingest_raw_event(state, _event(text="text A (refetched)", event_id="e1"))
        self.assertEqual(len(state["signals"]), 1)
        # The original classification is kept -- not re-classified from
        # the second, slightly different text.
        signal = next(iter(state["signals"].values()))
        self.assertEqual(signal["text"], "text A")

    def test_two_different_events_are_both_stored(self):
        state = engine._empty_state()
        engine.ingest_raw_event(state, _event(text="text A", event_id="e1"))
        engine.ingest_raw_event(state, _event(text="text B", event_id="e2"))
        self.assertEqual(len(state["signals"]), 2)

    def test_expires_at_is_set_from_the_configured_ttl(self):
        with mock.patch.object(engine, "NEWS_SIGNAL_TTL_MINUTES", 60):
            state = engine._empty_state()
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            engine.ingest_raw_event(state, _event(event_id="e1"), now=now)
        signal = next(iter(state["signals"].values()))
        self.assertEqual(signal["expires_at"], (now + timedelta(minutes=60)).isoformat())

    def test_missing_source_falls_back_to_unknown(self):
        event = RawNewsEvent(text="no source given", source=None)
        state = engine._empty_state()
        engine.ingest_raw_event(state, event)
        signal = next(iter(state["signals"].values()))
        self.assertEqual(signal["source"], "unknown")


class TestIsExpired(unittest.TestCase):
    def test_not_expired_before_expires_at(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = {"expires_at": (now + timedelta(minutes=10)).isoformat()}
        self.assertFalse(engine.is_expired(signal, now=now))

    def test_expired_after_expires_at(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = {"expires_at": (now - timedelta(minutes=10)).isoformat()}
        self.assertTrue(engine.is_expired(signal, now=now))

    def test_exactly_at_expiry_counts_as_expired(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = {"expires_at": now.isoformat()}
        self.assertTrue(engine.is_expired(signal, now=now))

    def test_missing_or_malformed_expires_at_counts_as_expired(self):
        self.assertTrue(engine.is_expired({}))
        self.assertTrue(engine.is_expired({"expires_at": "not-a-date"}))
        self.assertTrue(engine.is_expired({"expires_at": None}))


class TestPruneExpiredAndOverflow(IsolatedStateTestCase):
    def test_expired_signals_are_removed(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = {"signals": {
            "old": {"expires_at": (now - timedelta(minutes=1)).isoformat(), "ingested_at": now.isoformat()},
            "fresh": {"expires_at": (now + timedelta(minutes=1)).isoformat(), "ingested_at": now.isoformat()},
        }}
        engine._prune_expired_and_overflow(state, now=now)
        self.assertEqual(set(state["signals"].keys()), {"fresh"})

    def test_overflow_beyond_max_stored_drops_the_oldest_first(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = {"signals": {}}
        for i in range(5):
            state["signals"][f"e{i}"] = {
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "ingested_at": (now + timedelta(minutes=i)).isoformat(),
            }
        with mock.patch.object(engine, "NEWS_SIGNAL_MAX_STORED", 3):
            engine._prune_expired_and_overflow(state, now=now)
        self.assertEqual(set(state["signals"].keys()), {"e2", "e3", "e4"})

    def test_no_expired_or_overflow_leaves_state_unchanged(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = {"signals": {"e1": {"expires_at": (now + timedelta(hours=1)).isoformat(), "ingested_at": now.isoformat()}}}
        engine._prune_expired_and_overflow(state, now=now)
        self.assertEqual(len(state["signals"]), 1)


class TestIngestEvents(IsolatedStateTestCase):
    def test_ingests_from_a_single_provider(self):
        provider = MockNewsProvider(events=[_event(text="a", event_id="e1"), _event(text="b", event_id="e2")])
        new_count = engine.ingest_events([provider])
        self.assertEqual(new_count, 2)
        self.assertEqual(len(engine.load_state()["signals"]), 2)

    def test_ingests_from_multiple_providers(self):
        provider_a = MockNewsProvider(events=[_event(text="a", source="a", event_id="e1")])
        provider_b = MockNewsProvider(events=[_event(text="b", source="b", event_id="e1")])
        new_count = engine.ingest_events([provider_a, provider_b])
        self.assertEqual(new_count, 2)  # namespaced by source -- no collision

    def test_a_failing_provider_does_not_stop_ingestion_from_others(self):
        good_provider = MockNewsProvider(events=[_event(text="a", event_id="e1")])
        bad_provider = MockNewsProvider(raise_error=ConnectionError("network down"))
        new_count = engine.ingest_events([good_provider, bad_provider])  # must not raise
        self.assertEqual(new_count, 1)

    def test_all_providers_failing_yields_zero_new_not_a_crash(self):
        bad_provider_1 = MockNewsProvider(raise_error=ConnectionError("down"))
        bad_provider_2 = MockNewsProvider(raise_error=TimeoutError("slow"))
        new_count = engine.ingest_events([bad_provider_1, bad_provider_2])  # must not raise
        self.assertEqual(new_count, 0)

    def test_no_providers_at_all_is_a_no_op(self):
        self.assertEqual(engine.ingest_events([]), 0)
        self.assertEqual(engine.ingest_events(None), 0)

    def test_re_ingesting_the_same_event_does_not_increase_new_count(self):
        provider = MockNewsProvider(events=[_event(text="a", event_id="e1")])
        engine.ingest_events([provider])
        second_count = engine.ingest_events([provider])
        self.assertEqual(second_count, 0)
        self.assertEqual(len(engine.load_state()["signals"]), 1)

    def test_a_provider_returning_a_malformed_event_is_skipped_not_crashed_on(self):
        class BadProvider:
            name = "bad"

            def fetch_events(self, limit=None):
                return ["not-a-raw-event", 42, None]

        new_count = engine.ingest_events([BadProvider()])  # must not raise
        self.assertEqual(new_count, 0)

    def test_a_provider_returning_none_is_treated_as_no_events(self):
        class NoneProvider:
            name = "none-returning"

            def fetch_events(self, limit=None):
                return None

        new_count = engine.ingest_events([NoneProvider()])  # must not raise
        self.assertEqual(new_count, 0)

    def test_ingestion_prunes_expired_signals_too(self):
        with mock.patch.object(engine, "NEWS_SIGNAL_TTL_MINUTES", 10):
            provider = MockNewsProvider(events=[_event(text="old news", event_id="old")])
            old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
            engine.ingest_events([provider], now=old_time)

            later_provider = MockNewsProvider(events=[_event(text="new news", event_id="new")])
            later_time = old_time + timedelta(minutes=30)
            engine.ingest_events([later_provider], now=later_time)

        remaining = engine.load_state()["signals"]
        self.assertEqual(set(remaining.keys()), {"mock:new"})


class TestGroupSignalsByAsset(unittest.TestCase):
    """Pure function -- no I/O, no state file involved."""

    def _signal(self, event_id="e1", affected_assets=None):
        return {"event_id": event_id, "affected_assets": affected_assets or []}

    def test_empty_list_returns_empty_dict(self):
        self.assertEqual(engine.group_signals_by_asset([]), {})

    def test_none_input_returns_empty_dict(self):
        self.assertEqual(engine.group_signals_by_asset(None), {})

    def test_groups_a_single_signal_under_its_asset(self):
        signal = self._signal(affected_assets=["SOL"])
        grouped = engine.group_signals_by_asset([signal])
        self.assertEqual(grouped, {"SOL": [signal]})

    def test_a_signal_mentioning_multiple_assets_appears_under_each(self):
        signal = self._signal(affected_assets=["SOL", "BTC"])
        grouped = engine.group_signals_by_asset([signal])
        self.assertEqual(set(grouped.keys()), {"SOL", "BTC"})
        self.assertEqual(grouped["SOL"], [signal])
        self.assertEqual(grouped["BTC"], [signal])

    def test_multiple_signals_for_the_same_asset_are_both_kept(self):
        sig1 = self._signal(event_id="e1", affected_assets=["SOL"])
        sig2 = self._signal(event_id="e2", affected_assets=["SOL"])
        grouped = engine.group_signals_by_asset([sig1, sig2])
        self.assertEqual(grouped["SOL"], [sig1, sig2])

    def test_grouping_is_case_insensitive_and_upper_cased(self):
        signal = self._signal(affected_assets=["sol"])
        grouped = engine.group_signals_by_asset([signal])
        self.assertEqual(list(grouped.keys()), ["SOL"])

    def test_a_signal_with_no_affected_assets_contributes_nothing(self):
        signal = self._signal(affected_assets=[])
        self.assertEqual(engine.group_signals_by_asset([signal]), {})

    def test_a_non_dict_signal_in_the_list_is_skipped_not_crashed_on(self):
        grouped = engine.group_signals_by_asset(["not-a-dict", None, self._signal(affected_assets=["SOL"])])
        self.assertEqual(list(grouped.keys()), ["SOL"])

    def test_a_non_string_asset_entry_is_skipped_not_crashed_on(self):
        signal = self._signal(affected_assets=["SOL", 42, None])
        grouped = engine.group_signals_by_asset([signal])
        self.assertEqual(list(grouped.keys()), ["SOL"])

    def test_malformed_affected_assets_field_does_not_crash(self):
        signal = {"event_id": "e1", "affected_assets": "not-a-list"}
        self.assertEqual(engine.group_signals_by_asset([signal]), {})


class TestActiveSignalsAndSignalsForSymbols(IsolatedStateTestCase):
    def test_active_signals_excludes_expired_ones(self):
        with mock.patch.object(engine, "NEWS_SIGNAL_TTL_MINUTES", 10):
            provider = MockNewsProvider(events=[_event(text="old", event_id="old")])
            old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
            engine.ingest_events([provider], now=old_time)

        active = engine.active_signals(now=old_time + timedelta(minutes=30))
        self.assertEqual(active, [])

    def test_active_signals_returns_most_recently_ingested_first(self):
        engine.ingest_events([MockNewsProvider(events=[_event(text="first", event_id="e1")])],
                              now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        engine.ingest_events([MockNewsProvider(events=[_event(text="second", event_id="e2")])],
                              now=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc))
        active = engine.active_signals(now=datetime(2026, 1, 1, 0, 6, tzinfo=timezone.utc))
        self.assertEqual([sig["text"] for sig in active], ["second", "first"])

    def test_signals_for_symbols_filters_by_affected_assets(self):
        provider = MockNewsProvider(events=[
            _event(text="$SOL surges", event_id="e1"),
            _event(text="$ETH drops", event_id="e2"),
        ])
        engine.ingest_events([provider])
        results = engine.signals_for_symbols(["SOL"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["affected_assets"], ["SOL"])

    def test_signals_for_symbols_is_case_insensitive(self):
        provider = MockNewsProvider(events=[_event(text="$SOL surges", event_id="e1")])
        engine.ingest_events([provider])
        results = engine.signals_for_symbols(["sol"])
        self.assertEqual(len(results), 1)

    def test_signals_for_symbols_empty_input_returns_empty(self):
        self.assertEqual(engine.signals_for_symbols([]), [])
        self.assertEqual(engine.signals_for_symbols(None), [])

    def test_signals_for_symbols_no_match_returns_empty(self):
        provider = MockNewsProvider(events=[_event(text="$SOL surges", event_id="e1")])
        engine.ingest_events([provider])
        self.assertEqual(engine.signals_for_symbols(["BTC"]), [])

    def test_signals_for_symbols_excludes_expired(self):
        with mock.patch.object(engine, "NEWS_SIGNAL_TTL_MINUTES", 10):
            provider = MockNewsProvider(events=[_event(text="$SOL surges", event_id="e1")])
            old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
            engine.ingest_events([provider], now=old_time)

        results = engine.signals_for_symbols(["SOL"], now=old_time + timedelta(minutes=30))
        self.assertEqual(results, [])


class TestLoadSaveState(IsolatedStateTestCase):
    def test_load_when_file_does_not_exist_returns_empty(self):
        self.assertEqual(engine.load_state(), {"signals": {}})

    def test_save_then_load_round_trips(self):
        state = {"signals": {"e1": {"event_id": "e1"}}}
        engine.save_state(state)
        self.assertEqual(engine.load_state(), state)

    def test_corrupt_file_degrades_to_empty(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(engine.load_state(), {"signals": {}})

    def test_unexpected_top_level_shape_degrades_to_empty(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text('["not", "a", "dict"]', encoding="utf-8")
        self.assertEqual(engine.load_state(), {"signals": {}})


class TestIsolationFromWalletAndExecution(unittest.TestCase):
    def test_engine_source_does_not_import_wallet_risk_or_decision_modules(self):
        import inspect

        forbidden = (
            "src.wallet", "src.risk", "src.live_trader", "src.paper_trader",
            "src.portfolio", "src.paper_portfolio", "src.radar", "src.opportunity_watchlist",
        )
        source = inspect.getsource(engine)
        for module_name in forbidden:
            self.assertNotIn(module_name, source, f"{module_name} must never be imported by news_signal_engine.py")

    def test_providers_module_does_not_import_wallet_or_decision_modules(self):
        import inspect

        import src.news_providers as providers

        forbidden = ("src.wallet", "src.risk", "src.live_trader", "src.paper_trader")
        source = inspect.getsource(providers)
        for module_name in forbidden:
            self.assertNotIn(module_name, source, f"{module_name} must never be imported by news_providers.py")

    def test_decision_and_execution_modules_do_not_import_the_news_engine(self):
        import inspect

        import src.live_trader as live_trader
        import src.paper_trader as paper_trader
        import src.risk as risk
        import src.wallet as wallet

        forbidden_imports = (
            "import src.news_signal_engine", "from src.news_signal_engine", "from src import news_signal_engine",
            "import src.news_providers", "from src.news_providers", "from src import news_providers",
        )
        for module in (wallet, risk, live_trader, paper_trader):
            source = inspect.getsource(module)
            for forbidden in forbidden_imports:
                self.assertNotIn(forbidden, source, f"{module.__name__} must never import the news signal engine")

    def test_radar_and_opportunity_watchlist_link_is_read_only_and_scoped(self):
        # Phase 7 supersedes the previous "not wired yet" guarantee with
        # a narrower one: opportunity_watchlist.py now DOES import this
        # engine (read-only, see attach_news_signals()), but radar.py
        # itself still does not import this engine directly -- it only
        # calls opportunity_watchlist.attach_news_signals(), which is
        # where the actual read happens. Confirm that boundary here.
        import inspect

        import src.radar as radar

        forbidden_imports = (
            "import src.news_signal_engine", "from src.news_signal_engine", "from src import news_signal_engine",
        )
        source = inspect.getsource(radar)
        for forbidden in forbidden_imports:
            self.assertNotIn(forbidden, source)

    def test_opportunity_watchlist_only_reads_this_engine_never_writes_to_it(self):
        # The one-way, read-only nature of the Phase 7 link:
        # attach_news_signals() only ever calls this engine's read-only
        # functions (active_signals, group_signals_by_asset) -- never
        # any function that writes to data/news_signals.json
        # (save_state, ingest_raw_event, ingest_events).
        import inspect

        import src.opportunity_watchlist as opportunity_watchlist

        source = inspect.getsource(opportunity_watchlist.attach_news_signals)
        write_functions = ("ingest_raw_event(", "ingest_events(", "news_signal_engine.save_state(")
        for forbidden in write_functions:
            self.assertNotIn(forbidden, source)
        self.assertIn("active_signals", source)
        self.assertIn("group_signals_by_asset", source)


if __name__ == "__main__":
    unittest.main()
