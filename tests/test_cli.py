"""Full coverage of src/cli.py: every command's output against known
data, empty states, filters, missing-opportunity handling, argument
parsing, and its isolation from wallet/risk/execution.

All underlying state/log files (opportunity_watchlist.json,
news_signals.json, positions.json, paper_positions.json,
trade_log.jsonl, paper_trade_log.jsonl) are redirected to a temp
directory for every test in this file -- nothing here ever touches the
real project's data/ directory.
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.cli as cli
import src.news_signal_engine as news_signal_engine
import src.opportunity_watchlist as opportunity_watchlist
import src.paper_logger as paper_logger
import src.paper_portfolio as paper_portfolio
import src.portfolio as portfolio
import src.trade_logger as trade_logger


def _run(argv):
    """Run src.cli.main(argv) and return everything it printed."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli.main(argv)
    return buffer.getvalue()


def _make_result(address="addr-1", symbol="GOOD", score=70, base_score=70, momentum_score=70,
                  trend="NEUTRAL", stage="EARLY", ok=True):
    return {
        "address": address, "symbol": symbol, "score": score, "base_score": base_score,
        "momentum_score": momentum_score, "trend": trend, "stage": stage, "ok": ok,
    }


class IsolatedStateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        base = Path(self._tmp_dir.name)
        self._patches = [
            mock.patch.object(opportunity_watchlist, "STATE_FILE", base / "opportunity_watchlist.json"),
            mock.patch.object(news_signal_engine, "STATE_FILE", base / "news_signals.json"),
            mock.patch.object(paper_portfolio, "STATE_FILE", base / "paper_positions.json"),
            mock.patch.object(portfolio, "STATE_FILE", base / "positions.json"),
            mock.patch.object(paper_logger, "LOG_FILE", base / "paper_trade_log.jsonl"),
            mock.patch.object(trade_logger, "LOG_FILE", base / "trade_log.jsonl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp_dir.cleanup()


class TestWatchlistCommand(IsolatedStateTestCase):
    def test_empty_state_prints_a_friendly_message(self):
        output = _run(["watchlist"])
        self.assertIn("No opportunities tracked", output)

    def test_lists_a_tracked_opportunity(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=85, trend="STRONG")])
        output = _run(["watchlist"])
        self.assertIn("GOOD", output)
        self.assertIn("addr-1", output)

    def test_status_filter_only_shows_matching_entries(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=50, trend="NEUTRAL")])
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=50, trend="NEUTRAL")])
        opportunity_watchlist.update_from_results([_make_result("addr-2", "BAD", score=5, trend="WEAK", ok=False)])
        opportunity_watchlist.update_from_results([_make_result("addr-2", "BAD", score=5, trend="WEAK", ok=False)])
        output = _run(["watchlist", "--status", "REJECTED"])
        self.assertIn("BAD", output)
        self.assertNotIn("GOOD", output)

    def test_status_filter_is_case_insensitive(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=5, trend="WEAK", ok=False)])
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=5, trend="WEAK", ok=False)])
        output = _run(["watchlist", "--status", "rejected"])
        self.assertIn("GOOD", output)

    def test_empty_filtered_status_prints_a_friendly_message(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=50, trend="NEUTRAL")])
        output = _run(["watchlist", "--status", "QUALIFIED"])
        self.assertIn("No opportunities tracked", output)

    def test_invalid_status_value_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            _run(["watchlist", "--status", "NOT_A_REAL_STATUS"])


class TestOpportunityCommand(IsolatedStateTestCase):
    def test_unknown_address_prints_a_friendly_message_not_a_crash(self):
        output = _run(["opportunity", "addr-does-not-exist"])
        self.assertIn("No opportunity tracked", output)
        self.assertIn("addr-does-not-exist", output)

    def test_shows_the_full_detail_for_a_tracked_address(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=85, trend="STRONG", stage="EARLY")])
        output = _run(["opportunity", "addr-1"])
        self.assertIn("addr-1", output)
        self.assertIn("GOOD", output)
        self.assertIn("STRONG", output)
        self.assertIn("EARLY", output)

    def test_shows_momentum_signal_fields_from_the_latest_history_point(self):
        result = _make_result("addr-1", "GOOD")
        result["signals"] = {"buy_sell_pressure": 0.8, "volume_momentum": 0.5, "price_acceleration": 0.1, "persistence_streak": 3}
        opportunity_watchlist.update_from_results([result])
        output = _run(["opportunity", "addr-1"])
        self.assertIn("0.8", output)
        self.assertIn("0.5", output)

    def test_shows_no_news_message_when_there_is_none(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        output = _run(["opportunity", "addr-1"])
        self.assertIn("No active news signals", output)

    def test_shows_attached_news_when_present(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD")])
        with mock.patch.object(
            opportunity_watchlist, "group_signals_by_asset",
            return_value={"GOOD": [{"event_id": "e1", "event_type": "LISTING", "sentiment": "POSITIVE",
                                     "confidence": 0.7, "directional_bias": "BULLISH", "urgency": "HIGH"}]},
        ), mock.patch.object(opportunity_watchlist, "_active_news_signals", return_value=[]):
            opportunity_watchlist.attach_news_signals([_make_result("addr-1", "GOOD")])

        output = _run(["opportunity", "addr-1"])
        self.assertIn("LISTING", output)
        self.assertIn("BULLISH", output)

    def test_missing_address_argument_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            _run(["opportunity"])


class TestNewsCommand(IsolatedStateTestCase):
    def test_empty_state_prints_a_friendly_message(self):
        output = _run(["news"])
        self.assertIn("No active news signals", output)

    def test_lists_active_signals(self):
        from src.news_providers import MockNewsProvider, RawNewsEvent

        provider = MockNewsProvider(events=[
            RawNewsEvent(text="$SOL surges after listing", source="mock", event_id="e1"),
        ])
        news_signal_engine.ingest_events([provider])
        output = _run(["news"])
        self.assertIn("LISTING", output)
        self.assertIn("SOL", output)

    def test_symbol_filter_only_shows_matching_signals(self):
        from src.news_providers import MockNewsProvider, RawNewsEvent

        provider = MockNewsProvider(events=[
            RawNewsEvent(text="$SOL surges after listing", source="mock", event_id="e1"),
            RawNewsEvent(text="$ETH drops on regulatory news", source="mock", event_id="e2"),
        ])
        news_signal_engine.ingest_events([provider])
        output = _run(["news", "--symbol", "SOL"])
        self.assertIn("SOL", output)
        self.assertNotIn("ETH", output)

    def test_symbol_filter_with_no_match_prints_a_friendly_message(self):
        output = _run(["news", "--symbol", "NOPE"])
        self.assertIn("No active news signals", output)

    def test_expired_signals_are_excluded(self):
        from src.news_providers import MockNewsProvider, RawNewsEvent

        with mock.patch.object(news_signal_engine, "NEWS_SIGNAL_TTL_MINUTES", 10):
            provider = MockNewsProvider(events=[RawNewsEvent(text="$SOL surges", source="mock", event_id="e1")])
            old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
            news_signal_engine.ingest_events([provider], now=old_time)

        with mock.patch("src.news_signal_engine.datetime") as mock_dt:
            mock_dt.now.return_value = old_time + timedelta(minutes=30)
            mock_dt.fromisoformat = datetime.fromisoformat
            output = _run(["news"])
        self.assertIn("No active news signals", output)


class TestPerformanceCommand(IsolatedStateTestCase):
    def _write_paper_trade(self, pnl_usd=10.0, opened_at="2026-01-01T00:00:00+00:00", closed_at="2026-01-01T00:30:00+00:00"):
        paper_portfolio.save_state({
            "open_positions": [], "daily_pnl_usd": {},
            "closed_trades": [{
                "token_address": "addr-1", "symbol": "GOOD", "pnl_usd": pnl_usd,
                "opened_at": opened_at, "closed_at": closed_at, "entry_price_usd": 1.0,
                "exit_price_usd": 1.1, "amount_tokens": 100, "size_usd": 100, "reason": "take_profit",
            }],
        })

    def test_empty_state_prints_a_friendly_message(self):
        output = _run(["performance"])
        self.assertIn("No closed trades found", output)

    def test_shows_aggregate_stats_for_paper_trades(self):
        self._write_paper_trade(pnl_usd=15.0)
        output = _run(["performance"])
        self.assertIn("Trades analyzed: 1", output)
        self.assertIn("Win rate", output)

    def test_mode_live_reads_from_the_live_portfolio_not_paper(self):
        self._write_paper_trade(pnl_usd=15.0)
        output = _run(["performance", "--mode", "live"])
        self.assertIn("No closed trades found", output)

    def test_top_n_is_passed_through(self):
        with mock.patch("src.cli.analyze_recent") as mock_analyze:
            mock_analyze.return_value = mock.Mock(total_trades=0)
            _run(["performance", "--top-n", "5"])
        mock_analyze.assert_called_once_with(mode="paper", top_n_recent=5, since=None)

    def test_since_is_passed_through(self):
        with mock.patch("src.cli.analyze_recent") as mock_analyze:
            mock_analyze.return_value = mock.Mock(total_trades=0)
            _run(["performance", "--since", "2026-01-01"])
        mock_analyze.assert_called_once_with(mode="paper", top_n_recent=None, since="2026-01-01")

    def test_invalid_mode_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            _run(["performance", "--mode", "not-a-real-mode"])


class TestStatusCommand(IsolatedStateTestCase):
    def test_shows_execution_safety_flags(self):
        output = _run(["status"])
        self.assertIn("EXECUTION_ENABLED_IN_CODE", output)
        self.assertIn("False", output)
        self.assertIn("LIVE_TRADING", output)

    def test_shows_watchlist_counts_by_status(self):
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=50, trend="NEUTRAL")])
        opportunity_watchlist.update_from_results([_make_result("addr-1", "GOOD", score=50, trend="NEUTRAL")])
        output = _run(["status"])
        self.assertIn("WATCHING: 1", output)
        self.assertIn("Total tracked: 1", output)

    def test_shows_active_news_signal_count(self):
        from src.news_providers import MockNewsProvider, RawNewsEvent

        provider = MockNewsProvider(events=[RawNewsEvent(text="$SOL surges", source="mock", event_id="e1")])
        news_signal_engine.ingest_events([provider])
        output = _run(["status"])
        self.assertIn("Active signals: 1", output)

    def test_reflects_config_toggles(self):
        with mock.patch.object(cli, "NEWS_SIGNAL_WATCHLIST_LINK_ENABLED", False):
            output = _run(["status"])
        self.assertIn("News -> watchlist link:      disabled", output)


class TestExecutionEnabledFlagReading(unittest.TestCase):
    def test_reads_the_real_wallet_py_value(self):
        # Confirms the plain-text read actually finds the real flag in
        # the real wallet.py -- this must stay False.
        self.assertEqual(cli._read_execution_enabled_flag(), "False")

    def test_missing_file_returns_unknown_not_a_crash(self):
        with mock.patch.object(cli, "_WALLET_SOURCE_PATH", Path("/nonexistent/wallet.py")):
            self.assertIn("unknown", cli._read_execution_enabled_flag())

    def test_missing_line_returns_unknown_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_wallet = Path(tmp_dir) / "wallet.py"
            fake_wallet.write_text("# no such flag here\n", encoding="utf-8")
            with mock.patch.object(cli, "_WALLET_SOURCE_PATH", fake_wallet):
                self.assertIn("unknown", cli._read_execution_enabled_flag())


class TestArgumentParsing(unittest.TestCase):
    def test_no_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main([])

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main(["not-a-real-command"])

    def test_performance_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(["performance"])
        self.assertEqual(args.mode, "paper")
        self.assertIsNone(args.top_n)
        self.assertIsNone(args.since)

    def test_watchlist_default_has_no_status_filter(self):
        parser = cli._build_parser()
        args = parser.parse_args(["watchlist"])
        self.assertIsNone(args.status)


class TestIsolationFromExecutionAndWallet(unittest.TestCase):
    def test_cli_source_does_not_import_wallet_risk_or_trading_modules(self):
        import inspect

        forbidden_imports = (
            "import src.wallet", "from src.wallet", "from src import wallet",
            "import src.risk", "from src.risk", "from src import risk",
            "import src.live_trader", "from src.live_trader", "from src import live_trader",
            "import src.paper_trader", "from src.paper_trader", "from src import paper_trader",
            "import src.radar", "from src.radar", "from src import radar",
        )
        source = inspect.getsource(cli)
        for forbidden in forbidden_imports:
            self.assertNotIn(forbidden, source, f"{forbidden!r} must never appear in src/cli.py")

    def test_cli_never_calls_any_write_or_execution_function_by_name(self):
        # Belt-and-suspenders: confirm no obviously write/execution-shaped
        # function name appears anywhere in this module's source.
        import inspect

        forbidden_calls = (
            "save_state(", "ingest_events(", "ingest_raw_event(", "open_position(",
            "close_position(", "build_and_send_swap(", "run_live_cycle(", "run_paper_cycle(",
        )
        source = inspect.getsource(cli)
        for call in forbidden_calls:
            self.assertNotIn(call, source, f"{call} must never appear in src/cli.py")


if __name__ == "__main__":
    unittest.main()
