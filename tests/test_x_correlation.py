"""src/x_correlation.py: linking an X entity to real tokens, and the
clone-detection heuristic. Pure logic, no state/network involved.
"""

import unittest

import src.x_correlation as correlation


class TestCorrelate(unittest.TestCase):
    def test_exact_symbol_match(self):
        tokens = [{"symbol": "PEPITO", "address": "addr-1", "liquidity": 20000, "age": 30}]
        matches = correlation.correlate("PEPITO", tokens)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_type"], "exact")
        self.assertFalse(matches[0]["is_possible_clone"])

    def test_no_match_returns_empty_list(self):
        tokens = [{"symbol": "COMPLETELYDIFFERENT", "address": "addr-1", "liquidity": 20000, "age": 30}]
        self.assertEqual(correlation.correlate("PEPITO", tokens), [])

    def test_fuzzy_match_with_no_original_is_not_flagged_as_clone(self):
        # Only one similar-but-not-exact token exists -- nothing to be
        # "imitating", so it's a weaker correlation, not an accusation.
        tokens = [{"symbol": "PEPIT0", "address": "addr-1", "liquidity": 20000, "age": 30}]
        matches = correlation.correlate("PEPITO", tokens)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_type"], "fuzzy")
        self.assertFalse(matches[0]["is_possible_clone"])

    def test_fuzzy_match_flagged_as_clone_when_an_older_more_liquid_original_exists(self):
        tokens = [
            {"symbol": "PEPITO", "address": "addr-original", "liquidity": 100000, "age": 300},
            {"symbol": "PEP1TO", "address": "addr-clone", "liquidity": 3000, "age": 4},
        ]
        matches = correlation.correlate("PEPITO", tokens)
        by_address = {m["address"]: m for m in matches}
        self.assertFalse(by_address["addr-original"]["is_possible_clone"])
        self.assertTrue(by_address["addr-clone"]["is_possible_clone"])

    def test_exact_matches_sort_before_fuzzy_matches(self):
        tokens = [
            {"symbol": "PEP1TO", "address": "addr-fuzzy", "liquidity": 5000, "age": 10},
            {"symbol": "PEPITO", "address": "addr-exact", "liquidity": 5000, "age": 10},
        ]
        matches = correlation.correlate("PEPITO", tokens)
        self.assertEqual(matches[0]["address"], "addr-exact")

    def test_missing_symbol_is_skipped_safely(self):
        tokens = [{"symbol": "", "address": "addr-1"}, {"symbol": "?", "address": "addr-2"}]
        self.assertEqual(correlation.correlate("PEPITO", tokens), [])

    def test_empty_entity_or_no_candidates_returns_empty(self):
        self.assertEqual(correlation.correlate("", [{"symbol": "PEPITO", "address": "a"}]), [])
        self.assertEqual(correlation.correlate("PEPITO", []), [])


class TestSocialScoreForToken(unittest.TestCase):
    def test_returns_best_matching_signal_for_the_given_address(self):
        trend_summaries = [
            {"entity": "PEPITO", "confidence": 0.8, "independent_mentions": 5,
             "velocity_per_minute": 1.2, "avg_source_reputation": 1.1},
        ]
        tokens = [{"symbol": "PEPITO", "address": "addr-1", "liquidity": 20000, "age": 30}]

        signal = correlation.social_score_for_token("addr-1", trend_summaries, tokens)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["entity"], "PEPITO")
        self.assertEqual(signal["confidence"], 0.8)
        self.assertEqual(signal["source_quality"], 1.1)

    def test_returns_none_when_address_does_not_correlate_to_anything(self):
        trend_summaries = [
            {"entity": "PEPITO", "confidence": 0.8, "independent_mentions": 5,
             "velocity_per_minute": 1.2, "avg_source_reputation": 1.1},
        ]
        tokens = [{"symbol": "PEPITO", "address": "addr-1", "liquidity": 20000, "age": 30}]

        self.assertIsNone(correlation.social_score_for_token("addr-does-not-exist", trend_summaries, tokens))


if __name__ == "__main__":
    unittest.main()
