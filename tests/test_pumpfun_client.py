import unittest
from unittest import mock

import requests

from src.pumpfun_client import fetch_latest_launch_addresses, is_configured


def _mock_response(json_data, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
    return response


class TestIsConfigured(unittest.TestCase):
    def test_false_with_no_key(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", ""):
            self.assertFalse(is_configured())

    def test_true_with_a_key_and_enabled(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.PUMPFUN_ENABLED", True):
            self.assertTrue(is_configured())

    def test_false_when_explicitly_disabled_even_with_a_key(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.PUMPFUN_ENABLED", False):
            self.assertFalse(is_configured())


class TestFetchLatestLaunchAddresses(unittest.TestCase):
    def test_returns_empty_list_when_not_configured_and_makes_no_request(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", ""), \
             mock.patch("src.pumpfun_client.requests.get") as mocked_get:
            addresses = fetch_latest_launch_addresses()
        self.assertEqual(addresses, [])
        mocked_get.assert_not_called()

    def test_extracts_mint_addresses_from_a_bare_list_response(self):
        payload = [
            {"mint": "mint-1", "symbol": "AAA"},
            {"mint": "mint-2", "symbol": "BBB"},
            {"symbol": "no mint field"},
        ]
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.requests.get", return_value=_mock_response(payload)):
            addresses = fetch_latest_launch_addresses()
        self.assertEqual(addresses, ["mint-1", "mint-2"])

    def test_extracts_mint_addresses_from_a_wrapped_dict_response(self):
        payload = {"data": [{"mint": "mint-1"}, {"mint": "mint-2"}]}
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.requests.get", return_value=_mock_response(payload)):
            addresses = fetch_latest_launch_addresses()
        self.assertEqual(addresses, ["mint-1", "mint-2"])

    def test_empty_list_on_unexpected_shape(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.requests.get", return_value=_mock_response({"unexpected": "shape"})):
            addresses = fetch_latest_launch_addresses()
        self.assertEqual(addresses, [])

    def test_empty_list_on_persistent_network_failure(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.requests.get", side_effect=requests.exceptions.ConnectionError("down")), \
             mock.patch("src.pumpfun_client.time.sleep"):
            addresses = fetch_latest_launch_addresses()
        self.assertEqual(addresses, [])

    def test_api_key_is_sent_as_header_not_query_param(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "secret-key"), \
             mock.patch("src.pumpfun_client.requests.get", return_value=_mock_response([])) as mocked_get:
            fetch_latest_launch_addresses()
        _, kwargs = mocked_get.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "secret-key")
        self.assertNotIn("secret-key", str(kwargs.get("params", {})))

    def test_market_filter_and_sort_are_sent_as_params(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.requests.get", return_value=_mock_response([])) as mocked_get:
            fetch_latest_launch_addresses(limit=10)
        _, kwargs = mocked_get.call_args
        self.assertEqual(kwargs["params"]["market"], "pumpfun")
        self.assertEqual(kwargs["params"]["sortBy"], "createdAt")
        self.assertEqual(kwargs["params"]["limit"], 10)

    def test_a_non_429_4xx_status_does_not_retry(self):
        with mock.patch("src.pumpfun_client.PUMPFUN_API_KEY", "test-key"), \
             mock.patch("src.pumpfun_client.requests.get", return_value=_mock_response({}, status_code=401)) as mocked_get:
            addresses = fetch_latest_launch_addresses()
        self.assertEqual(addresses, [])
        self.assertEqual(mocked_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
