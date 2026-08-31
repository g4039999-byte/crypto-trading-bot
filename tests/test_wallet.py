"""Tests for src/wallet.py.

Most of this project's tests avoid needing the optional `solders`
dependency (genuinely not installed in the environment that wrote these
tests -- see requirements-live.txt) by testing the safety gates and each
HTTP/RPC step in isolation, and by injecting a small fake `solders`
package via sys.modules for the couple of tests that exercise the actual
signing step. This gives real coverage of the orchestration logic
(retries, error handling, what gets called in what order) without ever
claiming the on-chain byte-level behavior has been verified against a
real wallet -- it has not. Read build_and_send_swap()'s docstring before
ever using it for real.
"""

import sys
import types
import unittest
from unittest import mock

import requests

import src.wallet as wallet


class _FakeMessage:
    def __init__(self, raw):
        self._raw = raw

    def __bytes__(self):
        return self._raw


class _FakeVersionedTransaction:
    """Stands in for solders.transaction.VersionedTransaction: enough of
    its interface (from_bytes / populate / __bytes__) to exercise
    _sign_swap_transaction()'s orchestration, without claiming to
    replicate real Solana transaction serialization.
    """

    def __init__(self, message, signatures=None):
        self.message = message
        self.signatures = signatures or []

    @classmethod
    def from_bytes(cls, data):
        return cls(_FakeMessage(data))

    @classmethod
    def populate(cls, message, signatures):
        return cls(message, signatures)

    def __bytes__(self):
        return b"SIGNED:" + bytes(self.message)


def _fake_solders_modules():
    fake_solders = types.ModuleType("solders")
    fake_transaction = types.ModuleType("solders.transaction")
    fake_transaction.VersionedTransaction = _FakeVersionedTransaction
    fake_solders.transaction = fake_transaction
    return {"solders": fake_solders, "solders.transaction": fake_transaction}


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

    def test_the_gate_is_checked_before_any_network_call(self):
        # No requests.post should happen at all while the gate is closed.
        with mock.patch("src.wallet.requests.post") as mock_post:
            with self.assertRaises(RuntimeError):
                wallet.build_and_send_swap(quote_response={"outAmount": "1"})
        mock_post.assert_not_called()


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


class TestGetSplTokenBalanceRaw(unittest.TestCase):
    def test_sums_amounts_across_token_accounts(self):
        rpc_result = {
            "value": [
                {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "1000"}}}}}},
                {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "500"}}}}}},
            ]
        }
        with mock.patch("src.wallet._rpc_call", return_value=rpc_result):
            total = wallet.get_spl_token_balance_raw("Owner1", "Mint1")
        self.assertEqual(total, 1500)

    def test_returns_zero_when_no_token_account_exists(self):
        with mock.patch("src.wallet._rpc_call", return_value={"value": []}):
            total = wallet.get_spl_token_balance_raw("Owner1", "Mint1")
        self.assertEqual(total, 0)

    def test_ignores_malformed_entries_instead_of_crashing(self):
        rpc_result = {"value": [{"account": {}}, {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "42"}}}}}}]}
        with mock.patch("src.wallet._rpc_call", return_value=rpc_result):
            total = wallet.get_spl_token_balance_raw("Owner1", "Mint1")
        self.assertEqual(total, 42)


class TestRequestSwapTransaction(unittest.TestCase):
    def test_returns_swap_transaction_on_success(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"swapTransaction": "BASE64TX"}
        with mock.patch("src.wallet.requests.post", return_value=response):
            result = wallet._request_swap_transaction({"outAmount": "1"}, "PubKey111")
        self.assertEqual(result, "BASE64TX")

    def test_retries_then_succeeds(self):
        good = mock.Mock()
        good.raise_for_status.return_value = None
        good.json.return_value = {"swapTransaction": "BASE64TX"}
        with mock.patch(
            "src.wallet.requests.post",
            side_effect=[requests.exceptions.ConnectionError("blip"), good],
        ), mock.patch("src.wallet.time.sleep"):
            result = wallet._request_swap_transaction({"outAmount": "1"}, "PubKey111")
        self.assertEqual(result, "BASE64TX")

    def test_raises_after_exhausting_retries(self):
        with mock.patch(
            "src.wallet.requests.post", side_effect=requests.exceptions.ConnectionError("down")
        ), mock.patch("src.wallet.time.sleep"):
            with self.assertRaises(wallet.SwapExecutionError):
                wallet._request_swap_transaction({"outAmount": "1"}, "PubKey111")

    def test_raises_when_response_missing_swap_transaction(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"error": "no route"}
        with mock.patch("src.wallet.requests.post", return_value=response):
            with self.assertRaises(wallet.SwapExecutionError):
                wallet._request_swap_transaction({"outAmount": "1"}, "PubKey111")


class TestSignSwapTransaction(unittest.TestCase):
    def test_signs_locally_and_reencodes(self):
        import base64

        fake_modules = _fake_solders_modules()
        with mock.patch.dict(sys.modules, fake_modules):
            keypair = mock.Mock()
            keypair.sign_message.return_value = b"FAKESIG"
            raw_tx_bytes = b"unsigned-tx-bytes"
            swap_tx_b64 = base64.b64encode(raw_tx_bytes).decode("ascii")

            result_b64 = wallet._sign_swap_transaction(swap_tx_b64, keypair)

        decoded = base64.b64decode(result_b64)
        self.assertEqual(decoded, b"SIGNED:unsigned-tx-bytes")
        keypair.sign_message.assert_called_once_with(raw_tx_bytes)


class TestSendRawTransaction(unittest.TestCase):
    def test_returns_signature_on_success(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "SomeSignature111"}
        with mock.patch("src.wallet.requests.post", return_value=response):
            result = wallet._send_raw_transaction("SIGNEDTXB64")
        self.assertEqual(result, "SomeSignature111")

    def test_raises_on_rpc_error(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "blockhash not found"},
        }
        with mock.patch("src.wallet.requests.post", return_value=response):
            with self.assertRaises(wallet.SwapExecutionError):
                wallet._send_raw_transaction("SIGNEDTXB64")


class TestPollConfirmation(unittest.TestCase):
    def test_confirmed_quickly(self):
        rpc_result = {"value": [{"err": None, "confirmationStatus": "confirmed"}]}
        with mock.patch("src.wallet._rpc_call", return_value=rpc_result):
            result = wallet._poll_confirmation("Sig111", timeout_seconds=5, poll_interval_seconds=0)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["confirmation_status"], "confirmed")

    def test_on_chain_error_is_reported_as_not_confirmed(self):
        rpc_result = {"value": [{"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": None}]}
        with mock.patch("src.wallet._rpc_call", return_value=rpc_result):
            result = wallet._poll_confirmation("Sig111", timeout_seconds=5, poll_interval_seconds=0)
        self.assertFalse(result["confirmed"])
        self.assertIn("on_chain_error", result)

    def test_times_out_without_crashing(self):
        # First monotonic() call sets the deadline, second is the loop's
        # while-check -- forcing it past the deadline exits the loop
        # after zero iterations, deterministically, with no real sleep.
        with mock.patch("src.wallet.time.monotonic", side_effect=[0, 100]), mock.patch(
            "src.wallet._rpc_call", return_value={"value": [None]}
        ):
            result = wallet._poll_confirmation("Sig111", timeout_seconds=5, poll_interval_seconds=0)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result.get("timed_out"))


class TestBuildAndSendSwapFullPipeline(unittest.TestCase):
    def test_calls_every_step_in_order_when_execution_enabled(self):
        fake_modules = _fake_solders_modules()
        fake_keypair = mock.Mock()
        fake_keypair.pubkey.return_value = "PubKey111"

        with mock.patch.dict(sys.modules, fake_modules), mock.patch.object(
            wallet, "EXECUTION_ENABLED_IN_CODE", True
        ), mock.patch.object(wallet, "load_keypair_from_env", return_value=fake_keypair), mock.patch.object(
            wallet, "_request_swap_transaction", return_value="UNSIGNEDB64"
        ) as mock_request, mock.patch.object(
            wallet, "_sign_swap_transaction", return_value="SIGNEDB64"
        ) as mock_sign, mock.patch.object(
            wallet, "_send_raw_transaction", return_value="Sig111"
        ) as mock_send, mock.patch.object(
            wallet,
            "_poll_confirmation",
            return_value={"confirmed": True, "signature": "Sig111", "confirmation_status": "confirmed"},
        ) as mock_poll:
            result = wallet.build_and_send_swap({"outAmount": "1"})

        mock_request.assert_called_once_with({"outAmount": "1"}, "PubKey111", None)
        mock_sign.assert_called_once_with("UNSIGNEDB64", fake_keypair)
        mock_send.assert_called_once_with("SIGNEDB64")
        mock_poll.assert_called_once_with("Sig111")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["signature"], "Sig111")

    def test_raises_dependency_missing_when_solders_is_not_installed(self):
        # In the environment these tests run in, solders genuinely is not
        # installed -- this exercises the real ImportError path.
        self.assertNotIn("solders", sys.modules)
        with mock.patch.object(wallet, "EXECUTION_ENABLED_IN_CODE", True):
            with self.assertRaises(wallet.WalletDependencyMissing):
                wallet.build_and_send_swap({"outAmount": "1"})

    def test_nothing_is_sent_if_building_the_transaction_fails(self):
        fake_modules = _fake_solders_modules()
        fake_keypair = mock.Mock()
        fake_keypair.pubkey.return_value = "PubKey111"

        with mock.patch.dict(sys.modules, fake_modules), mock.patch.object(
            wallet, "EXECUTION_ENABLED_IN_CODE", True
        ), mock.patch.object(wallet, "load_keypair_from_env", return_value=fake_keypair), mock.patch.object(
            wallet, "_request_swap_transaction", side_effect=wallet.SwapExecutionError("no route")
        ), mock.patch.object(wallet, "_send_raw_transaction") as mock_send:
            with self.assertRaises(wallet.SwapExecutionError):
                wallet.build_and_send_swap({"outAmount": "1"})

        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
