"""End-to-end pipeline test using fixture data -- no network calls.

Confirms the observation/momentum/scoring/stage/snapshot stages are wired
together correctly by radar.run_radar(), without depending on the live
DexScreener API (which this sandbox cannot reach anyway).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.news_signal_engine as news_signal_engine
import src.opportunity_watchlist as opportunity_watchlist
import src.snapshot as snapshot
from src import radar

FIXTURE_PAIRS = [
    {
        "baseToken": {"symbol": "GOOD", "address": "addr-good"},
        "liquidity": {"usd": 50000},
        "volume": {"h24": 200000},
        "priceChange": {"h24": 40},
        "txns": {"h24": {"buys": 800, "sells": 200}},
        "pairCreatedAt": None,
    },
    {
        "baseToken": {"symbol": "WEAK", "address": "addr-weak"},
        "liquidity": {"usd": 1000},
        "volume": {"h24": 500},
        "priceChange": {"h24": -5},
        "txns": {"h24": {"buys": 2, "sells": 20}},
        "pairCreatedAt": None,
    },
    {
        # Deliberately malformed / partial data (nulls where DexScreener
        # sometimes omits or nulls a field) -- must not crash the run.
        "baseToken": {"symbol": "NULLY", "address": "addr-nully"},
        "liquidity": None,
        "volume": {"h24": None},
        "priceChange": None,
        "txns": None,
        "pairCreatedAt": None,
    },
    # A pair with no baseToken / totally malformed shape.
    {"unexpected": "shape"},
]


class TestRadarIntegration(unittest.TestCase):
    def setUp(self):
        # radar.run_radar() now also calls opportunity_watchlist.update_from_results()
        # and attach_news_signals() unconditionally every cycle (see src/radar.py) --
        # those two need their own isolated state files here for the same reason
        # snapshots does, otherwise every run of this test would write its fixture
        # data ("GOOD"/"addr-good", etc.) into the real data/opportunity_watchlist.json
        # and data/news_signals.json on disk.
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_snapshots = Path(self._tmp_dir.name) / "snapshots.json"
        tmp_watchlist = Path(self._tmp_dir.name) / "opportunity_watchlist.json"
        tmp_news = Path(self._tmp_dir.name) / "news_signals.json"

        self._patchers = [
            mock.patch.object(snapshot, "SNAPSHOT_FILE", tmp_snapshots),
            mock.patch.object(opportunity_watchlist, "STATE_FILE", tmp_watchlist),
            mock.patch.object(news_signal_engine, "STATE_FILE", tmp_news),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._tmp_dir.cleanup()

    def test_run_radar_end_to_end_with_fixture_data(self):
        with mock.patch(
            "src.radar.fetch_solana_token_addresses",
            return_value=["addr-good", "addr-weak", "addr-nully", "addr-missing"],
        ), mock.patch("src.radar.fetch_pairs", return_value=FIXTURE_PAIRS):
            results = radar.run_radar()

        # Every pair survives, including the one with a totally
        # unexpected shape -- it just scores 0 / gets rejected instead of
        # crashing the run.
        self.assertEqual(len(results), 4)

        symbols = {r["symbol"] for r in results}
        self.assertEqual(symbols, {"GOOD", "WEAK", "NULLY", "?"})

        unexpected = next(r for r in results if r["symbol"] == "?")
        self.assertFalse(unexpected["ok"])
        self.assertEqual(unexpected["score"], 0)

        # Results are ranked highest score first.
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

        good = next(r for r in results if r["symbol"] == "GOOD")
        self.assertTrue(good["ok"])

        nully = next(r for r in results if r["symbol"] == "NULLY")
        self.assertFalse(nully["ok"])
        self.assertEqual(nully["score"], 0)

    def test_run_radar_returns_empty_list_when_no_addresses_found(self):
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=[]):
            results = radar.run_radar()
        self.assertEqual(results, [])

    def test_watchlist_keeps_a_previously_seen_token_in_the_query_and_builds_a_real_trend(self):
        good_pair = FIXTURE_PAIRS[0]  # addr-good / GOOD

        # Cycle 1: addr-good is newly discovered -- first snapshot saved,
        # trend is necessarily INSUFFICIENT_DATA (only one data point).
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), mock.patch(
            "src.radar.fetch_pairs", return_value=[good_pair]
        ) as mock_fetch_pairs:
            results_cycle_1 = radar.run_radar()

        mock_fetch_pairs.assert_called_once_with(["addr-good"])
        self.assertEqual(results_cycle_1[0]["trend"], "INSUFFICIENT_DATA")

        # Cycle 2: DexScreener's "latest profiles" feed no longer surfaces
        # addr-good (it discovers something else instead) -- the
        # watchlist should still pull addr-good back in, giving it a
        # second snapshot and therefore a real trend.
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-other"]), mock.patch(
            "src.radar.fetch_pairs", return_value=[good_pair]
        ) as mock_fetch_pairs:
            results_cycle_2 = radar.run_radar()

        queried_addresses = mock_fetch_pairs.call_args[0][0]
        self.assertIn("addr-good", queried_addresses)
        self.assertIn("addr-other", queried_addresses)

        good_result = next(r for r in results_cycle_2 if r["symbol"] == "GOOD")
        self.assertNotEqual(good_result["trend"], "INSUFFICIENT_DATA")
        self.assertIn(good_result["trend"], ("STRONG", "RISING", "NEUTRAL", "WEAK"))

    def test_results_have_x_defaults_when_x_is_not_configured(self):
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_pairs", return_value=[FIXTURE_PAIRS[0]]):
            results = radar.run_radar()

        good = next(r for r in results if r["symbol"] == "GOOD")
        self.assertFalse(good["x_trend_detected"])
        self.assertEqual(good["social_score_bonus"], 0)
        self.assertFalse(good["possible_clone"])

    def test_x_signal_correlation_adds_a_bonus_and_never_required(self):
        fake_signal = {"entity": "GOOD", "confidence": 0.8, "independent_mentions": 4,
                        "velocity_per_minute": 1.5, "source_quality": 1.2, "is_possible_clone": False}
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_pairs", return_value=[FIXTURE_PAIRS[0]]), \
                mock.patch.object(radar.x_intelligence, "maybe_poll_and_update", return_value=1), \
                mock.patch.object(radar.x_intelligence, "get_active_trends", return_value=[{"entity": "GOOD"}]), \
                mock.patch.object(radar.x_intelligence, "social_signal_for_token", return_value=fake_signal):
            with_signal = radar.run_radar()

        good = next(r for r in with_signal if r["symbol"] == "GOOD")
        self.assertTrue(good["x_trend_detected"])
        self.assertGreater(good["social_score_bonus"], 0)

        # Same fixture, no X signal at all -- still evaluates and scores
        # the token; X is additive, never a gate.
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_pairs", return_value=[FIXTURE_PAIRS[0]]):
            without_signal = radar.run_radar()

        good_no_signal = next(r for r in without_signal if r["symbol"] == "GOOD")
        self.assertTrue(good_no_signal["ok"])
        self.assertEqual(good_no_signal["social_score_bonus"], 0)

    def test_run_radar_survives_x_intelligence_raising_entirely(self):
        """The core resilience guarantee: if every X-related call
        explodes, run_radar() must still return normal results, not
        crash the whole radar/paper-trading cycle.
        """
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_pairs", return_value=[FIXTURE_PAIRS[0]]), \
                mock.patch.object(radar.x_intelligence, "maybe_poll_and_update", side_effect=RuntimeError("X is on fire")), \
                mock.patch.object(radar.x_intelligence, "get_active_trends", side_effect=RuntimeError("X is on fire")):
            results = radar.run_radar()

        self.assertEqual(len(results), 1)
        good = results[0]
        self.assertEqual(good["symbol"], "GOOD")
        self.assertTrue(good["ok"])
        self.assertFalse(good["x_trend_detected"])

    def test_pumpfun_discovered_addresses_are_merged_into_the_query(self):
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_latest_launch_addresses", return_value=["addr-weak"]), \
                mock.patch("src.radar.fetch_pairs", return_value=FIXTURE_PAIRS[:2]) as mocked_fetch_pairs:
            radar.run_radar()

        queried_addresses = mocked_fetch_pairs.call_args[0][0]
        self.assertIn("addr-good", queried_addresses)
        self.assertIn("addr-weak", queried_addresses)

    def test_pumpfun_discovery_failure_does_not_break_the_radar_cycle(self):
        """The core resilience guarantee, mirrored from X above: Pump.fun
        being unconfigured, down, or erroring must never affect the
        radar's own DexScreener-sourced results.
        """
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_latest_launch_addresses", side_effect=RuntimeError("Pump.fun is down")), \
                mock.patch("src.radar.fetch_pairs", return_value=[FIXTURE_PAIRS[0]]):
            results = radar.run_radar()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["symbol"], "GOOD")

    def test_pumpfun_returning_nothing_is_not_treated_as_an_error(self):
        with mock.patch("src.radar.fetch_solana_token_addresses", return_value=["addr-good"]), \
                mock.patch("src.radar.fetch_latest_launch_addresses", return_value=[]), \
                mock.patch("src.radar.fetch_pairs", return_value=[FIXTURE_PAIRS[0]]):
            results = radar.run_radar()
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
