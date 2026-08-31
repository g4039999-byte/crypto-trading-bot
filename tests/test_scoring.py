import time
import unittest

from src.scoring import calculate_score


def make_pair(liquidity=10000, volume_h24=30000, change_h24=0, buys=60, sells=40, pair_created_at=None):
    return {
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume_h24},
        "priceChange": {"h24": change_h24},
        "txns": {"h24": {"buys": buys, "sells": sells}},
        "pairCreatedAt": pair_created_at,
    }


class TestCalculateScore(unittest.TestCase):
    def test_missing_liquidity_or_volume_returns_zero(self):
        self.assertEqual(calculate_score({"liquidity": {"usd": None}, "volume": {"h24": 1000}}), 0)
        self.assertEqual(calculate_score({"liquidity": {"usd": 1000}, "volume": {"h24": None}}), 0)

    def test_score_is_capped_at_100(self):
        pair = make_pair(
            liquidity=1_000_000,
            volume_h24=1_000_000,
            change_h24=150,
            buys=100,
            sells=1,
            pair_created_at=time.time() * 1000,  # brand new pair
        )
        self.assertLessEqual(calculate_score(pair), 100)

    def test_early_stage_bonus_applies(self):
        fresh = make_pair(pair_created_at=time.time() * 1000)
        old = make_pair(pair_created_at=time.time() * 1000 - 10 * 60 * 60 * 1000)
        self.assertGreater(calculate_score(fresh), calculate_score(old))

    def test_explicit_null_fields_do_not_raise(self):
        pair = {
            "liquidity": {"usd": 6000},
            "volume": {"h24": 30000},
            "priceChange": None,
            "txns": None,
            "pairCreatedAt": None,
        }
        # Should not raise, and should still produce a sensible score.
        self.assertGreaterEqual(calculate_score(pair), 0)

    def test_non_dict_pair_does_not_raise(self):
        self.assertEqual(calculate_score(None), 0)


if __name__ == "__main__":
    unittest.main()
