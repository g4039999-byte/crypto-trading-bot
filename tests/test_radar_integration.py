"""End-to-end pipeline test using fixture data -- no network calls.

Confirms the observation/momentum/scoring/stage/snapshot stages are wired
together correctly by radar.run_radar(), without depending on the live
DexScreener API (which this sandbox cannot reach anyway).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_file = Path(self._tmp_dir.name) / "snapshots.json"
        self._patcher = mock.patch.object(snapshot, "SNAPSHOT_FILE", tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
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


if __name__ == "__main__":
    unittest.main()
