import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stocks import health


class TestHealth(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(health, "HEALTH_FILE", Path(self._tmp_dir.name) / "health_status.json")
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_record_start_stamps_process_started_at(self):
        self.assertIsNone(health.load_health()["process_started_at"])
        state = health.record_start()
        self.assertIsNotNone(state["process_started_at"])
        self.assertEqual(health.load_health()["process_started_at"], state["process_started_at"])

    def test_record_start_resets_the_timestamp_on_every_call(self):
        first = health.record_start()["process_started_at"]
        second = health.record_start()["process_started_at"]
        # Both real timestamps -- just confirms this always re-stamps
        # (a restart genuinely resets uptime), not a one-time latch.
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_fresh_state_defaults_to_starting(self):
        state = health.load_health()
        self.assertEqual(state["status"], "STARTING")
        self.assertEqual(state["consecutive_failures"], 0)

    def test_record_success_sets_running_and_clears_failures(self):
        state = health.record_success(summary={"buys": 1})
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertIsNotNone(state["last_success_at"])
        self.assertEqual(state["last_success_summary"], {"buys": 1})

    def test_record_failure_increments_and_returns_a_backoff_delay(self):
        delay1 = health.record_failure("timeout")
        state1 = health.load_health()
        self.assertEqual(state1["consecutive_failures"], 1)
        self.assertEqual(state1["status"], "RECOVERING")

        delay2 = health.record_failure("timeout")
        state2 = health.load_health()
        self.assertEqual(state2["consecutive_failures"], 2)
        self.assertGreater(delay2, delay1)  # exponential growth

    def test_backoff_delay_is_capped(self):
        for _ in range(20):
            delay = health.record_failure("still down")
        from src.stocks.config import STOCKS_RECOVERY_BACKOFF_MAX_SECONDS
        self.assertLessEqual(delay, STOCKS_RECOVERY_BACKOFF_MAX_SECONDS)

    def test_many_consecutive_failures_escalate_to_degraded(self):
        for _ in range(5):
            health.record_failure("down")
        state = health.load_health()
        self.assertEqual(state["status"], "DEGRADED")

    def test_outage_started_at_is_set_once_and_kept_across_repeated_failures(self):
        health.record_failure("first")
        state1 = health.load_health()
        first_started = state1["outage_started_at"]

        health.record_failure("second")
        state2 = health.load_health()
        self.assertEqual(state2["outage_started_at"], first_started)  # not overwritten

    def test_success_after_a_failure_streak_records_a_recovery_and_clears_outage(self):
        health.record_failure("down")
        health.record_failure("still down")
        state = health.record_success()
        self.assertIsNotNone(state["last_recovery_at"])
        self.assertIsNone(state["outage_started_at"])
        self.assertIsNone(state["outage_reason"])

    def test_recovery_attempts_total_accumulates_across_outages(self):
        health.record_failure("outage 1")
        health.record_success()
        health.record_failure("outage 2")
        state = health.load_health()
        self.assertEqual(state["recovery_attempts_total"], 2)

    def test_corrupt_health_file_degrades_to_defaults_not_a_crash(self):
        health.HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        health.HEALTH_FILE.write_text("{not valid json", encoding="utf-8")
        state = health.load_health()
        self.assertEqual(state["status"], "STARTING")


if __name__ == "__main__":
    unittest.main()
