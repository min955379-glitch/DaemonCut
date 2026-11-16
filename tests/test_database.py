from __future__ import annotations

import unittest
from datetime import date

from backend.database import (
    ConflictError,
    DatabaseManager,
    IdempotencyConflictError,
)


class DatabaseManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = DatabaseManager(":memory:")
        self.workspace = self.db.create_workspace(
            "Breezy & Mal",
            timezone_name="Asia/Karachi",
            settings={"niche": "3D cartoon stories"},
        )

    def tearDown(self) -> None:
        self.db.close()

    def _seven_episodes(self) -> list[dict[str, str]]:
        return [
            {"topic": f"Original idea {index}", "title": f"Episode {index}", "script": f"Script {index}"}
            for index in range(1, 8)
        ]

    def test_saturday_batch_is_exactly_seven_and_persistent_in_manager(self) -> None:
        batch = self.db.create_saturday_batch(
            self.workspace["workspace_id"],
            self._seven_episodes(),
            batch_date=date(2026, 11, 21),
        )
        self.assertEqual(batch["target_count"], 7)
        self.assertEqual(len(batch["episodes"]), 7)
        self.assertEqual([item["episode_number"] for item in batch["episodes"]], list(range(1, 8)))
        self.assertEqual(len(self.db.list_queue(self.workspace["workspace_id"], statuses=["queued"])), 7)

    def test_standalone_episode_can_be_queued_outside_a_weekly_batch(self) -> None:
        episode = self.db.create_episode(
            self.workspace["workspace_id"],
            topic="An original tiny workshop mystery",
            title="The Missing Compass",
            hook="The compass points somewhere unexpected.",
            script="Breezy and Mal investigate a loose floorboard.",
            scheduled_for="2026-11-22T16:30:00+00:00",
            metadata={"video_path": "renders_archive/example.mp4"},
            status="scheduled",
        )
        self.assertEqual(episode["status"], "scheduled")
        self.assertIsNone(episode["batch_id"])
        self.assertIsNone(episode["episode_number"])
        self.assertEqual(episode["metadata"]["video_path"], "renders_archive/example.mp4")
        self.assertEqual(self.db.get_episode(episode["episode_id"])["title"], "The Missing Compass")

    def test_batch_requires_seven_and_saturday(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 7"):
            self.db.create_saturday_batch(
                self.workspace["workspace_id"], self._seven_episodes()[:6], batch_date="2026-11-21"
            )
        with self.assertRaisesRegex(ValueError, "not a Saturday"):
            self.db.create_saturday_batch(
                self.workspace["workspace_id"], self._seven_episodes(), batch_date="2026-11-20"
            )

    def test_variants_and_winner_selection(self) -> None:
        batch = self.db.create_saturday_batch(
            self.workspace["workspace_id"], self._seven_episodes(), batch_date="2026-11-21"
        )
        episode_id = batch["episodes"][0]["episode_id"]
        saved = self.db.save_variants(
            episode_id,
            "hook",
            [
                {"label": "A", "text": "A careful opening", "metrics": {"views": 100}},
                {"label": "B", "text": "A different honest opening"},
                {"label": "C", "text": "A third distinct opening"},
            ],
        )
        self.assertEqual(len(saved), 3)
        winner = self.db.set_winning_variant(episode_id, "hook", "B")
        self.assertEqual(winner["variant_label"], "B")
        self.assertTrue(winner["is_winner"])
        self.assertEqual(sum(item["is_winner"] for item in self.db.get_variants(episode_id, "hook")), 1)

    def test_upload_lock_prevents_duplicates_and_rejects_payload_reuse(self) -> None:
        first = self.db.acquire_upload_lock(
            self.workspace["workspace_id"], "youtube", "channel-123", "episode-1", "a" * 64
        )
        self.assertTrue(first.acquired)
        duplicate_during_upload = self.db.acquire_upload_lock(
            self.workspace["workspace_id"], "youtube", "channel-123", "episode-1", "a" * 64
        )
        self.assertFalse(duplicate_during_upload.acquired)
        self.assertEqual(duplicate_during_upload.reason, "in_progress")
        completed = self.db.finish_upload_lock(first.lock_token or "", success=True, remote_id="remote-1", result={"ok": True})
        self.assertEqual(completed["status"], "completed")
        duplicate_after_upload = self.db.acquire_upload_lock(
            self.workspace["workspace_id"], "youtube", "channel-123", "episode-1", "a" * 64
        )
        self.assertFalse(duplicate_after_upload.acquired)
        self.assertEqual(duplicate_after_upload.reason, "already_completed")
        self.assertEqual(duplicate_after_upload.remote_id, "remote-1")
        self.assertEqual(duplicate_after_upload.result, {"ok": True})
        with self.assertRaises(IdempotencyConflictError):
            self.db.acquire_upload_lock(
                self.workspace["workspace_id"], "youtube", "channel-123", "episode-1", "b" * 64
            )

    def test_status_transitions_are_checked(self) -> None:
        batch = self.db.create_saturday_batch(
            self.workspace["workspace_id"], self._seven_episodes(), batch_date="2026-11-21"
        )
        episode_id = batch["episodes"][0]["episode_id"]
        self.assertEqual(self.db.set_episode_status(episode_id, "approved")["status"], "approved")
        with self.assertRaises(ConflictError):
            self.db.set_episode_status(episode_id, "published")

    def test_memory_database_context_manager(self) -> None:
        with DatabaseManager(":memory:") as db:
            workspace = db.create_workspace("Temporary workspace")
            self.assertEqual(db.get_workspace(workspace["slug"])["name"], "Temporary workspace")


if __name__ == "__main__":
    unittest.main()
