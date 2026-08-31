import unittest
from unittest import mock

import requests

from src.dex_client import fetch_pairs, fetch_solana_token_addresses


def _mock_response(json_data, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
    return response


class TestFetchSolanaTokenAddresses(unittest.TestCase):
    def test_filters_to_solana_only(self):
        payload = [
            {"chainId": "solana", "tokenAddress": "sol-1"},
            {"chainId": "ethereum", "tokenAddress": "eth-1"},
            {"chainId": "solana", "tokenAddress": None},
        ]
        with mock.patch("src.dex_client.requests.get", return_value=_mock_response(payload)):
            addresses = fetch_solana_token_addresses()
        self.assertEqual(addresses, ["sol-1"])

    def test_empty_list_on_persistent_network_failure(self):
        with mock.patch(
            "src.dex_client.requests.get",
            side_effect=requests.exceptions.ConnectionError("no network"),
        ), mock.patch("src.dex_client.time.sleep"):
            addresses = fetch_solana_token_addresses()
        self.assertEqual(addresses, [])

    def test_empty_list_on_unexpected_shape(self):
        with mock.patch("src.dex_client.requests.get", return_value=_mock_response({"not": "a list"})):
            addresses = fetch_solana_token_addresses()
        self.assertEqual(addresses, [])


class TestFetchPairs(unittest.TestCase):
    def test_empty_addresses_returns_empty(self):
        self.assertEqual(fetch_pairs([]), [])

    def test_batches_requests_by_max_addresses(self):
        addresses = [f"addr-{i}" for i in range(35)]  # more than one batch of 30

        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            return _mock_response([{"pair": url}])

        with mock.patch("src.dex_client.requests.get", side_effect=fake_get):
            pairs = fetch_pairs(addresses)

        self.assertEqual(len(calls), 2)  # 30 + 5
        self.assertEqual(len(pairs), 2)

    def test_one_failed_batch_does_not_block_others(self):
        addresses = [f"addr-{i}" for i in range(35)]
        call_count = {"n": 0}

        def fake_get(url, timeout):
            call_count["n"] += 1
            # Exhaust every retry for the first batch only; later batches succeed.
            if call_count["n"] <= 3:
                raise requests.exceptions.Timeout("slow")
            return _mock_response([{"ok": True}])

        with mock.patch("src.dex_client.requests.get", side_effect=fake_get), mock.patch(
            "src.dex_client.time.sleep"
        ):
            pairs = fetch_pairs(addresses)

        self.assertEqual(len(pairs), 1)  # only the successful batch survives


if __name__ == "__main__":
    unittest.main()
