import unittest

from src.momentum import calculate_momentum


def make_pair(liquidity=10000, volume_h24=30000, change_h24=0, buys=60, sells=40):
    return {
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume_h24},
        "priceChange": {"h24": change_h24},
        "txns": {"h24": {"buys": buys, "sells": sells}},
    }


class TestCalculateMomentum(unittest.TestCase):
    def test_zero_liquidity_returns_zero(self):
        pair = make_pair(liquidity=0)
        self.assertEqual(calculate_momentum(pair), 0)

    def test_score_is_capped_at_75(self):
        pair = make_pair(liquidity=1000, volume_h24=50000, change_h24=150, buys=100, sells=1)
        self.assertLessEqual(calculate_momentum(pair), 75)

    def test_score_never_negative(self):
        pair = make_pair(liquidity=1000, volume_h24=100, change_h24=500, buys=1, sells=100)
        self.assertGreaterEqual(calculate_momentum(pair), 0)

    def test_missing_liquidity_key_does_not_raise(self):
        pair = {"volume": {"h24": 1000}}
        self.assertEqual(calculate_momentum(pair), 0)

    def test_explicit_null_liquidity_does_not_raise(self):
        # DexScreener can send {"liquidity": null} instead of omitting it.
        pair = {"liquidity": None, "volume": {"h24": 1000}}
        self.assertEqual(calculate_momentum(pair), 0)

    def test_non_dict_pair_does_not_raise(self):
        self.assertEqual(calculate_momentum(None), 0)
        self.assertEqual(calculate_momentum("not a pair"), 0)


if __name__ == "__main__":
    unittest.main()
