import unittest
from unittest import mock

import requests

from src.jupiter_client import get_quote, round_trip_check


def _mock_response(json_data, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400 and status_code != 404:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
    return response


class TestGetQuote(unittest.TestCase):
    def test_returns_none_on_404_no_route(self):
        with mock.patch("src.jupiter_client.requests.get", return_value=_mock_response({}, status_code=404)):
            self.assertIsNone(get_quote("A", "B", 1000, 300))

    def test_returns_none_after_persistent_failure(self):
        with mock.patch(
            "src.jupiter_client.requests.get", side_effect=requests.exceptions.ConnectionError("no network")
        ), mock.patch("src.jupiter_client.time.sleep"):
            self.assertIsNone(get_quote("A", "B", 1000, 300))

    def test_returns_parsed_quote_on_success(self):
        with mock.patch("src.jupiter_client.requests.get", return_value=_mock_response({"outAmount": "123"})):
            quote = get_quote("A", "B", 1000, 300)
        self.assertEqual(quote["outAmount"], "123")


class TestRoundTripCheck(unittest.TestCase):
    def test_not_sellable_when_no_buy_route(self):
        with mock.patch("src.jupiter_client.get_quote", return_value=None):
            result = round_trip_check("token-mint", 10_000_000, 300)
        self.assertFalse(result["sellable"])
        self.assertIn("buy route", result["reason"])

    def test_not_sellable_when_no_sell_route(self):
        buy_quote = {"outAmount": "1000000"}

        def fake_get_quote(input_mint, output_mint, amount, slippage_bps):
            if output_mint == "token-mint":
                return buy_quote
            return None  # no route back to SOL -- classic honeypot signature

        with mock.patch("src.jupiter_client.get_quote", side_effect=fake_get_quote):
            result = round_trip_check("token-mint", 10_000_000, 300)

        self.assertFalse(result["sellable"])
        self.assertIn("honeypot", result["reason"])

    def test_sellable_with_acceptable_round_trip_loss(self):
        def fake_get_quote(input_mint, output_mint, amount, slippage_bps):
            if output_mint == "token-mint":
                return {"outAmount": "1000000"}
            return {"outAmount": str(int(10_000_000 * 0.97))}  # 3% round-trip loss

        with mock.patch("src.jupiter_client.get_quote", side_effect=fake_get_quote), mock.patch(
            "src.jupiter_client.MAX_ROUND_TRIP_LOSS_PCT", 20.0
        ):
            result = round_trip_check("token-mint", 10_000_000, 300)

        self.assertTrue(result["sellable"])
        self.assertIsNone(result["reason"])
        self.assertAlmostEqual(result["round_trip_loss_pct"], 3.0, places=1)

    def test_flags_excessive_round_trip_loss(self):
        def fake_get_quote(input_mint, output_mint, amount, slippage_bps):
            if output_mint == "token-mint":
                return {"outAmount": "1000000"}
            return {"outAmount": str(int(10_000_000 * 0.5))}  # 50% round-trip loss

        with mock.patch("src.jupiter_client.get_quote", side_effect=fake_get_quote), mock.patch(
            "src.jupiter_client.MAX_ROUND_TRIP_LOSS_PCT", 20.0
        ):
            result = round_trip_check("token-mint", 10_000_000, 300)

        self.assertTrue(result["sellable"])  # a route exists...
        self.assertIsNotNone(result["reason"])  # ...but it's flagged as too costly


if __name__ == "__main__":
    unittest.main()
