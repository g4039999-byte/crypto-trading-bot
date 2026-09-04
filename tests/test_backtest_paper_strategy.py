"""Tests for the pure analysis functions in
scripts/backtest_paper_strategy.py -- the out-of-sample split (added
this session so a candidate strategy change can be checked against data
it wasn't picked to fit, not just an aggregate number) and the
expectancy-aware summary. Never touches data/snapshots.json or any real
state file -- every test builds its own tiny, synthetic Trade list.
"""

import unittest
from datetime import datetime, timezone

from scripts.backtest_paper_strategy import (
    Trade,
    compute_oos_cutoff,
    split_trades_by_cutoff,
    summarize,
    summarize_with_oos,
)


def _ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _trade(entry_ts, pnl_usd, reason="take_profit"):
    return Trade(
        token="tok", entry_idx=0, entry_ts=_ts(entry_ts), entry_price=1.0,
        entry_score=50, entry_trend="STRONG", entry_age_minutes=10.0,
        seconds_since_first_seen=100.0, exit_ts=_ts(entry_ts), exit_price=1.0,
        reason=reason, pnl_usd=pnl_usd, pnl_pct=pnl_usd,
    )


class TestComputeOosCutoff(unittest.TestCase):
    def _snapshots_with_timestamps(self, timestamps):
        return {"tok-a": [{"timestamp": ts} for ts in timestamps]}

    def test_cutoff_is_the_requested_percentile_of_snapshot_density_not_the_range_midpoint(self):
        # 9 snapshots tightly packed at the end, 1 far in the past -- a
        # midpoint-of-range cutoff would sit in the middle of a decade-
        # long gap; the percentile-of-density cutoff should not.
        timestamps = ["2020-01-01T00:00:00+00:00"] + [f"2026-09-04T00:0{i}:00+00:00" for i in range(9)]
        snapshots = self._snapshots_with_timestamps(timestamps)
        cutoff = compute_oos_cutoff(snapshots, in_sample_fraction=0.7)
        # 70th percentile of 10 sorted timestamps (index 7) is one of the
        # tightly-packed recent ones, not anywhere near 2020.
        self.assertGreater(cutoff.year, 2025)

    def test_higher_in_sample_fraction_yields_a_later_cutoff(self):
        timestamps = [f"2026-09-0{d}T00:00:00+00:00" for d in range(1, 5)]
        snapshots = self._snapshots_with_timestamps(timestamps)
        low = compute_oos_cutoff(snapshots, in_sample_fraction=0.25)
        high = compute_oos_cutoff(snapshots, in_sample_fraction=0.75)
        self.assertLess(low, high)

    def test_raises_on_empty_dataset(self):
        with self.assertRaises(ValueError):
            compute_oos_cutoff({})


class TestSplitTradesByCutoff(unittest.TestCase):
    def test_splits_by_entry_time_not_exit_time(self):
        cutoff = _ts("2026-09-02T00:00:00+00:00")
        before = _trade("2026-09-01T00:00:00+00:00", 1.0)
        after = _trade("2026-09-03T00:00:00+00:00", -1.0)
        in_sample, out_of_sample = split_trades_by_cutoff([before, after], cutoff)
        self.assertEqual(in_sample, [before])
        self.assertEqual(out_of_sample, [after])

    def test_a_trade_entered_exactly_at_the_cutoff_is_out_of_sample(self):
        cutoff = _ts("2026-09-02T00:00:00+00:00")
        at_cutoff = _trade("2026-09-02T00:00:00+00:00", 1.0)
        in_sample, out_of_sample = split_trades_by_cutoff([at_cutoff], cutoff)
        self.assertEqual(in_sample, [])
        self.assertEqual(out_of_sample, [at_cutoff])

    def test_empty_list_splits_into_two_empty_lists(self):
        self.assertEqual(split_trades_by_cutoff([], _ts("2026-09-02T00:00:00+00:00")), ([], []))


class TestSummarize(unittest.TestCase):
    def test_empty_trade_list_returns_zeroed_summary_not_none(self):
        summary = summarize("empty", [], verbose=False)
        self.assertEqual(summary["n"], 0)
        self.assertEqual(summary["expectancy"], 0.0)

    def test_expectancy_is_total_pnl_over_trade_count(self):
        trades = [_trade("2026-09-01T00:00:00+00:00", 4.0), _trade("2026-09-01T00:00:00+00:00", -2.0)]
        summary = summarize("mix", trades, verbose=False)
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["total_pnl"], 2.0)
        self.assertAlmostEqual(summary["expectancy"], 1.0)

    def test_wins_and_losses_are_classified_by_pnl_sign(self):
        trades = [_trade("2026-09-01T00:00:00+00:00", 1.0), _trade("2026-09-01T00:00:00+00:00", 0.0), _trade("2026-09-01T00:00:00+00:00", -1.0)]
        summary = summarize("mix", trades, verbose=False)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 2)  # a breakeven (pnl_usd == 0) trade counts as a loss, not a win


class TestSummarizeWithOos(unittest.TestCase):
    def test_in_sample_and_out_of_sample_partitions_sum_back_to_the_full_dataset(self):
        cutoff = _ts("2026-09-02T00:00:00+00:00")
        trades = [
            _trade("2026-09-01T00:00:00+00:00", 1.0),
            _trade("2026-09-01T12:00:00+00:00", 2.0),
            _trade("2026-09-03T00:00:00+00:00", -1.0),
        ]
        result = summarize_with_oos("test", trades, cutoff, verbose=False)
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["in_sample"]["n"] + result["out_of_sample"]["n"], 3)
        self.assertEqual(result["in_sample"]["n"], 2)
        self.assertEqual(result["out_of_sample"]["n"], 1)

    def test_no_trades_on_either_side_of_the_cutoff_is_handled_without_raising(self):
        cutoff = _ts("2026-09-02T00:00:00+00:00")
        result = summarize_with_oos("empty", [], cutoff, verbose=False)
        self.assertEqual(result["n"], 0)
        self.assertEqual(result["in_sample"]["n"], 0)
        self.assertEqual(result["out_of_sample"]["n"], 0)


if __name__ == "__main__":
    unittest.main()
