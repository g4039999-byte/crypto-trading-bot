import unittest
from unittest import mock

import src.risk as risk


def make_evaluated_pair(liquidity=20000, volume=60000, age=10, buys=100, sells=50):
    return {
        "symbol": "TEST",
        "liquidity": liquidity,
        "volume": volume,
        "age": age,
        "buys": buys,
        "sells": sells,
    }


SELLABLE = {"sellable": True, "reason": None, "round_trip_loss_pct": 2.0}


class TestAssessTokenSafety(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(risk, "MIN_LIVE_LIQUIDITY_USD", 15000),
            mock.patch.object(risk, "MIN_LIVE_VOLUME_24H_USD", 50000),
            mock.patch.object(risk, "MIN_LIVE_PAIR_AGE_MINUTES", 5),
            mock.patch.object(risk, "MAX_LIVE_PAIR_AGE_MINUTES", 180),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_passes_with_good_data_and_sellable_token(self):
        result = risk.assess_token_safety(make_evaluated_pair(), SELLABLE)
        self.assertTrue(result.passed)
        self.assertEqual(result.reasons, [])

    def test_fails_on_low_liquidity(self):
        result = risk.assess_token_safety(make_evaluated_pair(liquidity=1000), SELLABLE)
        self.assertFalse(result.passed)
        self.assertTrue(any("liquidity" in r for r in result.reasons))

    def test_fails_on_low_volume(self):
        result = risk.assess_token_safety(make_evaluated_pair(volume=100), SELLABLE)
        self.assertFalse(result.passed)

    def test_fails_when_too_young(self):
        result = risk.assess_token_safety(make_evaluated_pair(age=1), SELLABLE)
        self.assertFalse(result.passed)
        self.assertTrue(any("rug-risk window" in r for r in result.reasons))

    def test_fails_when_too_old(self):
        result = risk.assess_token_safety(make_evaluated_pair(age=500), SELLABLE)
        self.assertFalse(result.passed)

    def test_fails_without_a_round_trip_check(self):
        result = risk.assess_token_safety(make_evaluated_pair(), None)
        self.assertFalse(result.passed)
        self.assertTrue(any("not checked" in r for r in result.reasons))

    def test_fails_when_not_sellable(self):
        not_sellable = {"sellable": False, "reason": "no sell route -- possible honeypot", "round_trip_loss_pct": None}
        result = risk.assess_token_safety(make_evaluated_pair(), not_sellable)
        self.assertFalse(result.passed)
        self.assertTrue(any("honeypot" in r for r in result.reasons))

    def test_fails_when_round_trip_loss_too_high(self):
        lossy = {"sellable": True, "reason": "round-trip loss 35.0% exceeds the 20.0% limit", "round_trip_loss_pct": 35.0}
        result = risk.assess_token_safety(make_evaluated_pair(), lossy)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
