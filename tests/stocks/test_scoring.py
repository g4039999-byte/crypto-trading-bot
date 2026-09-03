import unittest
from unittest import mock

from src.stocks.scoring import best_strategy_signal, calculate_score
from src.stocks.features import compute_features
from tests.stocks.helpers import flat_bars, uptrend_bars


class TestBestStrategySignal(unittest.TestCase):
    def test_returns_none_when_everything_skips(self):
        results = {"a": {"action": "SKIP", "confidence": 0.0}, "b": {"action": "SKIP", "confidence": 0.0}}
        self.assertIsNone(best_strategy_signal(results))

    def test_picks_the_highest_confidence_buy_by_default(self):
        results = {
            "a": {"action": "BUY", "confidence": 0.3, "reason": "weak"},
            "b": {"action": "BUY", "confidence": 0.8, "reason": "strong"},
        }
        best = best_strategy_signal(results)
        self.assertEqual(best["strategy"], "b")

    def test_active_strategy_restricts_to_only_that_strategy(self):
        results = {
            "a": {"action": "BUY", "confidence": 0.9, "reason": "strong"},
            "b": {"action": "SKIP", "confidence": 0.0},
        }
        self.assertIsNone(best_strategy_signal(results, active_strategy="b"))
        best = best_strategy_signal(results, active_strategy="a")
        self.assertEqual(best["strategy"], "a")


class TestCalculateScore(unittest.TestCase):
    def test_no_signal_anywhere_scores_low_but_never_raises(self):
        df = flat_bars(n=80)
        features = compute_features(df)
        result = calculate_score(features, df)
        self.assertIsNone(result["best_strategy"])
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_a_real_setup_scores_meaningfully_higher_than_a_flat_market(self):
        flat_df = flat_bars(n=80)
        up_df = uptrend_bars(n=80, daily_gain_pct=0.5, volume=2_000_000)
        up_df.iloc[-1, up_df.columns.get_loc("volume")] = 5_000_000

        flat_score = calculate_score(compute_features(flat_df), flat_df)
        up_score = calculate_score(compute_features(up_df), up_df)
        self.assertGreater(up_score["score"], flat_score["score"])

    def test_social_bonus_is_capped_and_additive(self):
        df = flat_bars(n=80)
        features = compute_features(df)
        with mock.patch("src.stocks.scoring.STOCKS_X_SCORE_MAX_BONUS", 8):
            no_bonus = calculate_score(features, df, social_bonus=0)["score"]
            with_bonus = calculate_score(features, df, social_bonus=8)["score"]
            over_cap = calculate_score(features, df, social_bonus=999)["score"]
        self.assertGreaterEqual(with_bonus, no_bonus)
        self.assertEqual(with_bonus, over_cap)  # capped, not unbounded

    def test_score_never_exceeds_100_or_drops_below_0(self):
        df = uptrend_bars(n=80, daily_gain_pct=0.5, volume=2_000_000)
        result = calculate_score(compute_features(df), df, social_bonus=999, news_bonus=999)
        self.assertLessEqual(result["score"], 100)
        self.assertGreaterEqual(result["score"], 0)


if __name__ == "__main__":
    unittest.main()
