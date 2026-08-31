"""Tests for the parts of src/wallet.py that don't require the optional
`solders` dependency or real network access -- the safety gates
themselves. Signing/sending is intentionally not implemented yet (see
that module's docstring) so there is nothing live to test here.
"""

import unittest
from unittest import mock

import requests

import src.wallet as wallet


class TestSeedPhraseDetection(unittest.TestCase):
    def test_detects_12_word_phrase(self):
        phrase = " ".join(["apple"] * 12)
        self.assertTrue(wallet._looks_like_seed_phrase(phrase))

    def test_detects_24_word_phrase(self):
        phrase = " ".join(["apple"] * 24)
        self.assertTrue(wallet._looks_like_seed_phrase(phrase))

    def test_base58_key_is_not_flagged(self):
        fake_base58_key = "5Jq6VbF6hFdCzQnW9k2Yx3eK8P" * 2  # not real, just key-shaped
        self.assertFalse(wallet._looks_like_seed_phrase(fake_base58_key))

    def test_wrong_word_count_is_not_flagged(self):
        phrase = " ".join(["apple"] * 13)
        self.assertFalse(wallet._looks_like_seed_phrase(phrase))


class TestLoadKeypairFromEnv(unittest.TestCase):
    def test_raises_when_not_configured(self):
        with mock.patch.object(wallet, "SOLANA_PRIVATE_KEY", ""):
            with self.assertRaises(wallet.WalletNotConfigured):
                wallet.load_keypair_from_env()

    def test_refuses_a_seed_phrase(self):
        phrase = " ".join(["apple"] * 12)
        with mock.patch.object(wallet, "SOLANA_PRIVATE_KEY", phrase):
            with self.assertRaises(wallet.WalletKeyLooksInvalid):
                wallet.load_keypair_from_env()


class TestBuildAndSendSwapIsHardDisabled(unittest.TestCase):
    def test_refuses_even_if_every_other_gate_would_pass(self):
        # Even with a "valid-looking" key and no dependency issues,
        # EXECUTION_ENABLED_IN_CODE (module constant, source-level gate)
        # must stop this before anything else is touched.
        self.assertFalse(wallet.EXECUTION_ENABLED_IN_CODE)
        with self.assertRaises(RuntimeError) as ctx:
            wallet.build_and_send_swap(quote_response={"outAmount": "1"})
        self.assertIn("EXECUTION_ENABLED_IN_CODE", str(ctx.exception))


class TestConnectionTest(unittest.TestCase):
    def test_degrades_gracefully_when_rpc_unreachable(self):
        with mock.patch("src.wallet.requests.post", side_effect=requests.exceptions.ConnectionError("no network")):
            result = wallet.connection_test()
        self.assertFalse(result["rpc_reachable"])
        self.assertIn("error", result)

    def test_reports_balance_when_public_key_configured_and_rpc_ok(self):
        health_response = mock.Mock()
        health_response.raise_for_status.return_value = None
        health_response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "ok"}

        balance_response = mock.Mock()
        balance_response.raise_for_status.return_value = None
        balance_response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"value": 1_500_000_000}}

        with mock.patch.object(wallet, "SOLANA_WALLET_PUBLIC_KEY", "SomePublicKey111"), mock.patch(
            "src.wallet.requests.post", side_effect=[health_response, balance_response]
        ):
            result = wallet.connection_test()

        self.assertTrue(result["rpc_reachable"])
        self.assertAlmostEqual(result["balance_sol"], 1.5)


if __name__ == "__main__":
    unittest.main()
