"""Tests for src.stocks.live_broker. EVERY test here mocks
requests.get/post/delete -- none ever touches a real network socket, let
alone Alpaca's real live endpoint. Most tests deliberately flip
STOCKS_EXECUTION_ENABLED_IN_CODE to True (via mock.patch.object, scoped
to the test only) purely to exercise the request-building/response-
handling code paths in isolation from Layer 1 -- see
TestLayer1GateAlwaysCheckedFirst for proof the real, unpatched default
(False) blocks every single one of these functions on its own.
"""

import unittest
from unittest import mock

import requests

import src.stocks.live_broker as live_broker


def _resp(status_code=200, json_data=None, text=""):
    m = mock.Mock()
    m.status_code = status_code
    m.text = text
    m.content = b"x" if json_data is not None else b""
    m.json.return_value = json_data if json_data is not None else {}
    m.raise_for_status = mock.Mock()
    if status_code >= 400:
        m.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code}")
    return m


class TestLayer1GateAlwaysCheckedFirst(unittest.TestCase):
    """No patching of STOCKS_EXECUTION_ENABLED_IN_CODE here -- this is
    the real, shipped default (False). Every function that could ever
    reach the network must refuse before doing so.
    """

    def test_get_live_account_raises_when_gate_closed(self):
        with self.assertRaises(live_broker.LiveTradingDisabled):
            live_broker.get_live_account()

    def test_list_live_open_orders_raises_when_gate_closed(self):
        with self.assertRaises(live_broker.LiveTradingDisabled):
            live_broker.list_live_open_orders("AAPL")

    def test_submit_live_order_raises_when_gate_closed(self):
        with self.assertRaises(live_broker.LiveTradingDisabled):
            live_broker.submit_live_order("AAPL", 1, "buy", client_order_id="x")

    def test_cancel_all_live_orders_raises_when_gate_closed(self):
        with self.assertRaises(live_broker.LiveTradingDisabled):
            live_broker.cancel_all_live_orders()

    def test_gate_is_checked_before_network_is_ever_touched(self):
        with mock.patch("src.stocks.live_broker.requests.post") as mocked_post:
            with self.assertRaises(live_broker.LiveTradingDisabled):
                live_broker.submit_live_order("AAPL", 1, "buy", client_order_id="x")
        mocked_post.assert_not_called()

    def test_real_shipped_default_is_false(self):
        self.assertFalse(live_broker.STOCKS_EXECUTION_ENABLED_IN_CODE)


class _GateOpenTestCase(unittest.TestCase):
    """Base for tests that need Layer 1 open + live credentials present
    to exercise deeper logic -- scoped to each test only.
    """

    def setUp(self):
        self._patches = [
            mock.patch.object(live_broker, "STOCKS_EXECUTION_ENABLED_IN_CODE", True),
            mock.patch.object(live_broker, "ALPACA_LIVE_API_KEY", "test-key"),
            mock.patch.object(live_broker, "ALPACA_LIVE_API_SECRET", "test-secret"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class TestIsLiveConfigured(_GateOpenTestCase):
    def test_true_when_both_keys_set(self):
        self.assertTrue(live_broker.is_live_configured())

    def test_false_when_key_missing(self):
        with mock.patch.object(live_broker, "ALPACA_LIVE_API_KEY", ""):
            self.assertFalse(live_broker.is_live_configured())

    def test_not_configured_raises_before_any_request(self):
        with mock.patch.object(live_broker, "ALPACA_LIVE_API_KEY", ""), \
             mock.patch("src.stocks.live_broker.requests.get") as mocked_get:
            with self.assertRaises(live_broker.LiveNotConfigured):
                live_broker.get_live_account()
        mocked_get.assert_not_called()


class TestGetLiveAccount(_GateOpenTestCase):
    def test_returns_parsed_account_on_success(self):
        with mock.patch("src.stocks.live_broker.requests.get", return_value=_resp(200, {"equity": "200.00", "buying_power": "150.00"})):
            account = live_broker.get_live_account()
        self.assertEqual(account["buying_power"], "150.00")

    def test_returns_none_on_auth_failure(self):
        with mock.patch("src.stocks.live_broker.requests.get", return_value=_resp(401)):
            self.assertIsNone(live_broker.get_live_account())

    def test_returns_none_on_network_failure_after_retries(self):
        with mock.patch("src.stocks.live_broker.requests.get", side_effect=requests.exceptions.ConnectionError("boom")), \
             mock.patch("time.sleep"):
            self.assertIsNone(live_broker.get_live_account())


class TestListLiveOpenOrders(_GateOpenTestCase):
    def test_returns_list_on_success(self):
        with mock.patch("src.stocks.live_broker.requests.get", return_value=_resp(200, [{"id": "o1"}])):
            orders = live_broker.list_live_open_orders("AAPL")
        self.assertEqual(orders, [{"id": "o1"}])

    def test_returns_empty_list_when_response_is_not_a_list(self):
        with mock.patch("src.stocks.live_broker.requests.get", return_value=_resp(200, {"unexpected": "shape"})):
            orders = live_broker.list_live_open_orders("AAPL")
        self.assertEqual(orders, [])

    def test_returns_empty_list_on_failure(self):
        with mock.patch("src.stocks.live_broker.requests.get", return_value=_resp(500)):
            self.assertEqual(live_broker.list_live_open_orders("AAPL"), [])


class TestSubmitLiveOrder(_GateOpenTestCase):
    def test_successful_submission_returns_order_dict(self):
        with mock.patch("src.stocks.live_broker.requests.post", return_value=_resp(200, {"id": "order-123", "status": "accepted"})):
            order = live_broker.submit_live_order("AAPL", 0.25, "buy", client_order_id="coid-1")
        self.assertEqual(order["id"], "order-123")

    def test_rejected_order_raises_live_order_rejected_and_is_not_ambiguous(self):
        with mock.patch("src.stocks.live_broker.requests.post", return_value=_resp(422, text="insufficient buying power")):
            with self.assertRaises(live_broker.LiveOrderRejected):
                live_broker.submit_live_order("AAPL", 0.25, "buy", client_order_id="coid-1")

    def test_network_failure_raises_ambiguous_not_rejected(self):
        with mock.patch("src.stocks.live_broker.requests.post", side_effect=requests.exceptions.ConnectionError("timed out")):
            with self.assertRaises(live_broker.LiveOrderAmbiguous):
                live_broker.submit_live_order("AAPL", 0.25, "buy", client_order_id="coid-1")

    def test_ambiguous_outcome_is_never_silently_retried(self):
        """A single failed attempt must result in exactly one POST --
        this module must never retry an order submission whose outcome
        is unknown.
        """
        with mock.patch("src.stocks.live_broker.requests.post", side_effect=requests.exceptions.ConnectionError("timed out")) as mocked_post:
            with self.assertRaises(live_broker.LiveOrderAmbiguous):
                live_broker.submit_live_order("AAPL", 0.25, "buy", client_order_id="coid-1")
        self.assertEqual(mocked_post.call_count, 1)

    def test_rejects_non_positive_qty(self):
        with self.assertRaises(ValueError):
            live_broker.submit_live_order("AAPL", 0, "buy", client_order_id="coid-1")

    def test_rejects_invalid_side(self):
        with self.assertRaises(ValueError):
            live_broker.submit_live_order("AAPL", 1, "hold", client_order_id="coid-1")

    def test_client_order_id_is_sent_in_the_request_body(self):
        with mock.patch("src.stocks.live_broker.requests.post", return_value=_resp(200, {"id": "order-1"})) as mocked_post:
            live_broker.submit_live_order("AAPL", 1, "buy", client_order_id="my-unique-id")
        sent_body = mocked_post.call_args.kwargs["json"]
        self.assertEqual(sent_body["client_order_id"], "my-unique-id")


class TestPollOrderFill(_GateOpenTestCase):
    def test_returns_filled_true_once_status_is_filled(self):
        with mock.patch("src.stocks.live_broker.get_live_order", return_value={"status": "filled", "filled_qty": "1", "filled_avg_price": "101.5"}):
            result = live_broker.poll_order_fill("order-1", timeout_seconds=5, poll_interval_seconds=0)
        self.assertTrue(result["filled"])
        self.assertEqual(result["filled_avg_price"], "101.5")

    def test_returns_filled_false_on_terminal_rejection(self):
        with mock.patch("src.stocks.live_broker.get_live_order", return_value={"status": "rejected"}):
            result = live_broker.poll_order_fill("order-1", timeout_seconds=5, poll_interval_seconds=0)
        self.assertFalse(result["filled"])
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["timed_out"])

    def test_times_out_when_status_never_becomes_terminal(self):
        with mock.patch("src.stocks.live_broker.get_live_order", return_value={"status": "pending_new"}), \
             mock.patch("time.sleep"), \
             mock.patch("time.monotonic", side_effect=[0, 0.1, 100]):
            result = live_broker.poll_order_fill("order-1", timeout_seconds=5, poll_interval_seconds=0)
        self.assertFalse(result["filled"])
        self.assertTrue(result["timed_out"])

    def test_a_none_read_does_not_crash_and_keeps_polling(self):
        with mock.patch("src.stocks.live_broker.get_live_order", side_effect=[None, {"status": "filled", "filled_qty": "1", "filled_avg_price": "10"}]), \
             mock.patch("time.sleep"):
            result = live_broker.poll_order_fill("order-1", timeout_seconds=5, poll_interval_seconds=0)
        self.assertTrue(result["filled"])


class TestCancelAllLiveOrders(_GateOpenTestCase):
    def test_returns_true_on_success(self):
        with mock.patch("src.stocks.live_broker.requests.delete", return_value=_resp(207)):
            self.assertTrue(live_broker.cancel_all_live_orders())

    def test_returns_false_on_failure_and_does_not_raise(self):
        with mock.patch("src.stocks.live_broker.requests.delete", side_effect=requests.exceptions.ConnectionError("boom")):
            self.assertFalse(live_broker.cancel_all_live_orders())


if __name__ == "__main__":
    unittest.main()
