import unittest
from unittest import mock

from src.stocks import alpaca_client


def _resp(status_code=200, json_body=None, content=b"{}"):
    m = mock.Mock()
    m.status_code = status_code
    m.content = content
    m.json.return_value = json_body if json_body is not None else {}
    m.text = str(json_body)
    m.raise_for_status = mock.Mock()
    return m


class TestIsConfigured(unittest.TestCase):
    def test_false_when_no_credentials(self):
        with mock.patch.object(alpaca_client, "ALPACA_API_KEY", ""), mock.patch.object(alpaca_client, "ALPACA_API_SECRET", ""):
            self.assertFalse(alpaca_client.is_configured())

    def test_false_when_explicitly_disabled_even_with_credentials(self):
        with mock.patch.object(alpaca_client, "ALPACA_API_KEY", "k"), mock.patch.object(alpaca_client, "ALPACA_API_SECRET", "s"), mock.patch.object(alpaca_client, "ALPACA_ENABLED", False):
            self.assertFalse(alpaca_client.is_configured())

    def test_true_when_configured_and_enabled(self):
        with mock.patch.object(alpaca_client, "ALPACA_API_KEY", "k"), mock.patch.object(alpaca_client, "ALPACA_API_SECRET", "s"), mock.patch.object(alpaca_client, "ALPACA_ENABLED", True):
            self.assertTrue(alpaca_client.is_configured())


class TestUnconfiguredShortCircuits(unittest.TestCase):
    """Every public function must return its safe default without
    touching the network at all when unconfigured -- this is what lets
    the whole stocks pipeline run with zero Alpaca setup.
    """

    def setUp(self):
        patcher = mock.patch.object(alpaca_client, "is_configured", return_value=False)
        self.mock_is_configured = patcher.start()
        self.addCleanup(patcher.stop)
        self.request_patcher = mock.patch("requests.request")
        self.mock_request = self.request_patcher.start()
        self.addCleanup(self.request_patcher.stop)

    def test_get_account_returns_none(self):
        self.assertIsNone(alpaca_client.get_account())
        self.mock_request.assert_not_called()

    def test_list_positions_returns_empty_list(self):
        self.assertEqual(alpaca_client.list_positions(), [])
        self.mock_request.assert_not_called()

    def test_submit_paper_order_returns_none(self):
        self.assertIsNone(alpaca_client.submit_paper_order("AAPL", 1, "buy"))
        self.mock_request.assert_not_called()

    def test_get_bars_returns_empty_list(self):
        self.assertEqual(alpaca_client.get_bars("AAPL"), [])
        self.mock_request.assert_not_called()

    def test_get_snapshot_returns_none(self):
        self.assertIsNone(alpaca_client.get_snapshot("AAPL"))
        self.mock_request.assert_not_called()


class TestRequestResilience(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(alpaca_client, "is_configured", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_429_is_retried_then_can_still_succeed(self):
        with mock.patch("requests.request", side_effect=[_resp(429), _resp(200, {"ok": True})]) as mock_request, \
             mock.patch("time.sleep"):
            result = alpaca_client.get_account()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_request.call_count, 2)

    def test_401_is_not_retried(self):
        with mock.patch("requests.request", return_value=_resp(401)) as mock_request:
            result = alpaca_client.get_account()
        self.assertIsNone(result)
        self.assertEqual(mock_request.call_count, 1)

    def test_network_exception_never_raises_out_of_the_client(self):
        import requests as requests_module

        with mock.patch("requests.request", side_effect=requests_module.exceptions.ConnectionError("down")), \
             mock.patch("time.sleep"):
            result = alpaca_client.get_account()
        self.assertIsNone(result)

    def test_exhausted_retries_returns_none_not_a_raise(self):
        with mock.patch("requests.request", return_value=_resp(429)), mock.patch("time.sleep"):
            result = alpaca_client.list_positions()
        self.assertEqual(result, [])

    def test_submit_paper_order_hits_the_paper_base_url_only(self):
        with mock.patch("requests.request", return_value=_resp(200, {"id": "abc"})) as mock_request:
            alpaca_client.submit_paper_order("AAPL", 1, "buy")
        called_url = mock_request.call_args[0][1]
        self.assertIn("paper-api.alpaca.markets", called_url)
        self.assertNotIn("://api.alpaca.markets", called_url)


if __name__ == "__main__":
    unittest.main()
