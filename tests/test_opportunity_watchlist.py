"""Full coverage of src/opportunity_watchlist.py: every status
transition (NEW -> WATCHING -> QUALIFIED / REJECTED -> EXPIRED and
back), history recording, de-duplication, and its isolation from
positions/paper_positions/execution.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.opportunity_watchlist as watchlist


def _make_result(address="addr-1", symbol="GOOD", score=70, base_score=70, momentum_score=70,
                  trend="NEUTRAL", stage="EARLY", ok=True):
    return {
        "address": address, "symbol": symbol, "score": score, "base_score": base_score,
        "momentum_score": momentum_score, "trend": trend, "stage": stage, "ok": ok,
    }


class WatchlistTestCase(unittest.TestCase):
    """Shared isolated-state-file setup, mirroring tests/test_paper_portfolio.py."""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_file = Path(self._tmp_dir.name) / "opportunity_watchlist.json"
        self._patches = [
            mock.patch.object(watchlist, "STATE_FILE", tmp_file),
            mock.patch.object(watchlist, "OPPORTUNITY_QUALIFY_SCORE", 75),
            mock.patch.object(watchlist, "OPPORTUNITY_QUALIFY_TRENDS", ("STRONG", "RISING")),
            mock.patch.object(watchlist, "OPPORTUNITY_REJECT_SCORE", 20),
            mock.patch.object(watchlist, "OPPORTUNITY_EXPIRY_MINUTES", 180),
            mock.patch.object(watchlist, "OPPORTUNITY_HISTORY_LIMIT", 60),
        ]
        for p in self._patches:
            p.start()
        self._state_file = tmp_file

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()


class TestLoadSaveState(WatchlistTestCase):
    def test_load_state_when_file_does_not_exist_returns_empty(self):
        self.assertEqual(watchlist.load_state(), {"opportunities": {}})

    def test_save_then_load_round_trips(self):
        state = {"opportunities": {"addr-1": {"status": "NEW"}}}
        watchlist.save_state(state)
        self.assertEqual(watchlist.load_state(), state)

    def test_corrupt_file_degrades_to_empty_instead_of_raising(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(watchlist.load_state(), {"opportunities": {}})

    def test_unexpected_top_level_shape_degrades_to_empty(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        self.assertEqual(watchlist.load_state(), {"opportunities": {}})


class TestFirstSighting(WatchlistTestCase):
    def test_a_never_seen_address_becomes_new(self):
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50,
            base_score=50, momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True,
        )
        entry = state["opportunities"]["addr-1"]
        self.assertEqual(entry["status"], "NEW")
        self.assertEqual(entry["symbol"], "GOOD")
        self.assertEqual(len(entry["history"]), 1)
        self.assertEqual(entry["first_seen_at"], entry["last_updated_at"])

    def test_first_sighting_is_never_immediately_qualified_or_rejected(self):
        # Even a score/trend that would otherwise qualify or reject must
        # still land as NEW on the very first sighting -- status
        # transitions only start from the SECOND update onward.
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=99,
            base_score=99, momentum_score=99, trend="STRONG", stage="EARLY", ok=True,
        )
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "NEW")

    def test_malformed_address_is_a_no_op(self):
        state = watchlist._empty_state()
        watchlist.update_one(state, address="?", symbol="X", score=1, base_score=1,
                              momentum_score=1, trend="WEAK", stage="EARLY", ok=False)
        watchlist.update_one(state, address=None, symbol="X", score=1, base_score=1,
                              momentum_score=1, trend="WEAK", stage="EARLY", ok=False)
        watchlist.update_one(state, address="", symbol="X", score=1, base_score=1,
                              momentum_score=1, trend="WEAK", stage="EARLY", ok=False)
        self.assertEqual(state["opportunities"], {})


class TestActiveTransitions(WatchlistTestCase):
    """NEW/WATCHING/QUALIFIED move freely based on each update's fresh
    data (see module docstring) -- these tests drive an entry through
    the second-and-later updates that actually decide status.
    """

    def _new_then_update(self, **kwargs):
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50,
            base_score=50, momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True,
        )
        defaults = dict(address="addr-1", symbol="GOOD", score=50, base_score=50,
                         momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True)
        defaults.update(kwargs)
        return watchlist.update_one(state, **defaults)

    def test_second_sighting_with_middling_data_becomes_watching(self):
        state = self._new_then_update(score=50, trend="NEUTRAL", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "WATCHING")

    def test_high_score_and_strong_trend_qualifies(self):
        state = self._new_then_update(score=80, trend="STRONG", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "QUALIFIED")

    def test_high_score_alone_without_a_qualifying_trend_does_not_qualify(self):
        state = self._new_then_update(score=90, trend="NEUTRAL", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "WATCHING")

    def test_low_score_rejects(self):
        state = self._new_then_update(score=10, trend="WEAK", ok=False)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "REJECTED")

    def test_failing_the_first_pass_filter_rejects_even_with_a_high_score(self):
        # ok=False (failed radar.py's first-pass filter) is enough on
        # its own, independent of score.
        state = self._new_then_update(score=95, trend="STRONG", ok=False)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "REJECTED")

    def test_watching_can_move_up_to_qualified_on_a_later_update(self):
        state = self._new_then_update(score=50, trend="NEUTRAL", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "WATCHING")
        state = watchlist.update_one(state, address="addr-1", symbol="GOOD", score=85,
                                      base_score=85, momentum_score=85, trend="RISING", stage="EARLY", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "QUALIFIED")

    def test_qualified_can_drop_back_to_watching_if_it_cools_off(self):
        state = self._new_then_update(score=85, trend="STRONG", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "QUALIFIED")
        state = watchlist.update_one(state, address="addr-1", symbol="GOOD", score=40,
                                      base_score=40, momentum_score=40, trend="NEUTRAL", stage="EARLY", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "WATCHING")


class TestRejectedIsTerminal(WatchlistTestCase):
    def test_rejected_status_never_changes_even_with_much_better_later_data(self):
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50,
            base_score=50, momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True,
        )
        state = watchlist.update_one(state, address="addr-1", symbol="GOOD", score=5,
                                      base_score=5, momentum_score=5, trend="WEAK", stage="EARLY", ok=False)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "REJECTED")

        # A dramatically better later update must NOT revive it.
        state = watchlist.update_one(state, address="addr-1", symbol="GOOD", score=95,
                                      base_score=95, momentum_score=95, trend="STRONG", stage="EARLY", ok=True)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "REJECTED")

    def test_rejected_still_records_history_even_though_status_is_frozen(self):
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50,
            base_score=50, momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True,
        )
        state = watchlist.update_one(state, address="addr-1", symbol="GOOD", score=5,
                                      base_score=5, momentum_score=5, trend="WEAK", stage="EARLY", ok=False)
        state = watchlist.update_one(state, address="addr-1", symbol="GOOD", score=6,
                                      base_score=6, momentum_score=6, trend="WEAK", stage="RISING", ok=False)
        self.assertEqual(len(state["opportunities"]["addr-1"]["history"]), 3)


class TestExpiry(WatchlistTestCase):
    def test_a_stale_watching_entry_expires(self):
        now = datetime.now(timezone.utc)
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True, now=now - timedelta(minutes=200),
        )
        state = watchlist._apply_expiry(state, touched_addresses=set(), now=now)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "EXPIRED")

    def test_an_entry_touched_this_cycle_is_never_expired_regardless_of_age(self):
        now = datetime.now(timezone.utc)
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True, now=now - timedelta(minutes=200),
        )
        state = watchlist._apply_expiry(state, touched_addresses={"addr-1"}, now=now)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "NEW")

    def test_a_fresh_entry_does_not_expire(self):
        now = datetime.now(timezone.utc)
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True, now=now - timedelta(minutes=5),
        )
        state = watchlist._apply_expiry(state, touched_addresses=set(), now=now)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "NEW")

    def test_rejected_entries_are_never_marked_expired(self):
        now = datetime.now(timezone.utc)
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True, now=now - timedelta(minutes=300),
        )
        state = watchlist.update_one(
            state, address="addr-1", symbol="GOOD", score=5, base_score=5, momentum_score=5,
            trend="WEAK", stage="EARLY", ok=False, now=now - timedelta(minutes=300),
        )
        state = watchlist._apply_expiry(state, touched_addresses=set(), now=now)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "REJECTED")

    def test_expired_is_not_terminal_it_revives_on_the_next_sighting(self):
        now = datetime.now(timezone.utc)
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True, now=now - timedelta(minutes=200),
        )
        state = watchlist._apply_expiry(state, touched_addresses=set(), now=now)
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "EXPIRED")

        # Seen again later, with data that would qualify -- must revive
        # into the active funnel, not stay stuck as EXPIRED.
        state = watchlist.update_one(
            state, address="addr-1", symbol="GOOD", score=90, base_score=90, momentum_score=90,
            trend="STRONG", stage="EARLY", ok=True, now=now + timedelta(minutes=10),
        )
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "QUALIFIED")

    def test_already_expired_entries_are_left_alone_by_a_second_expiry_pass(self):
        now = datetime.now(timezone.utc)
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True, now=now - timedelta(minutes=400),
        )
        state = watchlist._apply_expiry(state, touched_addresses=set(), now=now)
        before = state["opportunities"]["addr-1"]["last_updated_at"]
        state = watchlist._apply_expiry(state, touched_addresses=set(), now=now + timedelta(minutes=1))
        self.assertEqual(state["opportunities"]["addr-1"]["last_updated_at"], before)


class TestSignalFieldsAreRecordedButDoNotAffectStatus(WatchlistTestCase):
    """Phase 4: src.momentum_signals fields flow into history entries as
    pure records -- they must never change the NEW/WATCHING/QUALIFIED/
    REJECTED classification logic, which is entirely unchanged.
    """

    def test_signal_fields_land_in_the_history_entry(self):
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True,
            buy_sell_pressure=0.8, volume_momentum=0.5, price_acceleration=0.1, persistence_streak=3,
        )
        entry = state["opportunities"]["addr-1"]["history"][0]
        self.assertEqual(entry["buy_sell_pressure"], 0.8)
        self.assertEqual(entry["volume_momentum"], 0.5)
        self.assertEqual(entry["price_acceleration"], 0.1)
        self.assertEqual(entry["persistence_streak"], 3)

    def test_omitting_signal_fields_defaults_them_to_none_in_history(self):
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50, base_score=50,
            momentum_score=50, trend="NEUTRAL", stage="EARLY", ok=True,
        )
        entry = state["opportunities"]["addr-1"]["history"][0]
        self.assertIsNone(entry["buy_sell_pressure"])
        self.assertIsNone(entry["volume_momentum"])
        self.assertIsNone(entry["price_acceleration"])
        self.assertIsNone(entry["persistence_streak"])

    def test_extreme_signal_values_never_change_the_qualify_reject_outcome(self):
        # A qualifying score+trend must still qualify regardless of what
        # the signal fields say, and vice versa for rejection -- proves
        # these fields are not silently wired into _classify_active().
        state = watchlist.update_one(
            watchlist._empty_state(), address="addr-1", symbol="GOOD", score=50,
            base_score=50, momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True,
        )
        state = watchlist.update_one(
            state, address="addr-1", symbol="GOOD", score=85, base_score=85, momentum_score=85,
            trend="STRONG", stage="EARLY", ok=True,
            buy_sell_pressure=0.0, volume_momentum=-0.99, price_acceleration=-5.0, persistence_streak=0,
        )
        self.assertEqual(state["opportunities"]["addr-1"]["status"], "QUALIFIED")

        state2 = watchlist.update_one(
            watchlist._empty_state(), address="addr-2", symbol="BAD", score=50,
            base_score=50, momentum_score=50, trend="INSUFFICIENT_DATA", stage="UNKNOWN", ok=True,
        )
        state2 = watchlist.update_one(
            state2, address="addr-2", symbol="BAD", score=5, base_score=5, momentum_score=5,
            trend="WEAK", stage="EARLY", ok=False,
            buy_sell_pressure=1.0, volume_momentum=10.0, price_acceleration=5.0, persistence_streak=99,
        )
        self.assertEqual(state2["opportunities"]["addr-2"]["status"], "REJECTED")


class TestUpdateFromResultsCarriesSignals(WatchlistTestCase):
    def test_signals_dict_on_a_result_flows_into_the_history_entry(self):
        result = _make_result("addr-1", "A")
        result["signals"] = {
            "buy_sell_pressure": 0.7, "volume_momentum": 0.25,
            "price_acceleration": 0.05, "persistence_streak": 2,
        }
        watchlist.update_from_results([result])
        entry = watchlist.load_state()["opportunities"]["addr-1"]["history"][0]
        self.assertEqual(entry["buy_sell_pressure"], 0.7)
        self.assertEqual(entry["volume_momentum"], 0.25)
        self.assertEqual(entry["price_acceleration"], 0.05)
        self.assertEqual(entry["persistence_streak"], 2)

    def test_a_result_with_no_signals_key_at_all_still_works(self):
        # Backward compatibility: a result dict shaped like it was
        # before Phase 4 (no "signals" key) must not raise or be
        # skipped.
        result = _make_result("addr-1", "A")
        watchlist.update_from_results([result])
        entry = watchlist.load_state()["opportunities"]["addr-1"]["history"][0]
        self.assertIsNone(entry["buy_sell_pressure"])

    def test_a_malformed_signals_value_is_ignored_not_raised(self):
        result = _make_result("addr-1", "A")
        result["signals"] = "not-a-dict"
        watchlist.update_from_results([result])  # must not raise
        entry = watchlist.load_state()["opportunities"]["addr-1"]["history"][0]
        self.assertIsNone(entry["buy_sell_pressure"])


class TestUpdateFromResults(WatchlistTestCase):
    def test_updates_every_address_in_the_results_list(self):
        watchlist.update_from_results([_make_result("addr-1", "A"), _make_result("addr-2", "B")])
        state = watchlist.load_state()
        self.assertEqual(set(state["opportunities"].keys()), {"addr-1", "addr-2"})

    def test_empty_results_is_a_no_op_and_does_not_create_the_file(self):
        watchlist.update_from_results([])
        self.assertFalse(self._state_file.exists())

    def test_duplicate_address_within_one_call_is_not_duplicated(self):
        watchlist.update_from_results([_make_result("addr-1", "A", score=50), _make_result("addr-1", "A", score=99)])
        state = watchlist.load_state()
        self.assertEqual(len(state["opportunities"]), 1)
        # Only the FIRST occurrence in the list is used for this cycle.
        self.assertEqual(len(state["opportunities"]["addr-1"]["history"]), 1)
        self.assertEqual(state["opportunities"]["addr-1"]["history"][0]["score"], 50)

    def test_calling_it_across_multiple_cycles_does_not_duplicate_the_entry(self):
        watchlist.update_from_results([_make_result("addr-1", "A")])
        watchlist.update_from_results([_make_result("addr-1", "A")])
        watchlist.update_from_results([_make_result("addr-1", "A")])
        state = watchlist.load_state()
        self.assertEqual(len(state["opportunities"]), 1)
        self.assertEqual(len(state["opportunities"]["addr-1"]["history"]), 3)

    def test_malformed_and_placeholder_entries_are_skipped(self):
        watchlist.update_from_results([
            "not-a-dict",
            {"symbol": "no address key"},
            _make_result(address="?", symbol="unparseable"),
            _make_result("addr-1", "REAL"),
        ])
        state = watchlist.load_state()
        self.assertEqual(list(state["opportunities"].keys()), ["addr-1"])

    def test_a_broken_write_is_caught_and_logged_not_raised(self):
        with mock.patch.object(watchlist, "save_state", side_effect=OSError("disk full")):
            watchlist.update_from_results([_make_result("addr-1", "A")])  # must not raise

    def test_history_is_capped_at_the_configured_limit(self):
        with mock.patch.object(watchlist, "OPPORTUNITY_HISTORY_LIMIT", 3):
            for i in range(5):
                watchlist.update_from_results([_make_result("addr-1", "A", score=i)])
        state = watchlist.load_state()
        history = state["opportunities"]["addr-1"]["history"]
        self.assertEqual(len(history), 3)
        self.assertEqual([h["score"] for h in history], [2, 3, 4])  # oldest trimmed off


class TestReadHelpers(WatchlistTestCase):
    def test_get_opportunity_returns_none_for_an_untracked_address(self):
        self.assertIsNone(watchlist.get_opportunity("never-seen"))

    def test_get_opportunity_returns_the_tracked_entry(self):
        watchlist.update_from_results([_make_result("addr-1", "A")])
        entry = watchlist.get_opportunity("addr-1")
        self.assertEqual(entry["symbol"], "A")

    def test_list_by_status_filters_and_sorts_most_recent_first(self):
        now = datetime.now(timezone.utc)
        watchlist.update_from_results([_make_result("addr-old", "OLD", score=50, trend="NEUTRAL")], now=now - timedelta(minutes=10))
        watchlist.update_from_results([_make_result("addr-old", "OLD", score=50, trend="NEUTRAL")], now=now - timedelta(minutes=5))
        watchlist.update_from_results([_make_result("addr-new", "NEW_TOKEN", score=50, trend="NEUTRAL")], now=now)

        watching = watchlist.list_by_status("WATCHING")
        self.assertEqual([e["address"] for e in watching], ["addr-old"])

        new_entries = watchlist.list_by_status("NEW")
        self.assertEqual([e["address"] for e in new_entries], ["addr-new"])

    def test_list_by_status_returns_empty_for_a_status_with_nothing_tracked(self):
        self.assertEqual(watchlist.list_by_status("QUALIFIED"), [])

    def test_list_all_returns_nothing_tracked_yet_as_empty(self):
        self.assertEqual(watchlist.list_all(), [])

    def test_list_all_returns_entries_regardless_of_status(self):
        watchlist.update_from_results([_make_result("addr-1", "A", score=50, trend="NEUTRAL")])
        watchlist.update_from_results([_make_result("addr-2", "B", score=5, trend="WEAK", ok=False)])
        addresses = {e["address"] for e in watchlist.list_all()}
        self.assertEqual(addresses, {"addr-1", "addr-2"})

    def test_list_all_sorts_most_recently_updated_first(self):
        now = datetime.now(timezone.utc)
        watchlist.update_from_results([_make_result("addr-old", "OLD")], now=now - timedelta(minutes=10))
        watchlist.update_from_results([_make_result("addr-new", "NEW_TOKEN")], now=now)
        self.assertEqual([e["address"] for e in watchlist.list_all()], ["addr-new", "addr-old"])


class TestAttachNewsSignals(WatchlistTestCase):
    """Phase 7: attach_news_signals() enriches EXISTING watchlist
    entries with active news signals -- purely informational, read-only
    with respect to src.news_signal_engine, and never touching status/
    history/qualification logic.
    """

    def _signal(self, event_id="e1", event_type="LISTING", sentiment="POSITIVE",
                confidence=0.6, directional_bias="BULLISH", urgency="HIGH", extra_field="unused"):
        return {
            "event_id": event_id, "event_type": event_type, "sentiment": sentiment,
            "confidence": confidence, "directional_bias": directional_bias, "urgency": urgency,
            "text": "should never be copied into the watchlist entry", "url": "should also never be copied",
            "extra_field": extra_field,
        }

    def test_matching_signal_is_attached_to_the_existing_entry(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(watchlist, "_active_news_signals", return_value=[self._signal()]), mock.patch.object(
            watchlist, "group_signals_by_asset", return_value={"GOOD": [self._signal()]}
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])

        entry = watchlist.get_opportunity("addr-1")
        self.assertEqual(len(entry["news"]), 1)
        self.assertEqual(entry["news"][0]["event_type"], "LISTING")

    def test_only_the_small_fixed_fields_are_copied_not_the_raw_text_or_url(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={"GOOD": [self._signal()]}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])

        entry = watchlist.get_opportunity("addr-1")
        self.assertEqual(set(entry["news"][0].keys()), {
            "event_id", "event_type", "sentiment", "confidence", "directional_bias", "urgency",
        })
        self.assertNotIn("text", entry["news"][0])
        self.assertNotIn("url", entry["news"][0])

    def test_no_matching_signal_leaves_an_empty_news_list(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])

        entry = watchlist.get_opportunity("addr-1")
        self.assertEqual(entry["news"], [])

    def test_a_stale_news_field_is_cleared_when_no_longer_matching(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={"GOOD": [self._signal()]}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])
        self.assertEqual(len(watchlist.get_opportunity("addr-1")["news"]), 1)

        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])
        self.assertEqual(watchlist.get_opportunity("addr-1")["news"], [])

    def test_does_not_create_a_new_opportunity_for_a_symbol_only_seen_in_news(self):
        # No prior update_from_results() call for this address -- it
        # has no watchlist entry at all.
        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={"GHOST": [self._signal()]}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-ghost", "GHOST")])

        self.assertIsNone(watchlist.get_opportunity("addr-ghost"))

    def test_never_changes_status_or_writes_a_history_entry(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD", score=85, trend="STRONG")])
        entry_before = watchlist.get_opportunity("addr-1")
        status_before = entry_before["status"]
        history_len_before = len(entry_before["history"])

        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={"GOOD": [self._signal()]}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD", score=85, trend="STRONG")])

        entry_after = watchlist.get_opportunity("addr-1")
        self.assertEqual(entry_after["status"], status_before)
        self.assertEqual(len(entry_after["history"]), history_len_before)

    def test_empty_results_is_a_no_op(self):
        with mock.patch.object(watchlist, "_active_news_signals") as mock_active:
            watchlist.attach_news_signals([])
        mock_active.assert_not_called()

    def test_a_broken_read_from_the_news_engine_does_not_raise(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(watchlist, "_active_news_signals", side_effect=RuntimeError("news engine broke")):
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])  # must not raise
        # entry is left exactly as it was -- no partial/garbage "news" field added
        self.assertNotIn("news", watchlist.get_opportunity("addr-1"))

    def test_disabled_via_config_is_a_complete_no_op(self):
        watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(watchlist, "NEWS_SIGNAL_WATCHLIST_LINK_ENABLED", False), mock.patch.object(
            watchlist, "_active_news_signals"
        ) as mock_active:
            watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])
        mock_active.assert_not_called()
        self.assertNotIn("news", watchlist.get_opportunity("addr-1"))

    def test_matching_is_case_insensitive_on_symbol(self):
        watchlist.update_from_results([_make_result("addr-1", "good")])
        with mock.patch.object(watchlist, "group_signals_by_asset", return_value={"GOOD": [self._signal()]}), mock.patch.object(
            watchlist, "_active_news_signals", return_value=[]
        ):
            watchlist.attach_news_signals([_make_result("addr-1", "good")])
        self.assertEqual(len(watchlist.get_opportunity("addr-1")["news"]), 1)

    def test_reads_active_signals_exactly_once_per_call_regardless_of_result_count(self):
        watchlist.update_from_results([
            _make_result("addr-1", "AAA"), _make_result("addr-2", "BBB"), _make_result("addr-3", "CCC"),
        ])
        with mock.patch.object(watchlist, "_active_news_signals", return_value=[]) as mock_active, mock.patch.object(
            watchlist, "group_signals_by_asset", return_value={}
        ):
            watchlist.attach_news_signals([
                _make_result("addr-1", "AAA"), _make_result("addr-2", "BBB"), _make_result("addr-3", "CCC"),
            ])
        mock_active.assert_called_once()


class TestIsolationFromExecutionAndPositions(WatchlistTestCase):
    def test_module_source_does_not_import_wallet_portfolio_or_trading_modules(self):
        import inspect

        forbidden = ("src.wallet", "src.portfolio", "src.paper_portfolio", "src.live_trader", "src.paper_trader")
        source = inspect.getsource(watchlist)
        for module_name in forbidden:
            self.assertNotIn(module_name, source, f"{module_name} must never be imported by opportunity_watchlist.py")

    def test_uses_its_own_state_file_distinct_from_positions_files(self):
        self.assertNotIn("positions", str(watchlist.STATE_FILE.name).replace("opportunity_watchlist", ""))
        self.assertEqual(watchlist.STATE_FILE.name, "opportunity_watchlist.json")


if __name__ == "__main__":
    unittest.main()
