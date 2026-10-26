import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from local_runner import scheduler


class ReleaseSlotTests(unittest.TestCase):
    def due(self, value):
        return scheduler.latest_due_slot(datetime.fromisoformat(value))

    def test_summer_announcement_is_monday_0805_beijing(self):
        slot = self.due("2026-10-26T08:05:00+08:00")
        self.assertEqual(slot.date, "2026-10-26")
        self.assertEqual(slot.scheduled_at.astimezone(scheduler.BEIJING).hour, 8)

    def test_winter_announcement_is_monday_0905_beijing(self):
        slot = self.due("2026-11-02T09:05:00+08:00")
        self.assertEqual(slot.date, "2026-11-02")
        self.assertEqual(slot.scheduled_at.astimezone(scheduler.BEIJING).hour, 9)

    def test_dst_end_does_not_run_one_hour_early(self):
        slot = self.due("2026-11-02T08:05:00+08:00")
        self.assertEqual(slot.date, "2026-10-30")

    def test_dst_start_moves_back_to_0805(self):
        self.assertEqual(self.due("2026-03-09T08:05:00+08:00").date, "2026-03-09")
        self.assertEqual(self.due("2026-03-09T08:04:59+08:00").date, "2026-03-06")

    def test_weekend_does_not_create_a_new_release(self):
        self.assertEqual(self.due("2026-10-25T15:00:00+08:00").date, "2026-10-23")

    def test_late_start_and_before_next_release_retain_last_due(self):
        self.assertEqual(self.due("2026-10-26T22:00:00+08:00").date, "2026-10-26")
        self.assertEqual(self.due("2026-10-27T07:00:00+08:00").date, "2026-10-26")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.now = datetime.fromisoformat("2026-10-26T10:30:00+08:00")
        self.clock = patch.object(scheduler, "now_utc", side_effect=lambda: self.now).start()
        self.addCleanup(patch.stopall)
        self.network = patch.object(scheduler, "network_ready", return_value=(True, "")).start()
        self.pipeline = patch.object(scheduler, "run_pipeline", return_value=0).start()
        patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-not-a-real-key"}).start()
        patch.object(scheduler, "log").start()

    def test_starting_late_runs_once_and_records_release(self):
        self.assertEqual(scheduler.run_once(self.workspace), 0)
        self.pipeline.assert_called_once_with(self.workspace, "2026-10-26", "2026-10-26")
        self.assertEqual(scheduler.read_state(self.workspace)["last_success_slot"], "2026-10-26")
        scheduler.run_once(self.workspace)
        self.assertEqual(self.pipeline.call_count, 1)

    def test_offline_does_not_consume_attempt_then_reconnect_runs(self):
        self.network.return_value = (False, "offline")
        scheduler.run_once(self.workspace)
        state = scheduler.read_state(self.workspace)
        self.assertEqual(state["status"], "waiting_network")
        self.assertEqual(state["attempts"], 0)
        self.assertNotIn("last_success_slot", state)
        self.pipeline.assert_not_called()
        self.network.return_value = (True, "")
        scheduler.run_once(self.workspace)
        self.pipeline.assert_called_once()

    def test_missing_key_waits_without_network_or_paid_attempt(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            scheduler.run_once(self.workspace)
        self.network.assert_not_called()
        self.pipeline.assert_not_called()
        self.assertEqual(scheduler.read_state(self.workspace)["attempts"], 0)
        self.assertEqual(scheduler.read_state(self.workspace)["status"], "waiting_credentials")

    def test_failure_cooldown_and_limit_reset_next_release(self):
        self.pipeline.return_value = 7
        self.assertEqual(scheduler.run_once(self.workspace), 7)
        scheduler.run_once(self.workspace)
        self.assertEqual(self.pipeline.call_count, 1)
        self.now += timedelta(minutes=10)
        scheduler.run_once(self.workspace)
        self.now += timedelta(minutes=10)
        scheduler.run_once(self.workspace)
        self.now += timedelta(hours=1)
        scheduler.run_once(self.workspace)
        state = scheduler.read_state(self.workspace)
        self.assertEqual(state["status"], "retry_limit_reached")
        self.assertEqual(self.pipeline.call_count, 3)
        self.assertNotIn("last_success_slot", state)
        self.now += timedelta(days=1)
        self.pipeline.return_value = 0
        scheduler.run_once(self.workspace)
        self.assertEqual(self.pipeline.call_count, 4)
        self.assertEqual(scheduler.read_state(self.workspace)["last_success_slot"], "2026-10-27")

    def test_catch_up_before_today_slot_uses_actual_date_without_marking_future(self):
        self.now = datetime.fromisoformat("2026-10-27T07:00:00+08:00")
        scheduler.run_once(self.workspace, run_now=True)
        self.pipeline.assert_called_once_with(self.workspace, "2026-10-27", "2026-10-26")
        self.assertEqual(scheduler.read_state(self.workspace)["last_success_slot"], "2026-10-26")

    def test_retry_wait_starts_after_long_failed_run(self):
        def fail_after_work(*args):
            self.now += timedelta(minutes=20)
            return 1

        self.pipeline.side_effect = fail_after_work
        scheduler.run_once(self.workspace)
        scheduler.run_once(self.workspace)
        self.assertEqual(self.pipeline.call_count, 1)
        state = scheduler.read_state(self.workspace)
        self.assertEqual(datetime.fromisoformat(state["next_retry_at"]), self.now + timedelta(minutes=10))

    def test_delayed_or_holiday_release_waits_without_consuming_attempts(self):
        self.pipeline.return_value = scheduler.WAITING_RELEASE_EXIT_CODE
        for _ in range(5):
            self.assertEqual(scheduler.run_once(self.workspace), 0)
            state = scheduler.read_state(self.workspace)
            self.assertEqual(state["status"], "waiting_release")
            self.assertEqual(state["attempts"], 0)
            self.assertNotIn("last_success_slot", state)
            self.now += timedelta(minutes=5)
        self.assertEqual(self.pipeline.call_count, 5)
        self.pipeline.return_value = 0
        scheduler.run_once(self.workspace)
        self.assertEqual(scheduler.read_state(self.workspace)["last_success_slot"], "2026-10-26")

    def test_waiting_for_release_preserves_existing_processing_failure_count(self):
        scheduler.write_state(self.workspace, {"active_slot": "2026-10-26", "attempts": 2})
        self.pipeline.return_value = scheduler.WAITING_RELEASE_EXIT_CODE
        scheduler.run_once(self.workspace, run_now=True)
        state = scheduler.read_state(self.workspace)
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["status"], "waiting_release")

    def test_new_slot_status_is_due_after_previous_retry_limit(self):
        scheduler.write_state(self.workspace, {
            "active_slot": "2026-10-23", "attempts": 3, "status": "retry_limit_reached"
        })
        report = scheduler.status(self.workspace, self.now)
        self.assertEqual(report["waiting"], "due")
        self.assertEqual(report["attempts"], 0)

    def test_run_across_next_due_does_not_mark_new_slot_successful(self):
        self.now = datetime.fromisoformat("2026-10-27T07:55:00+08:00")

        def finish_after_next_due(*args):
            self.now += timedelta(minutes=20)
            return 0

        self.pipeline.side_effect = finish_after_next_due
        scheduler.run_once(self.workspace)
        self.assertEqual(scheduler.read_state(self.workspace)["last_success_slot"], "2026-10-26")
        self.assertEqual(scheduler.status(self.workspace)["due_slot"], "2026-10-27")
        scheduler.run_once(self.workspace)
        self.assertEqual(scheduler.read_state(self.workspace)["last_success_slot"], "2026-10-27")

    def test_status_does_not_access_credentials_network_or_run(self):
        with patch.object(scheduler.os, "environ") as env:
            report = scheduler.status(self.workspace, self.now)
        env.get.assert_not_called()
        self.network.assert_not_called()
        self.pipeline.assert_not_called()
        self.assertEqual(report["due_slot"], "2026-10-26")
        self.assertFalse((self.workspace / "scheduler-state.json").exists())

    def test_run_now_still_requires_network(self):
        self.network.return_value = (False, "offline")
        scheduler.run_once(self.workspace, run_now=True)
        self.pipeline.assert_not_called()

    def test_run_now_allows_validation_after_success(self):
        scheduler.run_once(self.workspace)
        scheduler.run_once(self.workspace, run_now=True)
        self.assertEqual(self.pipeline.call_count, 2)

    def test_process_lock_blocks_duplicate_invocation(self):
        with scheduler.exclusive_lock(self.workspace) as acquired:
            self.assertTrue(acquired)
            scheduler.run_once(self.workspace, run_now=True)
        self.pipeline.assert_not_called()

    def test_corrupt_state_fails_closed(self):
        (self.workspace / "scheduler-state.json").write_text("not-json", encoding="utf-8")
        self.assertEqual(scheduler.main(["--workspace", str(self.workspace)]), 1)
        self.pipeline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
