import unittest
from unittest import mock

import pandas as pd

from src.stocks.data_provider import AlpacaProvider, YFinanceProvider, _AutoProvider, get_provider


def _bars_df(n=5, start_price=100.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": [start_price] * n, "high": [start_price + 1] * n, "low": [start_price - 1] * n,
         "close": [start_price + i for i in range(n)], "volume": [1_000_000] * n},
        index=idx,
    )


class TestYFinanceProviderResilience(unittest.TestCase):
    def test_download_exception_returns_empty_frames_not_a_raise(self):
        provider = YFinanceProvider()
        with mock.patch("yfinance.download", side_effect=RuntimeError("network down")):
            result = provider.get_daily_bars_batch(["AAPL", "MSFT"])
        self.assertEqual(set(result.keys()), {"AAPL", "MSFT"})
        for df in result.values():
            self.assertTrue(df.empty)

    def test_get_daily_bars_single_symbol_empty_download(self):
        provider = YFinanceProvider()
        with mock.patch("yfinance.download", return_value=pd.DataFrame()):
            df = provider.get_daily_bars("AAPL")
        self.assertTrue(df.empty)

    def test_get_latest_price_none_on_empty_data(self):
        provider = YFinanceProvider()
        with mock.patch.object(provider, "get_daily_bars", return_value=pd.DataFrame()):
            self.assertIsNone(provider.get_latest_price("AAPL"))

    def test_intraday_download_exception_returns_empty_frame(self):
        provider = YFinanceProvider()
        with mock.patch("yfinance.download", side_effect=RuntimeError("boom")):
            df = provider.get_intraday_bars("AAPL")
        self.assertTrue(df.empty)

    def test_normalize_single_missing_columns_returns_empty(self):
        bad = pd.DataFrame({"weird": [1, 2, 3]})
        result = YFinanceProvider._normalize_single(bad)
        self.assertTrue(result.empty)

    def test_split_batch_missing_symbol_gets_empty_frame(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        cols = pd.MultiIndex.from_product([["AAPL"], ["open", "high", "low", "close", "volume"]])
        raw = pd.DataFrame([[1, 2, 0.5, 1.5, 1000]] * 3, index=idx, columns=cols)
        out = YFinanceProvider._split_batch(raw, ["AAPL", "MISSING"])
        self.assertFalse(out["AAPL"].empty)
        self.assertTrue(out["MISSING"].empty)


class TestAlpacaProviderResilience(unittest.TestCase):
    def test_empty_bars_payload_returns_empty_frame(self):
        provider = AlpacaProvider()
        with mock.patch("src.stocks.data_provider.alpaca_client.get_bars", return_value=[]):
            df = provider.get_daily_bars("AAPL")
        self.assertTrue(df.empty)

    def test_get_latest_price_none_when_no_snapshot(self):
        provider = AlpacaProvider()
        with mock.patch("src.stocks.data_provider.alpaca_client.get_snapshot", return_value=None):
            self.assertIsNone(provider.get_latest_price("AAPL"))

    def test_normalize_handles_malformed_bar_dicts_without_raising(self):
        result = AlpacaProvider._normalize([{"weird": "shape"}])
        self.assertTrue(result.empty)


class TestAutoProviderFallback(unittest.TestCase):
    def test_falls_back_to_yfinance_when_alpaca_not_configured(self):
        provider = _AutoProvider()
        with mock.patch("src.stocks.data_provider.alpaca_client.is_configured", return_value=False), \
             mock.patch.object(provider._yfinance, "get_daily_bars", return_value=_bars_df()) as mock_yf, \
             mock.patch.object(provider._alpaca, "get_daily_bars") as mock_alpaca:
            df = provider.get_daily_bars("AAPL")
        mock_yf.assert_called_once()
        mock_alpaca.assert_not_called()
        self.assertFalse(df.empty)

    def test_falls_back_to_yfinance_when_alpaca_returns_empty(self):
        provider = _AutoProvider()
        with mock.patch("src.stocks.data_provider.alpaca_client.is_configured", return_value=True), \
             mock.patch.object(provider._alpaca, "get_daily_bars", return_value=pd.DataFrame()), \
             mock.patch.object(provider._yfinance, "get_daily_bars", return_value=_bars_df()) as mock_yf:
            df = provider.get_daily_bars("AAPL")
        mock_yf.assert_called_once()
        self.assertFalse(df.empty)

    def test_does_not_fall_back_when_alpaca_succeeds(self):
        provider = _AutoProvider()
        with mock.patch("src.stocks.data_provider.alpaca_client.is_configured", return_value=True), \
             mock.patch.object(provider._alpaca, "get_daily_bars", return_value=_bars_df()), \
             mock.patch.object(provider._yfinance, "get_daily_bars") as mock_yf:
            provider.get_daily_bars("AAPL")
        mock_yf.assert_not_called()


class TestGetProvider(unittest.TestCase):
    def test_respects_explicit_provider_choice(self):
        with mock.patch("src.stocks.data_provider.STOCKS_DATA_PROVIDER", "yfinance"):
            self.assertIsInstance(get_provider(), YFinanceProvider)
        with mock.patch("src.stocks.data_provider.STOCKS_DATA_PROVIDER", "alpaca"):
            self.assertIsInstance(get_provider(), AlpacaProvider)

    def test_auto_is_the_default(self):
        with mock.patch("src.stocks.data_provider.STOCKS_DATA_PROVIDER", "auto"):
            self.assertIsInstance(get_provider(), _AutoProvider)


if __name__ == "__main__":
    unittest.main()
