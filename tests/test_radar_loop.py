import unittest
from unittest import mock

from src import radar


class TestRunForever(unittest.TestCase):
    def test_stops_after_max_iterations(self):
        with mock.patch("src.radar.run_once") as mock_run_once, mock.patch("src.radar.time.sleep") as mock_sleep:
            radar.run_forever(interval_seconds=5, max_iterations=3)

        self.assertEqual(mock_run_once.call_count, 3)
        # No sleep after the final iteration.
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(5)

    def test_a_failing_cycle_does_not_stop_the_loop(self):
        with mock.patch(
            "src.radar.run_once", side_effect=[RuntimeError("boom"), None, None]
        ) as mock_run_once, mock.patch("src.radar.time.sleep"):
            radar.run_forever(interval_seconds=1, max_iterations=3)

        self.assertEqual(mock_run_once.call_count, 3)

    def test_keyboard_interrupt_stops_cleanly(self):
        with mock.patch("src.radar.run_once", side_effect=KeyboardInterrupt), mock.patch("src.radar.time.sleep"):
            radar.run_forever(interval_seconds=1, max_iterations=None)  # should not hang or raise

    def test_passes_on_results_callback_through_to_run_once(self):
        callback = mock.Mock()
        with mock.patch("src.radar.run_once") as mock_run_once, mock.patch("src.radar.time.sleep"):
            radar.run_forever(interval_seconds=1, max_iterations=1, on_results=callback)

        mock_run_once.assert_called_once_with(on_results=callback)


FULL_RESULT = {
    "score": 90, "base_score": 90, "momentum_score": 90, "ok": True, "symbol": "GOOD",
    "stage": "EARLY", "age": 5.0, "liquidity": 20000, "volume": 60000, "buys": 100,
    "sells": 50, "address": "addr-1", "price_usd": 1.0, "trend": "STRONG",
}


class TestRunOnce(unittest.TestCase):
    def test_on_results_receives_the_results_list(self):
        callback = mock.Mock()
        with mock.patch("src.radar.run_radar", return_value=[FULL_RESULT]):
            results = radar.run_once(on_results=callback)

        callback.assert_called_once_with([FULL_RESULT])
        self.assertEqual(results, [FULL_RESULT])

    def test_a_failing_on_results_callback_does_not_raise(self):
        callback = mock.Mock(side_effect=RuntimeError("paper trader exploded"))
        with mock.patch("src.radar.run_radar", return_value=[]):
            results = radar.run_once(on_results=callback)  # must not raise

        self.assertEqual(results, [])


class TestArgParsing(unittest.TestCase):
    def test_defaults_are_single_run_no_paper(self):
        args = radar._parse_args([])
        self.assertFalse(args.loop)
        self.assertFalse(args.paper)
        self.assertIsNone(args.interval)
        self.assertIsNone(args.max_iterations)

    def test_flags_parse(self):
        args = radar._parse_args(["--loop", "--interval", "30", "--paper", "--max-iterations", "5"])
        self.assertTrue(args.loop)
        self.assertTrue(args.paper)
        self.assertEqual(args.interval, 30)
        self.assertEqual(args.max_iterations, 5)


class TestMainWiresPaperTraderOnlyWhenRequested(unittest.TestCase):
    def test_paper_flag_imports_and_uses_paper_trader(self):
        fake_run_paper_cycle = mock.Mock()
        with mock.patch("src.paper_trader.run_paper_cycle", fake_run_paper_cycle), mock.patch(
            "src.radar.run_once"
        ) as mock_run_once:
            radar.main(["--paper"])

        called_kwargs = mock_run_once.call_args.kwargs
        self.assertIs(called_kwargs["on_results"], fake_run_paper_cycle)

    def test_without_paper_flag_on_results_is_none(self):
        with mock.patch("src.radar.run_once") as mock_run_once:
            radar.main([])

        self.assertIsNone(mock_run_once.call_args.kwargs["on_results"])


if __name__ == "__main__":
    unittest.main()
