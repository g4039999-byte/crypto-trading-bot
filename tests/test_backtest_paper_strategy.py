"""Tests for the pure analysis functions in
scripts/backtest_paper_strategy.py -- the out-of-sample split (added
this session so a candidate strategy change can be checked against data
it wasn't picked to fit, not just an aggregate number) and the
expectancy-aware summary. Never touches data/snapshots.json or any real
state file -- every test builds its own tiny, synthetic Trade list.
"""

import unittest
from datetime import datetime, timedelta, timezone

from scripts.backtest_paper_strategy import (
    CANDIDATE,
    Strategy,
    Trade,
    _check_exit,
    _passes_entry,
    _row,
    _update_trailing_stop,
    _velocity_cap_applies,
    assign_fold_index,
    broad_scan_strategy,
    compute_fold_boundaries,
    compute_oos_cutoff,
    fold_stability_score,
    replay_token,
    split_tokens_into_groups,
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


class TestRow(unittest.TestCase):
    """_row() formats a summarize()/full_report() dict for the
    comparison printouts -- an empty bucket (e.g. a variant whose
    out-of-sample slice happens to have zero trades) is a completely
    normal, expected outcome, not a reason to crash a whole comparison
    run (regression: this used to raise TypeError formatting None).
    """

    def test_does_not_raise_on_a_completely_empty_summary(self):
        empty = summarize("empty", [], verbose=False)
        row = _row("out-of-sample", empty)
        self.assertIn("n=   0", row)
        self.assertIn("n/a", row)

    def test_a_populated_summary_shows_real_numbers_not_n_a(self):
        trades = [_trade("2026-09-01T00:00:00+00:00", 1.0), _trade("2026-09-01T00:00:00+00:00", -1.0)]
        summary = summarize("mix", trades, verbose=False)
        row = _row("full", summary)
        self.assertNotIn("n/a", row)


class TestBroadScanStrategy(unittest.TestCase):
    def test_defaults_to_candidate_with_score_and_trend_gates_removed(self):
        strategy = broad_scan_strategy()
        self.assertEqual(strategy.min_score, 0)
        self.assertEqual(set(strategy.entry_trends), {"STRONG", "RISING", "NEUTRAL", "WEAK"})
        self.assertIsNone(strategy.trend_score_override)
        self.assertIsNone(strategy.max_velocity_pct_per_min)
        self.assertIsNone(strategy.velocity_spike_threshold_pct_per_min)
        self.assertFalse(strategy.require_trend_persistence)
        self.assertIsNone(strategy.min_buy_ratio)

    def test_preserves_risk_protections_from_the_base_strategy(self):
        strategy = broad_scan_strategy()
        self.assertEqual(strategy.stop_loss_pct, CANDIDATE.stop_loss_pct)
        self.assertEqual(strategy.take_profit_pct, CANDIDATE.take_profit_pct)
        self.assertEqual(strategy.stop_loss_cooldown_minutes, CANDIDATE.stop_loss_cooldown_minutes)
        self.assertEqual(strategy.max_liq_drawdown_pct, CANDIDATE.max_liq_drawdown_pct)
        self.assertEqual(strategy.min_liquidity_usd, CANDIDATE.min_liquidity_usd)

    def test_accepts_a_different_base_strategy(self):
        custom = _entry_strategy(stop_loss_pct=30)
        strategy = broad_scan_strategy(custom)
        self.assertEqual(strategy.stop_loss_pct, 30)
        self.assertEqual(strategy.min_score, 0)


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


def _trailing_strategy(**overrides):
    base = dict(
        name="test-trail", min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
        min_liquidity_usd=5000, min_volume_24h_usd=25000,
        min_age_minutes=15, max_age_minutes=180,
        stop_loss_pct=25, take_profit_pct=None, max_holding_minutes=240,
        max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
        trailing_arm_pct=15, trailing_stop_pct=10,
    )
    base.update(overrides)
    return Strategy(**base)


def _entry_strategy(**overrides):
    base = dict(
        name="test-entry", min_score=40, entry_trends=("STRONG", "RISING", "NEUTRAL"),
        min_liquidity_usd=5000, min_volume_24h_usd=25000,
        min_age_minutes=15, max_age_minutes=180,
        stop_loss_pct=25, take_profit_pct=25, max_holding_minutes=240,
        max_liq_drawdown_pct=40, stop_loss_cooldown_minutes=60,
    )
    base.update(overrides)
    return Strategy(**base)


def _evaluated(**overrides):
    base = dict(
        score=60, trend="STRONG", liquidity=20000, volume=60000, age=20.0,
        buys=100, sells=50, price_usd=1.0, liq_drawdown_pct=0.0,
        velocity_pct_per_min=0.5,
    )
    base.update(overrides)
    return base


class TestPassesEntryWithTrendScoreOverride(unittest.TestCase):
    def test_neutral_is_unaffected_by_an_override_that_does_not_name_it(self):
        strategy = _entry_strategy(trend_score_override={"RISING": 55, "STRONG": 55})
        evaluated = _evaluated(trend="NEUTRAL", score=45)
        self.assertTrue(_passes_entry(evaluated, strategy))

    def test_strong_below_the_override_bar_is_rejected_even_though_it_clears_min_score(self):
        strategy = _entry_strategy(min_score=40, trend_score_override={"STRONG": 55})
        evaluated = _evaluated(trend="STRONG", score=45)  # clears min_score=40, not the override=55
        self.assertFalse(_passes_entry(evaluated, strategy))

    def test_strong_at_or_above_the_override_bar_is_accepted(self):
        strategy = _entry_strategy(trend_score_override={"STRONG": 55})
        evaluated = _evaluated(trend="STRONG", score=55)
        self.assertTrue(_passes_entry(evaluated, strategy))

    def test_no_override_at_all_means_only_min_score_applies(self):
        strategy = _entry_strategy(min_score=40, trend_score_override=None)
        evaluated = _evaluated(trend="STRONG", score=45)
        self.assertTrue(_passes_entry(evaluated, strategy))


class TestPassesEntryWithVelocityCap(unittest.TestCase):
    def test_rejected_when_velocity_exceeds_the_cap(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=3.0)
        evaluated = _evaluated(velocity_pct_per_min=5.0)
        self.assertFalse(_passes_entry(evaluated, strategy))

    def test_accepted_when_velocity_is_at_or_below_the_cap(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=3.0)
        evaluated = _evaluated(velocity_pct_per_min=3.0)
        self.assertTrue(_passes_entry(evaluated, strategy))

    def test_no_cap_means_any_velocity_is_accepted(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=None)
        evaluated = _evaluated(velocity_pct_per_min=999.0)
        self.assertTrue(_passes_entry(evaluated, strategy))


class TestPassesEntryWithTrendPersistence(unittest.TestCase):
    def test_strong_rejected_with_no_previous_evaluated_point_at_all(self):
        strategy = _entry_strategy(require_trend_persistence=True)
        evaluated = _evaluated(trend="STRONG")
        self.assertFalse(_passes_entry(evaluated, strategy, previous_evaluated=None))

    def test_strong_rejected_when_the_previous_point_was_not_elevated(self):
        strategy = _entry_strategy(require_trend_persistence=True)
        evaluated = _evaluated(trend="STRONG")
        previous = _evaluated(trend="NEUTRAL")
        self.assertFalse(_passes_entry(evaluated, strategy, previous_evaluated=previous))

    def test_strong_accepted_when_the_previous_point_was_also_elevated(self):
        strategy = _entry_strategy(require_trend_persistence=True)
        evaluated = _evaluated(trend="STRONG")
        previous = _evaluated(trend="RISING")  # elevated, doesn't need to be the exact same label
        self.assertTrue(_passes_entry(evaluated, strategy, previous_evaluated=previous))

    def test_neutral_is_never_affected_by_the_persistence_requirement(self):
        strategy = _entry_strategy(require_trend_persistence=True)
        evaluated = _evaluated(trend="NEUTRAL")
        self.assertTrue(_passes_entry(evaluated, strategy, previous_evaluated=None))


class TestVelocityCapApplies(unittest.TestCase):
    """The 'smart' velocity-cap qualifiers (round 2, 2026-09-04) -- with
    none of velocity_cap_trends/velocity_cap_max_liquidity_usd/
    velocity_cap_min_relative_volume set, the cap applies unconditionally
    (the original round-1 behavior).
    """

    def test_applies_unconditionally_with_no_qualifiers_set(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=4.0)
        self.assertTrue(_velocity_cap_applies(_evaluated(trend="NEUTRAL", liquidity=1_000_000, relative_volume=0.1), strategy))

    def test_trend_qualifier_excludes_trends_not_listed(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=4.0, velocity_cap_trends=("RISING",))
        self.assertFalse(_velocity_cap_applies(_evaluated(trend="STRONG"), strategy))
        self.assertTrue(_velocity_cap_applies(_evaluated(trend="RISING"), strategy))

    def test_liquidity_qualifier_only_applies_below_the_threshold(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=4.0, velocity_cap_max_liquidity_usd=20000)
        self.assertFalse(_velocity_cap_applies(_evaluated(liquidity=25000), strategy))  # deep pool -- cap does not apply
        self.assertTrue(_velocity_cap_applies(_evaluated(liquidity=10000), strategy))  # thin pool -- cap applies

    def test_relative_volume_qualifier_only_applies_at_or_above_the_threshold(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=4.0, velocity_cap_min_relative_volume=15.0)
        self.assertFalse(_velocity_cap_applies(_evaluated(relative_volume=5.0), strategy))
        self.assertTrue(_velocity_cap_applies(_evaluated(relative_volume=15.0), strategy))

    def test_qualifiers_combine_with_and_semantics(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=4.0, velocity_cap_trends=("RISING",), velocity_cap_max_liquidity_usd=20000)
        # Right trend, but liquidity too deep -- must not apply.
        self.assertFalse(_velocity_cap_applies(_evaluated(trend="RISING", liquidity=25000), strategy))
        # Both conditions met.
        self.assertTrue(_velocity_cap_applies(_evaluated(trend="RISING", liquidity=10000), strategy))


class TestPassesEntryWithSmartVelocityCap(unittest.TestCase):
    def test_a_qualified_cap_rejects_only_the_targeted_trend(self):
        strategy = _entry_strategy(max_velocity_pct_per_min=4.0, velocity_cap_trends=("RISING",))
        self.assertFalse(_passes_entry(_evaluated(trend="RISING", velocity_pct_per_min=10.0), strategy))
        self.assertTrue(_passes_entry(_evaluated(trend="STRONG", velocity_pct_per_min=10.0), strategy))


class TestVelocitySpikeCooldownInReplayToken(unittest.TestCase):
    """End-to-end through replay_token(): a token seen moving faster than
    velocity_spike_threshold_pct_per_min gets a temporary per-token
    entry delay (velocity_spike_cooldown_minutes), not a permanent
    reject -- once it cools off and the cooldown lapses, it can still be
    entered if it otherwise qualifies.
    """

    def _history(self, prices, *, liquidity=20000, volume=60000, start=None):
        start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
        return [
            {
                "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
                "price_usd": p, "liquidity_usd": liquidity, "volume_24h": volume,
                "buys_24h": 100 + 10 * i, "sells_24h": 50 + 2 * i,
            }
            for i, p in enumerate(prices)
        ]

    def test_entry_is_delayed_past_the_cooldown_not_permanently_blocked(self):
        # +100% right at the earliest possible entry point (idx=3,
        # age=15m) gives velocity=6.67%/min there, then the price goes
        # flat -- velocity keeps decaying afterward purely because it is
        # cumulative-change-since-first-seen divided by a growing age
        # (idx=4/age=20 -> 5.0%/min, idx=5/age=25 -> 4.0%/min, ...).
        # The extra final bar at 3.0 (+50% from the 2.0 entry price, past
        # the 25% take-profit) is only there so both positions actually
        # CLOSE within this history and show up in replay_token()'s
        # returned trades -- an open position at the end of history data
        # is dropped, not returned, same as the live system just hasn't
        # decided yet.
        prices = [1.0, 1.0, 1.0] + [2.0] * 20 + [3.0]
        history = self._history(prices)

        # A HARD cap at the same threshold lets it in as soon as
        # velocity next drops to/under 5.0%/min -- idx=4 (age=20m).
        hard_cap_strategy = _entry_strategy(min_age_minutes=15, max_velocity_pct_per_min=5.0)
        hard_cap_trades = replay_token("tok", history, hard_cap_strategy)
        self.assertEqual(len(hard_cap_trades), 1)
        self.assertEqual(hard_cap_trades[0].entry_age_minutes, 20.0)

        # The SPIKE-COOLDOWN mechanism, same 5.0%/min threshold, delays
        # entry for a full 60 minutes from the spike itself (idx=3,
        # age=15) regardless of how quickly velocity itself decays --
        # first eligible re-check is age=75 (15 + 60), strictly later
        # than the hard cap's age=20.
        cooldown_strategy = _entry_strategy(
            min_age_minutes=15, velocity_spike_threshold_pct_per_min=5.0, velocity_spike_cooldown_minutes=60,
        )
        cooldown_trades = replay_token("tok", history, cooldown_strategy)
        self.assertEqual(len(cooldown_trades), 1)
        self.assertEqual(cooldown_trades[0].entry_age_minutes, 75.0)
        self.assertGreater(cooldown_trades[0].entry_age_minutes, hard_cap_trades[0].entry_age_minutes)

    def test_disabled_by_default_matches_original_behavior(self):
        strategy = _entry_strategy()  # neither velocity_spike field set
        self.assertIsNone(strategy.velocity_spike_threshold_pct_per_min)
        self.assertIsNone(strategy.velocity_spike_cooldown_minutes)


class TestUpdateTrailingStop(unittest.TestCase):
    def test_disabled_when_trailing_stop_pct_is_none(self):
        strategy = _trailing_strategy(trailing_stop_pct=None)
        result = _update_trailing_stop(strategy, entry_price=100.0, peak_price=200.0, existing_trailing_stop=None)
        self.assertIsNone(result)

    def test_not_armed_until_peak_reaches_the_arm_level(self):
        strategy = _trailing_strategy(trailing_arm_pct=15, trailing_stop_pct=10)
        result = _update_trailing_stop(strategy, entry_price=100.0, peak_price=110.0, existing_trailing_stop=None)
        self.assertIsNone(result)  # only +10% above entry, arm level is +15%

    def test_arms_and_computes_the_trail_level_once_peak_clears_the_arm_level(self):
        strategy = _trailing_strategy(trailing_arm_pct=15, trailing_stop_pct=10)
        result = _update_trailing_stop(strategy, entry_price=100.0, peak_price=120.0, existing_trailing_stop=None)
        self.assertAlmostEqual(result, 120.0 * 0.90)

    def test_zero_arm_pct_means_armed_immediately_from_entry(self):
        strategy = _trailing_strategy(trailing_arm_pct=0, trailing_stop_pct=15)
        result = _update_trailing_stop(strategy, entry_price=100.0, peak_price=100.0, existing_trailing_stop=None)
        self.assertAlmostEqual(result, 85.0)

    def test_only_ever_ratchets_up_never_down(self):
        strategy = _trailing_strategy(trailing_arm_pct=15, trailing_stop_pct=10)
        armed_high = _update_trailing_stop(strategy, entry_price=100.0, peak_price=150.0, existing_trailing_stop=None)
        # price pulls back from its peak -- the trail must not follow it down
        result = _update_trailing_stop(strategy, entry_price=100.0, peak_price=130.0, existing_trailing_stop=armed_high)
        self.assertEqual(result, armed_high)


class TestCheckExitWithTrailing(unittest.TestCase):
    def test_hard_stop_loss_fires_even_with_an_armed_trailing_stop(self):
        # a crash straight through both levels at once -- the hard floor
        # must still be respected, never loosened by trailing having armed.
        reason = _check_exit(100.0, _ts_now(), 70.0, _ts_now(), _trailing_strategy(), trailing_stop_price=90.0)
        self.assertEqual(reason, "stop_loss")

    def test_trailing_stop_fires_when_price_falls_to_its_level(self):
        reason = _check_exit(100.0, _ts_now(), 91.0, _ts_now(), _trailing_strategy(), trailing_stop_price=92.0)
        self.assertEqual(reason, "trailing_stop")

    def test_no_exit_while_above_every_level(self):
        reason = _check_exit(100.0, _ts_now(), 130.0, _ts_now(), _trailing_strategy(), trailing_stop_price=108.0)
        self.assertIsNone(reason)

    def test_uncapped_take_profit_never_fires_on_its_own(self):
        strategy = _trailing_strategy(take_profit_pct=None)
        reason = _check_exit(100.0, _ts_now(), 500.0, _ts_now(), strategy, trailing_stop_price=None)
        self.assertIsNone(reason)  # a 5x move with no trailing stop armed (armed only via peak tracking in replay_token) and no cap -- must not spuriously exit

    def test_capped_take_profit_still_fires_when_set(self):
        strategy = _trailing_strategy(take_profit_pct=50)
        reason = _check_exit(100.0, _ts_now(), 151.0, _ts_now(), strategy, trailing_stop_price=None)
        self.assertEqual(reason, "take_profit")


def _ts_now():
    return datetime.now(timezone.utc)


class TestReplayTokenWithTrailingStop(unittest.TestCase):
    """End-to-end through replay_token() -- the trailing-stop machinery
    wired into the actual replay loop, not just the pure helper
    functions above.
    """

    def _history(self, prices, *, liquidity=20000, volume=60000, start=None):
        # buys_24h/sells_24h must actually GROW between snapshots, with
        # buys outpacing sells -- src.observation.compute_trend derives
        # buy_pressure from the DELTA between consecutive snapshots, not
        # the absolute count, so a constant buys/sells figure (delta=0)
        # always classifies as WEAK regardless of price action.
        start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
        return [
            {
                "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
                "price_usd": p, "liquidity_usd": liquidity, "volume_24h": volume,
                "buys_24h": 100 + 10 * i, "sells_24h": 50 + 2 * i,
            }
            for i, p in enumerate(prices)
        ]

    def test_a_rally_then_pullback_exits_via_trailing_stop_not_the_hard_stop(self):
        # Steady climb to +30% above the eventual entry price, then a
        # pullback that never touches the 25% hard stop but does fall
        # through the trailing level.
        prices = [1.00] * 5 + [1.05, 1.10, 1.15, 1.20, 1.25, 1.30, 1.20, 1.10]
        history = self._history(prices)
        strategy = _trailing_strategy(trailing_arm_pct=15, trailing_stop_pct=10, stop_loss_pct=25)
        trades = replay_token("tok", history, strategy)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "trailing_stop")

    def test_never_exits_via_trailing_stop_if_price_never_arms_it(self):
        prices = [1.00] * 5 + [1.05, 1.08, 1.05, 1.02, 0.99, 0.80, 0.70]  # peak +8% never clears the 15% arm level, eventually hits the 25% hard stop (entry at 1.00, stop at 0.75)
        history = self._history(prices)
        strategy = _trailing_strategy(trailing_arm_pct=15, trailing_stop_pct=10, stop_loss_pct=25)
        trades = replay_token("tok", history, strategy)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "stop_loss")


class TestFoldHelpers(unittest.TestCase):
    def _snapshots_with_timestamps(self, timestamps):
        return {"tok-a": [{"timestamp": ts} for ts in timestamps]}

    def test_compute_fold_boundaries_returns_n_minus_one_cutoffs(self):
        timestamps = [f"2026-09-0{d}T00:00:00+00:00" for d in range(1, 5)]
        snapshots = self._snapshots_with_timestamps(timestamps)
        boundaries = compute_fold_boundaries(snapshots, n_folds=4)
        self.assertEqual(len(boundaries), 3)
        self.assertEqual(boundaries, sorted(boundaries))

    def test_assign_fold_index_is_monotonic_with_time(self):
        boundaries = [_ts("2026-09-02T00:00:00+00:00"), _ts("2026-09-03T00:00:00+00:00")]
        self.assertEqual(assign_fold_index(_ts("2026-09-01T00:00:00+00:00"), boundaries), 0)
        self.assertEqual(assign_fold_index(_ts("2026-09-02T12:00:00+00:00"), boundaries), 1)
        self.assertEqual(assign_fold_index(_ts("2026-09-04T00:00:00+00:00"), boundaries), 2)

    def test_fold_stability_score_is_zero_with_no_trades(self):
        boundaries = [_ts("2026-09-02T00:00:00+00:00")]
        self.assertEqual(fold_stability_score([], boundaries), 0.0)

    def test_fold_stability_score_counts_only_folds_with_a_positive_edge(self):
        boundaries = [_ts("2026-09-02T00:00:00+00:00")]  # 2 folds
        # Fold 0: consistently profitable. Fold 1: consistently losing.
        trades = [
            _trade("2026-09-01T00:00:00+00:00", 2.0), _trade("2026-09-01T01:00:00+00:00", 2.0),
            _trade("2026-09-03T00:00:00+00:00", -2.0), _trade("2026-09-03T01:00:00+00:00", -2.0),
        ]
        self.assertEqual(fold_stability_score(trades, boundaries), 0.5)

    def test_split_tokens_into_groups_is_deterministic_and_covers_every_token(self):
        snapshots = {f"addr-{i}": [{"timestamp": "2026-09-01T00:00:00+00:00"}] for i in range(20)}
        groups_a = split_tokens_into_groups(snapshots, n_groups=2)
        groups_b = split_tokens_into_groups(snapshots, n_groups=2)
        self.assertEqual({k for g in groups_a for k in g}, set(snapshots.keys()))
        self.assertEqual([set(g.keys()) for g in groups_a], [set(g.keys()) for g in groups_b])


if __name__ == "__main__":
    unittest.main()
