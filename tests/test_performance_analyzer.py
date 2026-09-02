"""Full coverage of src/performance_analyzer.py: every statistic's
formula, every breakdown, empty/missing/corrupt data, best-effort entry-
context correlation, and its isolation from wallet/risk/execution.

All four underlying files (data/positions.json, data/paper_positions.json,
data/trade_log.jsonl, data/paper_trade_log.jsonl) are redirected to a
temp directory for every test in this file -- nothing here ever touches
the real project's data/ directory.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.paper_logger as paper_logger
import src.paper_portfolio as paper_portfolio
import src.performance_analyzer as pa
import src.portfolio as portfolio
import src.trade_logger as trade_logger


def _trade(pnl_usd=10.0, opened_at="2026-01-01T00:00:00+00:00", closed_at="2026-01-01T00:30:00+00:00",
           token_address="addr-1", symbol="AAA", mode="PAPER", **extra):
    trade = {
        "token_address": token_address, "symbol": symbol, "pnl_usd": pnl_usd,
        "opened_at": opened_at, "closed_at": closed_at, "entry_price_usd": 1.0,
        "exit_price_usd": 1.0, "amount_tokens": 100, "size_usd": 100, "reason": "take_profit",
        "mode": mode,
    }
    trade.update(extra)
    return trade


class IsolatedFilesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        base = Path(self._tmp_dir.name)
        self._patches = [
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

    def _write_paper_closed_trades(self, trades):
        paper_portfolio.save_state({"open_positions": [], "daily_pnl_usd": {}, "closed_trades": trades})

    def _write_live_closed_trades(self, trades):
        portfolio.save_state({"open_positions": [], "daily_pnl_usd": {}, "closed_trades": trades})

    def _write_paper_log_lines(self, entries):
        paper_logger.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with paper_logger.LOG_FILE.open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")


class TestLoadClosedTrades(IsolatedFilesTestCase):
    def test_no_files_yields_empty_list(self):
        self.assertEqual(pa.load_closed_trades("paper"), [])
        self.assertEqual(pa.load_closed_trades("live"), [])
        self.assertEqual(pa.load_closed_trades("both"), [])

    def test_paper_mode_only_reads_paper_trades(self):
        self._write_paper_closed_trades([_trade(token_address="p1")])
        self._write_live_closed_trades([_trade(token_address="l1")])
        trades = pa.load_closed_trades("paper")
        self.assertEqual([t["token_address"] for t in trades], ["p1"])
        self.assertEqual(trades[0]["mode"], "PAPER")

    def test_live_mode_only_reads_live_trades(self):
        self._write_paper_closed_trades([_trade(token_address="p1")])
        self._write_live_closed_trades([_trade(token_address="l1")])
        trades = pa.load_closed_trades("live")
        self.assertEqual([t["token_address"] for t in trades], ["l1"])
        self.assertEqual(trades[0]["mode"], "LIVE")

    def test_both_mode_reads_and_tags_both(self):
        self._write_paper_closed_trades([_trade(token_address="p1")])
        self._write_live_closed_trades([_trade(token_address="l1")])
        trades = pa.load_closed_trades("both")
        modes = {t["token_address"]: t["mode"] for t in trades}
        self.assertEqual(modes, {"p1": "PAPER", "l1": "LIVE"})

    def test_corrupt_state_file_yields_empty_not_a_crash(self):
        paper_portfolio.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        paper_portfolio.STATE_FILE.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(pa.load_closed_trades("paper"), [])

    def test_non_dict_entries_in_closed_trades_are_skipped(self):
        paper_portfolio.save_state({"open_positions": [], "daily_pnl_usd": {}, "closed_trades": ["not-a-dict", None, _trade()]})
        trades = pa.load_closed_trades("paper")
        self.assertEqual(len(trades), 1)


class TestBuyLogCorrelation(IsolatedFilesTestCase):
    def test_no_log_file_gives_no_context(self):
        result = pa._entry_context_for(_trade(token_address="addr-1"), {})
        self.assertEqual(result, {})

    def test_finds_the_closest_buy_entry_by_timestamp(self):
        buy_entries = {
            "addr-1": [
                {"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00", "score": 60},
                {"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T05:00:00+00:00", "score": 90},
            ]
        }
        trade = _trade(token_address="addr-1", opened_at="2026-01-01T04:58:00+00:00")
        context = pa._entry_context_for(trade, buy_entries)
        self.assertEqual(context["score"], 90)

    def test_no_matching_address_gives_no_context(self):
        buy_entries = {"addr-other": [{"action": "BUY", "token_address": "addr-other", "timestamp": "2026-01-01T00:00:00+00:00"}]}
        context = pa._entry_context_for(_trade(token_address="addr-1"), buy_entries)
        self.assertEqual(context, {})

    def test_missing_opened_at_falls_back_to_most_recent_buy(self):
        buy_entries = {
            "addr-1": [
                {"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00", "score": 60},
                {"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T05:00:00+00:00", "score": 90},
            ]
        }
        trade = _trade(token_address="addr-1")
        trade.pop("opened_at")
        context = pa._entry_context_for(trade, buy_entries)
        self.assertEqual(context["score"], 90)

    def test_load_buy_log_entries_skips_malformed_lines(self):
        self._write_paper_log_lines([{"action": "SKIP", "token_address": "addr-1", "timestamp": "x"}])
        with paper_logger.LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("not valid json at all\n")
        self._write_paper_log_lines([{"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00", "score": 70}])
        by_address = pa._load_buy_log_entries("paper")
        self.assertEqual(len(by_address["addr-1"]), 1)  # SKIP line and garbage line both excluded
        self.assertEqual(by_address["addr-1"][0]["score"], 70)

    def test_load_buy_log_entries_returns_empty_when_no_log_file(self):
        self.assertEqual(pa._load_buy_log_entries("paper"), {})


class TestAnalyzeTradesAggregateStats(unittest.TestCase):
    """Pure function tests -- no file I/O, batch given directly."""

    def test_empty_batch_returns_a_zeroed_report(self):
        report = pa.analyze_trades([])
        self.assertEqual(report.total_trades, 0)
        self.assertIsNone(report.win_rate)
        self.assertIsNone(report.profit_factor)
        self.assertIsNone(report.expectancy_usd)
        self.assertEqual(report.total_pnl_usd, 0.0)
        self.assertEqual(report.by_score_bucket, {})

    def test_none_or_non_list_input_never_raises(self):
        self.assertEqual(pa.analyze_trades(None).total_trades, 0)

    def test_win_loss_breakeven_counts(self):
        trades = [_trade(pnl_usd=10), _trade(pnl_usd=-5), _trade(pnl_usd=0)]
        report = pa.analyze_trades(trades)
        self.assertEqual(report.wins, 1)
        self.assertEqual(report.losses, 1)
        self.assertEqual(report.breakeven, 1)
        self.assertAlmostEqual(report.win_rate, 1 / 3)
        self.assertAlmostEqual(report.loss_rate, 1 / 3)

    def test_average_win_and_average_loss(self):
        trades = [_trade(pnl_usd=10), _trade(pnl_usd=30), _trade(pnl_usd=-4), _trade(pnl_usd=-8)]
        report = pa.analyze_trades(trades)
        self.assertAlmostEqual(report.average_win_usd, 20.0)
        self.assertAlmostEqual(report.average_loss_usd, -6.0)

    def test_profit_factor(self):
        trades = [_trade(pnl_usd=30), _trade(pnl_usd=10), _trade(pnl_usd=-10), _trade(pnl_usd=-10)]
        report = pa.analyze_trades(trades)
        self.assertAlmostEqual(report.profit_factor, 2.0)  # 40 / 20

    def test_profit_factor_is_none_with_no_losses(self):
        report = pa.analyze_trades([_trade(pnl_usd=10), _trade(pnl_usd=5)])
        self.assertIsNone(report.profit_factor)

    def test_expectancy_is_mean_pnl_across_the_batch(self):
        trades = [_trade(pnl_usd=10), _trade(pnl_usd=-4), _trade(pnl_usd=0)]
        report = pa.analyze_trades(trades)
        self.assertAlmostEqual(report.expectancy_usd, 2.0)  # (10 - 4 + 0) / 3

    def test_total_pnl(self):
        trades = [_trade(pnl_usd=10), _trade(pnl_usd=-3.5)]
        report = pa.analyze_trades(trades)
        self.assertAlmostEqual(report.total_pnl_usd, 6.5)

    def test_average_holding_time(self):
        trades = [
            _trade(opened_at="2026-01-01T00:00:00+00:00", closed_at="2026-01-01T00:10:00+00:00"),
            _trade(opened_at="2026-01-01T00:00:00+00:00", closed_at="2026-01-01T00:20:00+00:00"),
        ]
        report = pa.analyze_trades(trades)
        self.assertAlmostEqual(report.average_holding_time_minutes, 15.0)

    def test_holding_time_skips_trades_with_unparseable_timestamps(self):
        trades = [
            _trade(opened_at="2026-01-01T00:00:00+00:00", closed_at="2026-01-01T00:10:00+00:00"),
            _trade(opened_at="not-a-date", closed_at="also-not-a-date"),
        ]
        report = pa.analyze_trades(trades)
        self.assertAlmostEqual(report.average_holding_time_minutes, 10.0)

    def test_a_trade_missing_pnl_usd_is_excluded_from_pnl_stats_not_crashed_on(self):
        trades = [_trade(pnl_usd=10), {"token_address": "addr-x", "opened_at": "2026-01-01T00:00:00+00:00"}]
        report = pa.analyze_trades(trades)
        self.assertEqual(report.total_trades, 2)  # still counted in total
        self.assertEqual(report.wins, 1)  # but excluded from win/loss/pnl math

    def test_non_dict_entries_in_the_batch_are_filtered_out(self):
        report = pa.analyze_trades([_trade(pnl_usd=5), "not-a-dict", None, 42])
        self.assertEqual(report.total_trades, 1)


class TestTopNRecent(unittest.TestCase):
    def test_keeps_only_the_n_most_recently_closed(self):
        trades = [
            _trade(pnl_usd=1, closed_at="2026-01-01T00:00:00+00:00"),
            _trade(pnl_usd=2, closed_at="2026-01-02T00:00:00+00:00"),
            _trade(pnl_usd=3, closed_at="2026-01-03T00:00:00+00:00"),
        ]
        report = pa.analyze_trades(trades, top_n_recent=2)
        self.assertEqual(report.total_trades, 2)
        self.assertAlmostEqual(report.total_pnl_usd, 5.0)  # trades 2 + 3, not 1

    def test_top_n_recent_zero_yields_empty_report(self):
        report = pa.analyze_trades([_trade()], top_n_recent=0)
        self.assertEqual(report.total_trades, 0)

    def test_trades_with_unparseable_closed_at_sort_last_and_get_trimmed_first(self):
        trades = [
            _trade(pnl_usd=1, closed_at="not-a-date"),
            _trade(pnl_usd=2, closed_at="2026-01-01T00:00:00+00:00"),
        ]
        report = pa.analyze_trades(trades, top_n_recent=1)
        self.assertAlmostEqual(report.total_pnl_usd, 1.0)  # the parseable, earlier one is kept


class TestBucketSummaryShape(unittest.TestCase):
    def test_bucket_summary_of_empty_list(self):
        self.assertEqual(pa._bucket_summary([]), {"trades": 0, "win_rate": None, "average_pnl_usd": None, "total_pnl_usd": 0.0})

    def test_bucket_summary_basic(self):
        summary = pa._bucket_summary([10.0, -5.0, 10.0])
        self.assertEqual(summary["trades"], 3)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3)
        self.assertAlmostEqual(summary["average_pnl_usd"], 5.0)
        self.assertAlmostEqual(summary["total_pnl_usd"], 15.0)


class TestScoreBucketAndSignalDirection(unittest.TestCase):
    def test_score_buckets(self):
        self.assertEqual(pa._score_bucket(10), "0-49")
        self.assertEqual(pa._score_bucket(55), "50-59")
        self.assertEqual(pa._score_bucket(79), "70-79")
        self.assertEqual(pa._score_bucket(90), "90-100")
        self.assertEqual(pa._score_bucket(100), "90-100")

    def test_score_bucket_none_is_no_context(self):
        self.assertEqual(pa._score_bucket(None), pa.NO_CONTEXT)

    def test_score_bucket_non_numeric_is_no_context(self):
        self.assertEqual(pa._score_bucket("not-a-number"), pa.NO_CONTEXT)

    def test_signal_direction(self):
        self.assertEqual(pa._signal_direction(0.5), "positive")
        self.assertEqual(pa._signal_direction(-0.5), "negative")
        self.assertEqual(pa._signal_direction(0), "flat")
        self.assertEqual(pa._signal_direction(None), pa.NO_CONTEXT)
        self.assertEqual(pa._signal_direction("bad"), pa.NO_CONTEXT)


class TestBreakdownsWithEntryContext(IsolatedFilesTestCase):
    def test_by_score_bucket_uses_correlated_entry_context(self):
        self._write_paper_log_lines([
            {"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00", "score": 85, "trend": "STRONG"},
        ])
        self._write_paper_closed_trades([_trade(pnl_usd=15, token_address="addr-1", opened_at="2026-01-01T00:00:00+00:00")])
        report = pa.analyze_recent(mode="paper")
        self.assertIn("80-89", report.by_score_bucket)
        self.assertIn("STRONG", report.by_trend)

    def test_a_trade_with_no_matching_buy_log_entry_still_counts_fully(self):
        trades = [_trade(pnl_usd=15, token_address="addr-unlogged")]
        report = pa.analyze_trades(trades)
        self.assertEqual(report.total_trades, 1)
        self.assertIn(pa.NO_CONTEXT, report.by_score_bucket)
        self.assertEqual(report.by_score_bucket[pa.NO_CONTEXT]["trades"], 1)

    def test_by_stage_and_by_entry_reason(self):
        self._write_paper_log_lines([
            {
                "action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00",
                "score": 85, "trend": "STRONG", "stage": "EARLY", "reason": "passed score/trend/risk/sellability screening",
            },
        ])
        self._write_paper_closed_trades([_trade(pnl_usd=15, token_address="addr-1", opened_at="2026-01-01T00:00:00+00:00")])
        report = pa.analyze_recent(mode="paper")
        self.assertIn("EARLY", report.by_stage)
        self.assertIn("passed score/trend/risk/sellability screening", report.by_entry_reason)

    def test_by_volume_momentum_and_price_acceleration_direction(self):
        self._write_paper_log_lines([
            {
                "action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00",
                "signals": {"volume_momentum": 0.4, "price_acceleration": -0.1},
            },
        ])
        self._write_paper_closed_trades([_trade(pnl_usd=15, token_address="addr-1", opened_at="2026-01-01T00:00:00+00:00")])
        report = pa.analyze_recent(mode="paper")
        self.assertIn("positive", report.by_volume_momentum_direction)
        self.assertIn("negative", report.by_price_acceleration_direction)

    def test_malformed_signals_value_in_the_log_entry_does_not_crash(self):
        self._write_paper_log_lines([
            {"action": "BUY", "token_address": "addr-1", "timestamp": "2026-01-01T00:00:00+00:00", "signals": "not-a-dict"},
        ])
        self._write_paper_closed_trades([_trade(pnl_usd=15, token_address="addr-1", opened_at="2026-01-01T00:00:00+00:00")])
        report = pa.analyze_recent(mode="paper")  # must not raise
        self.assertIn(pa.NO_CONTEXT, report.by_volume_momentum_direction)


class TestAnalyzeRecent(IsolatedFilesTestCase):
    def test_no_data_at_all_returns_empty_report(self):
        report = pa.analyze_recent(mode="paper")
        self.assertEqual(report.total_trades, 0)

    def test_since_filters_out_older_trades(self):
        self._write_paper_closed_trades([
            _trade(pnl_usd=1, closed_at="2026-01-01T00:00:00+00:00"),
            _trade(pnl_usd=2, closed_at="2026-01-10T00:00:00+00:00"),
        ])
        report = pa.analyze_recent(mode="paper", since="2026-01-05T00:00:00+00:00")
        self.assertEqual(report.total_trades, 1)
        self.assertAlmostEqual(report.total_pnl_usd, 2.0)

    def test_since_as_a_datetime_object_also_works(self):
        self._write_paper_closed_trades([_trade(pnl_usd=1, closed_at="2026-01-01T00:00:00+00:00")])
        cutoff = datetime(2026, 1, 2, tzinfo=timezone.utc)
        report = pa.analyze_recent(mode="paper", since=cutoff)
        self.assertEqual(report.total_trades, 0)

    def test_an_unparseable_since_value_is_ignored_not_raised(self):
        self._write_paper_closed_trades([_trade(pnl_usd=1)])
        report = pa.analyze_recent(mode="paper", since="not-a-real-date")  # must not raise
        self.assertEqual(report.total_trades, 1)

    def test_both_mode_combines_paper_and_live(self):
        self._write_paper_closed_trades([_trade(pnl_usd=1, token_address="p1")])
        self._write_live_closed_trades([_trade(pnl_usd=2, token_address="l1")])
        report = pa.analyze_recent(mode="both")
        self.assertEqual(report.total_trades, 2)


class TestIsolationFromWalletAndExecution(unittest.TestCase):
    def test_module_source_does_not_import_wallet_risk_or_decision_modules(self):
        import inspect

        forbidden = ("src.wallet", "src.risk", "src.live_trader", "src.paper_trader")
        source = inspect.getsource(pa)
        for module_name in forbidden:
            self.assertNotIn(module_name, source, f"{module_name} must never be imported by performance_analyzer.py")

    def test_decision_and_execution_modules_do_not_import_performance_analyzer(self):
        import inspect

        import src.live_trader as live_trader
        import src.paper_trader as paper_trader
        import src.risk as risk
        import src.wallet as wallet

        # Check for an actual import statement, not just any mention of
        # the module's name (which can legitimately appear in a comment
        # -- e.g. live_trader.py/paper_trader.py's Phase 5 logging
        # comments reference "src.performance_analyzer" in prose while
        # correctly never importing it).
        forbidden_imports = ("import src.performance_analyzer", "from src.performance_analyzer", "from src import performance_analyzer")
        for module in (wallet, risk, live_trader, paper_trader):
            source = inspect.getsource(module)
            for forbidden in forbidden_imports:
                self.assertNotIn(forbidden, source, f"{module.__name__} must never import performance_analyzer")


if __name__ == "__main__":
    unittest.main()
