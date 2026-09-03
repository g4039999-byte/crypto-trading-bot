import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from src.stocks import bar_cache
from tests.stocks.helpers import uptrend_bars


def _fake_provider(**results):
    provider = mock.Mock()
    provider.get_daily_bars_batch.return_value = results
    return provider


class TestBarCache(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(bar_cache, "CACHE_DIR", Path(self._tmp_dir.name))
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_a_cold_cache_fetches_every_symbol_from_the_provider(self):
        df = uptrend_bars(n=80)
        provider = _fake_provider(AAPL=df)

        result = bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)

        provider.get_daily_bars_batch.assert_called_once_with(["AAPL"], 80)
        self.assertEqual(len(result["AAPL"]), 80)

    def test_a_second_call_within_ttl_serves_from_disk_not_the_provider(self):
        df = uptrend_bars(n=80)
        provider = _fake_provider(AAPL=df)
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)

        provider.get_daily_bars_batch.reset_mock()
        result = bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)

        provider.get_daily_bars_batch.assert_not_called()
        self.assertEqual(len(result["AAPL"]), 80)

    def test_a_stale_cache_past_ttl_refetches(self):
        df = uptrend_bars(n=80)
        provider = _fake_provider(AAPL=df)
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)

        # Backdate the cache file's mtime past the TTL.
        cache_file = bar_cache._cache_path("AAPL")
        old_time = time.time() - 999999
        import os
        os.utime(cache_file, (old_time, old_time))

        provider.get_daily_bars_batch.reset_mock()
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)
        provider.get_daily_bars_batch.assert_called_once()

    def test_a_cache_entry_requested_shorter_than_the_new_request_is_treated_as_a_miss(self):
        short_df = uptrend_bars(n=30)
        provider = _fake_provider(AAPL=short_df)
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 30)  # requested (and got) 30 rows

        longer_df = uptrend_bars(n=100)
        provider2 = _fake_provider(AAPL=longer_df)
        result = bar_cache.get_daily_bars_batch_cached(provider2, ["AAPL"], 100)  # now needs more than was ever requested

        provider2.get_daily_bars_batch.assert_called_once()
        self.assertEqual(len(result["AAPL"]), 100)

    def test_a_young_listing_that_genuinely_has_less_history_than_requested_is_still_cached(self):
        # A stock with only ~30 real trading days of history (e.g. a
        # recent IPO) can never satisfy a 3650-day request no matter how
        # many times it's refetched -- the cache must still serve it
        # from disk on a second call instead of refetching forever.
        short_df = uptrend_bars(n=30)
        provider = _fake_provider(AAPL=short_df)
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 3650)  # asked for 3650, only 30 exist

        provider2 = _fake_provider(AAPL=short_df)
        result = bar_cache.get_daily_bars_batch_cached(provider2, ["AAPL"], 3650)

        provider2.get_daily_bars_batch.assert_not_called()  # served from disk, not refetched
        self.assertEqual(len(result["AAPL"]), 30)

    def test_a_cache_file_from_before_the_sidecar_existed_falls_back_to_row_count(self):
        df = uptrend_bars(n=30)
        bar_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(bar_cache._cache_path("AAPL"))  # no .meta.json sidecar written

        provider = _fake_provider(AAPL=uptrend_bars(n=100))
        result = bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 100)

        provider.get_daily_bars_batch.assert_called_once()  # conservative: treated as a miss, not silently served short
        self.assertEqual(len(result["AAPL"]), 100)

    def test_a_failed_fetch_is_never_cached(self):
        provider = _fake_provider(AAPL=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)

        self.assertFalse(bar_cache._cache_path("AAPL").exists())

    def test_mixed_hit_and_miss_only_fetches_the_missing_symbols(self):
        df = uptrend_bars(n=80)
        provider = _fake_provider(AAPL=df)
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)  # AAPL now cached

        provider2 = _fake_provider(MSFT=df)
        result = bar_cache.get_daily_bars_batch_cached(provider2, ["AAPL", "MSFT"], 80)

        provider2.get_daily_bars_batch.assert_called_once_with(["MSFT"], 80)
        self.assertEqual(set(result.keys()), {"AAPL", "MSFT"})

    def test_a_corrupt_cache_file_is_treated_as_a_miss_not_a_crash(self):
        bar_cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        bar_cache._cache_path("AAPL").write_text("not,valid,csv,\nat,all", encoding="utf-8")

        df = uptrend_bars(n=80)
        provider = _fake_provider(AAPL=df)
        result = bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)

        provider.get_daily_bars_batch.assert_called_once()
        self.assertEqual(len(result["AAPL"]), 80)

    def test_clear_cache_removes_every_cached_file_and_its_sidecar(self):
        df = uptrend_bars(n=80)
        provider = _fake_provider(AAPL=df)
        bar_cache.get_daily_bars_batch_cached(provider, ["AAPL"], 80)
        self.assertTrue(bar_cache._cache_path("AAPL").exists())
        self.assertTrue(bar_cache._meta_path("AAPL").exists())

        bar_cache.clear_cache()

        self.assertFalse(bar_cache._cache_path("AAPL").exists())
        self.assertFalse(bar_cache._meta_path("AAPL").exists())

    def test_clear_cache_on_a_missing_directory_does_not_raise(self):
        bar_cache.clear_cache()  # nothing cached yet -- CACHE_DIR doesn't even exist


if __name__ == "__main__":
    unittest.main()
