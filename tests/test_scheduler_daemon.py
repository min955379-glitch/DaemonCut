from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.database import DatabaseManager
from backend.maintenance import CleanupSummary
from backend.scheduler_daemon import SchedulerDaemon, is_pkt_peak_window, wait_for_network


class SchedulerDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = DatabaseManager(":memory:")
        self.addCleanup(self.database.close)
        self.workspace = self.database.create_workspace("Scheduler Test", slug="scheduler-test")
        self.channel = self.database.add_channel(
            self.workspace["workspace_id"],
            "youtube",
            "channel-123",
            display_name="Test channel",
        )
        now = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
        episodes = []
        for index in range(7):
            schedule = now - timedelta(minutes=5) if index == 0 else now + timedelta(days=1, minutes=index)
            episodes.append({
                "title": f"Episode {index + 1}",
                "topic": f"Original topic {index + 1}",
                "script": f"Story {index + 1}",
                "scheduled_for": schedule.isoformat(),
                "metadata": {"rendered_path": f"renders_archive/ep-{index + 1}.mp4"},
            })
        self.batch = self.database.create_saturday_batch(
            self.workspace["workspace_id"],
            episodes,
            batch_date=date(2026, 11, 14),
        )
        self.now = now

    def test_bounded_exponential_network_retry(self) -> None:
        attempts = 0
        delays: list[float] = []

        def probe() -> bool:
            nonlocal attempts
            attempts += 1
            return attempts == 4

        result = wait_for_network(
            max_attempts=5,
            base_delay_seconds=1,
            max_delay_seconds=3,
            probe=probe,
            sleep=delays.append,
        )
        self.assertTrue(result.connected)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(delays, [1.0, 2.0, 3.0])
        self.assertEqual(result.delays_seconds, (1.0, 2.0, 3.0))

    def test_network_retries_stop_at_configured_attempt_count(self) -> None:
        attempts = []
        delays: list[float] = []
        result = wait_for_network(
            max_attempts=3,
            base_delay_seconds=0.5,
            max_delay_seconds=1,
            probe=lambda: attempts.append(True) and False,
            sleep=delays.append,
        )
        self.assertFalse(result.connected)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_pkt_peak_windows_use_asia_karachi_and_cross_midnight(self) -> None:
        pkt = ZoneInfo("Asia/Karachi")
        self.assertTrue(is_pkt_peak_window(datetime(2026, 11, 16, 21, 30, tzinfo=pkt)))
        self.assertTrue(is_pkt_peak_window(datetime(2026, 11, 17, 0, 30, tzinfo=pkt)))
        self.assertTrue(is_pkt_peak_window(datetime(2026, 11, 16, 21, 18, tzinfo=pkt), window_minutes=15))
        self.assertFalse(is_pkt_peak_window(datetime(2026, 11, 16, 22, 0, tzinfo=pkt), window_minutes=15))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            is_pkt_peak_window(datetime(2026, 11, 16, 21, 30))

    def test_due_query_selects_only_past_scheduled_episodes(self) -> None:
        daemon = SchedulerDaemon(self.database, clock=lambda: self.now)
        due = daemon.due_episodes(now=self.now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][1]["title"], "Episode 1")

    def test_upload_uses_database_lock_and_restart_does_not_post_twice(self) -> None:
        uploads: list[tuple[str, str]] = []

        def handler(episode: dict[str, object], channel: dict[str, object], **kwargs: object) -> dict[str, str]:
            uploads.append((str(episode["episode_id"]), str(kwargs["idempotency_key"])))
            return {"remote_id": "platform-post-1", "verified": True}

        daemon = SchedulerDaemon(
            self.database,
            upload_handler=handler,
            clock=lambda: self.now,
            network_probe=lambda: True,
        )
        first = daemon.run_once(wait_for_connectivity=False, cleanup_scratch=False, now=self.now)
        self.assertEqual(first.due_episode_count, 1)
        self.assertEqual(first.attempts[0].status, "completed")
        self.assertEqual(first.attempts[0].remote_id, "platform-post-1")
        self.assertEqual(len(uploads), 1)

        # A fresh daemon instance simulates a PC/API restart; the completed DB
        # lock is replayed instead of invoking the publisher a second time.
        restarted = SchedulerDaemon(
            self.database,
            upload_handler=handler,
            clock=lambda: self.now,
            network_probe=lambda: True,
        )
        second = restarted.run_once(wait_for_connectivity=False, cleanup_scratch=False, now=self.now)
        self.assertEqual(second.attempts[0].status, "already_completed")
        self.assertEqual(second.attempts[0].remote_id, "platform-post-1")
        self.assertEqual(len(uploads), 1)

    def test_failed_upload_marks_lease_failed_and_can_retry(self) -> None:
        calls = 0

        def flaky_handler(_episode: object, _channel: object, **_kwargs: object) -> dict[str, str]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary publisher outage")
            return {"remote_id": "retry-post"}

        daemon = SchedulerDaemon(self.database, upload_handler=flaky_handler, clock=lambda: self.now)
        first = daemon.run_once(wait_for_connectivity=False, cleanup_scratch=False, now=self.now)
        self.assertEqual(first.attempts[0].status, "failed")
        second = daemon.run_once(wait_for_connectivity=False, cleanup_scratch=False, now=self.now)
        self.assertEqual(second.attempts[0].status, "completed")
        self.assertEqual(calls, 2)

    def test_no_publisher_does_not_claim_lock_or_fake_completion(self) -> None:
        daemon = SchedulerDaemon(self.database, clock=lambda: self.now)
        result = daemon.run_once(wait_for_connectivity=False, cleanup_scratch=False, now=self.now)
        self.assertEqual(result.attempts[0].status, "waiting_for_adapter")
        episode = self.batch["episodes"][0]
        lock = self.database.get_upload_lock(
            self.workspace["workspace_id"], "youtube", "channel-123", f"episode:{episode['episode_id']}"
        )
        self.assertIsNone(lock)

    def test_boot_run_cleans_scratch_and_requests_shutdown_when_idle(self) -> None:
        cleanups: list[dict[str, object]] = []
        empty_db = DatabaseManager(":memory:")
        self.addCleanup(empty_db.close)
        summary = CleanupSummary(dry_run=False, ttl_hours=24)

        def cleanup(**kwargs: object) -> CleanupSummary:
            cleanups.append(kwargs)
            return summary

        daemon = SchedulerDaemon(
            empty_db,
            network_probe=lambda: True,
            cleanup=cleanup,
            clock=lambda: self.now,
            max_network_attempts=1,
        )
        result = daemon.run_boot_watchdog()
        self.assertEqual(result.due_episode_count, 0)
        self.assertTrue(result.shutdown_requested)
        self.assertTrue(daemon.shutdown_event.is_set())
        self.assertEqual(cleanups, [{"ttl_hours": 24, "dry_run": False}])

    def test_due_boot_run_with_adapter_shuts_down_after_verified_completion(self) -> None:
        daemon = SchedulerDaemon(
            self.database,
            upload_handler=lambda *_args, **_kwargs: {"remote_id": "verified"},
            network_probe=lambda: True,
            cleanup=lambda **_kwargs: CleanupSummary(dry_run=False, ttl_hours=24),
            clock=lambda: self.now,
            max_network_attempts=1,
        )
        result = daemon.run_boot_watchdog()
        self.assertTrue(result.shutdown_requested)
        self.assertEqual(result.attempts[0].status, "completed")

    def test_due_boot_run_without_adapter_does_not_shutdown_as_if_uploads_completed(self) -> None:
        daemon = SchedulerDaemon(
            self.database,
            network_probe=lambda: True,
            cleanup=lambda **_kwargs: CleanupSummary(dry_run=False, ttl_hours=24),
            clock=lambda: self.now,
            max_network_attempts=1,
        )
        result = daemon.run_boot_watchdog()
        self.assertFalse(result.shutdown_requested)
        self.assertFalse(daemon.shutdown_event.is_set())
        self.assertEqual(result.attempts[0].status, "waiting_for_adapter")


if __name__ == "__main__":
    unittest.main()
