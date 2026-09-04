"""Tests for the pure helper functions in
scripts/diagnose_paper_strategy.py -- a read-only diagnostic tool, never
touches data/snapshots.json or any real state file in these tests.
"""

import unittest
from datetime import datetime, timezone

from scripts.backtest_paper_strategy import Trade
from scripts.diagnose_paper_strategy import (
    categorical_buckets,
    correlation_or_none,
    tercile_buckets,
)


def _ts():
    return datetime.now(timezone.utc)


def _trade(pnl_pct, **extra):
    fields = dict(
        token="tok", entry_idx=0, entry_ts=_ts(), entry_price=1.0,
        entry_score=50, entry_trend="STRONG", entry_age_minutes=10.0,
        seconds_since_first_seen=100.0, exit_ts=_ts(), exit_price=1.0,
        reason="take_profit", pnl_usd=pnl_pct, pnl_pct=pnl_pct,
    )
    fields.update(extra)
    return Trade(**fields)


class TestTercileBuckets(unittest.TestCase):
    def test_splits_into_three_roughly_equal_groups_by_value(self):
        trades = [_trade(0, entry_score=i) for i in range(9)]
        buckets = tercile_buckets(trades, lambda t: t.entry_score)
        self.assertEqual(len(buckets["LOW"]), 3)
        self.assertEqual(len(buckets["MID"]), 3)
        self.assertEqual(len(buckets["HIGH"]), 3)
        self.assertEqual([t.entry_score for t in buckets["LOW"]], [0, 1, 2])
        self.assertEqual([t.entry_score for t in buckets["HIGH"]], [6, 7, 8])

    def test_excludes_trades_where_the_key_is_none(self):
        trades = [_trade(0, entry_score=i) for i in range(6)] + [_trade(0, entry_score=None)]
        buckets = tercile_buckets(trades, lambda t: t.entry_score)
        total = len(buckets["LOW"]) + len(buckets["MID"]) + len(buckets["HIGH"])
        self.assertEqual(total, 6)

    def test_too_few_trades_returns_empty_buckets_not_a_crash(self):
        trades = [_trade(0, entry_score=1), _trade(0, entry_score=2)]
        buckets = tercile_buckets(trades, lambda t: t.entry_score)
        self.assertEqual(buckets, {"LOW": [], "MID": [], "HIGH": []})


class TestCategoricalBuckets(unittest.TestCase):
    def test_groups_by_the_key_functions_exact_return_value(self):
        trades = [_trade(0, entry_trend="STRONG"), _trade(0, entry_trend="STRONG"), _trade(0, entry_trend="NEUTRAL")]
        buckets = categorical_buckets(trades, lambda t: t.entry_trend)
        self.assertEqual(len(buckets["STRONG"]), 2)
        self.assertEqual(len(buckets["NEUTRAL"]), 1)


class TestCorrelationOrNone(unittest.TestCase):
    def test_returns_a_strong_positive_correlation_for_a_linear_relationship(self):
        xs = [1, 2, 3, 4, 5]
        ys = [2, 4, 6, 8, 10]
        r = correlation_or_none(xs, ys)
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_returns_none_with_fewer_than_three_points(self):
        self.assertIsNone(correlation_or_none([1, 2], [1, 2]))

    def test_returns_none_when_every_x_value_is_identical(self):
        self.assertIsNone(correlation_or_none([5, 5, 5], [1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
