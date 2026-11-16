from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import config
from backend.clipper_engine import HighlightSegment
from backend.database import DatabaseManager
from backend.director_chat import DictationBuffer
from backend.main import create_app, create_services
from backend.social_publisher import OAuthTokenVault, SocialPublisher


class MainAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.drive = root / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        self.database = DatabaseManager(":memory:")
        self.addCleanup(self.database.close)
        self.services = create_services(database=self.database)
        self.services.director_chat.dictation = DictationBuffer(state_path=root / "dictation.json")
        self.app = create_app(self.services, start_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_health_and_system_status_use_locked_palette(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "active")
        self.assertIn(health.json()["color"], config.UI_PALETTE.values())
        system = self.client.get("/api/system")
        self.assertEqual(system.status_code, 200)
        body = system.json()
        self.assertTrue(body["drive"]["mounted"])
        self.assertEqual(body["hardware"]["cpu_physical_cores"], 6)
        self.assertEqual(body["api"]["workers"], 1)
        self.assertTrue(set(body["palette"].values()).issubset(set(config.UI_PALETTE.values())))

    def test_workspaces_and_exactly_seven_episode_saturday_batch(self) -> None:
        created = self.client.post("/api/workspaces", json={"name": "API Test Studio", "slug": "api-test-studio"})
        self.assertEqual(created.status_code, 200, created.text)
        workspace_id = created.json()["workspace"]["workspace_id"]
        episodes = [{"topic": f"topic {i}", "title": f"episode {i}"} for i in range(1, 8)]
        batch_response = self.client.post(
            f"/api/workspaces/{workspace_id}/batches/saturday",
            json={"episodes": episodes, "batch_date": "2026-11-14"},
        )
        self.assertEqual(batch_response.status_code, 200, batch_response.text)
        batch = batch_response.json()["batch"]
        self.assertEqual(len(batch["episodes"]), 7)
        self.assertEqual(batch["target_count"], 7)
        invalid = self.client.post(
            f"/api/workspaces/{workspace_id}/batches/saturday",
            json={"episodes": episodes[:6], "batch_date": "2026-11-21"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("color", invalid.json()["detail"])

    def test_script_endpoint_keeps_reference_prompts_unlimited_and_scene_prompts_bounded(self) -> None:
        script = {
            "title": "Original",
            "hook": "A new twist",
            "script": "A fresh script",
            "description": "",
            "tags": [],
            "scenes": [{"video_prompt": "A scene within the prompt ceiling", "prompt_char_count": 32}],
        }
        references = {key: "reference " + ("detail " * 200) for key in ("E1", "E2", "E3", "E3B")}
        with patch.object(self.services.brain, "generate_script", return_value=script):
            with patch.object(self.services.brain, "generate_reference_prompts", return_value=references):
                response = self.client.post(
                    "/api/studio/scripts",
                    json={
                        "niche": "cartoon comedy",
                        "topic": "a paper airplane",
                        "include_reference_prompts": True,
                        "character_description": "Two new friends",
                        "environment_description": "A new room",
                    },
                )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()["script"]
        self.assertGreater(len(payload["reference_prompts"]["E1"]), config.MAX_YT_CREATE_CHARS)
        self.assertLessEqual(len(payload["scenes"][0]["video_prompt"]), config.MAX_YT_CREATE_CHARS)
        self.assertFalse(payload["prompt_limits"]["reference_image_prompts_capped"])

    def test_script_endpoint_rejects_video_prompt_over_880(self) -> None:
        script = {"scenes": [{"video_prompt": "x" * 881}]}
        with patch.object(self.services.brain, "generate_script", return_value=script):
            response = self.client.post("/api/studio/scripts", json={"niche": "custom", "topic": "test"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("880-character", response.json()["detail"]["message"])

    def test_dictation_pause_resume_endpoints_preserve_buffered_text(self) -> None:
        session = "mobile-session-1"
        append = self.client.post(f"/api/director/dictation/{session}/chunk", json={"text": "the first half"})
        self.assertEqual(append.json()["draft"]["text"], "the first half")
        paused = self.client.post(f"/api/director/dictation/{session}/pause")
        self.assertTrue(paused.json()["draft"]["paused"])
        denied = self.client.post(f"/api/director/dictation/{session}/chunk", json={"text": "not yet"})
        self.assertEqual(denied.status_code, 409)
        self.assertIn("paused", denied.json()["detail"]["message"])
        resumed = self.client.post(f"/api/director/dictation/{session}/resume")
        self.assertFalse(resumed.json()["draft"]["paused"])
        final = self.client.post(f"/api/director/dictation/{session}/chunk", json={"text": "the second half"})
        self.assertEqual(final.json()["draft"]["text"], "the first half the second half")
        finalized = self.client.post(f"/api/director/dictation/{session}/finalize")
        self.assertEqual(finalized.json()["text"], "the first half the second half")

    def test_bridge_page_has_embedded_qr_and_only_approved_palette_colors(self) -> None:
        token = self.app.state.bridge_token
        response = self.client.get(f"/bridge?token={token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<svg", response.text)
        self.assertIn("X-Bridge-Token", response.text)
        self.assertIn("Upload to Drive", response.text)
        used_colors = set(re.findall(r"#[0-9A-Fa-f]{6}", response.text))
        self.assertTrue(used_colors)
        self.assertTrue(used_colors.issubset(set(config.UI_PALETTE.values())))
        self.assertNotIn("#00ff00", response.text.casefold())
        self.assertEqual(self.client.get("/bridge?token=expired").status_code, 403)
        qr = self.client.get(f"/bridge/qr.svg?token={token}")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.headers["content-type"].split(";")[0], "image/svg+xml")

    def test_bridge_upload_streams_file_to_drive_and_requires_token(self) -> None:
        token = self.app.state.bridge_token
        unauthenticated = self.client.post("/api/bridge/upload", files={"file": ("clip.mp4", b"clip", "video/mp4")})
        self.assertEqual(unauthenticated.status_code, 403)
        unsupported = self.client.post(
            "/api/bridge/upload",
            headers={"X-Bridge-Token": token},
            files={"file": ("document.txt", b"not media", "text/plain")},
        )
        self.assertEqual(unsupported.status_code, 415)
        uploaded = self.client.post(
            "/api/bridge/upload",
            headers={"X-Bridge-Token": token},
            files={"file": ("my phone clip.mp4", b"phone-video-bytes", "video/mp4")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        payload = uploaded.json()
        self.assertEqual(payload["status"], "uploaded")
        self.assertIn(payload["color"], config.UI_PALETTE.values())
        destination = self.drive / "source_media" / "phone_uploads" / payload["stored_name"]
        self.assertEqual(destination.read_bytes(), b"phone-video-bytes")
        self.assertEqual(payload["size_bytes"], len(b"phone-video-bytes"))

    def test_bridge_upload_enforces_configured_file_size_limit(self) -> None:
        token = self.app.state.bridge_token
        with patch.object(config, "BRIDGE_UPLOAD_MAX_BYTES", 1_024):
            response = self.client.post(
                "/api/bridge/upload",
                headers={"X-Bridge-Token": token},
                files={"file": ("too-large.mp4", b"x" * 1_025, "video/mp4")},
            )
        self.assertEqual(response.status_code, 413, response.text)
        upload_dir = self.drive / "source_media" / "phone_uploads"
        if upload_dir.exists():
            self.assertEqual(list(upload_dir.iterdir()), [])

    def test_trend_route_passes_workspace_history_to_scout(self) -> None:
        workspace = self.database.create_workspace("Trend Studio", slug="trend-studio")
        with patch.object(self.services.trend_scout, "scout", return_value=[{"topic": "paper engineering"}]) as scout:
            response = self.client.get(f"/api/trends?workspace_id={workspace['workspace_id']}&limit=5")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["topic"], "paper engineering")
        self.assertEqual(scout.call_args.kwargs["recent_topics"], [])
        self.assertEqual(scout.call_args.kwargs["limit"], 5)

    def test_custom_niche_and_subscription_are_persisted_as_workspace_metadata(self) -> None:
        workspace = self.database.create_workspace("Niche Studio", slug="niche-studio")
        saved = self.client.post("/api/niches", json={
            "workspace_id": workspace["workspace_id"],
            "name": "Stoic Philosophy",
            "target_audience": "Curious adults",
            "channel_promise": "One practical idea, carefully explained",
            "tone": "Grounded and clear",
            "boundaries": ["Distinguish history from interpretation"],
            "blueprint": {"content_pillars": ["daily practice"]},
            "series_bible": {"visualCanon": "Original editorial graphics"},
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        niche_id = saved.json()["niche"]["id"]
        listed = self.client.get(f"/api/niches?workspace_id={workspace['workspace_id']}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["niches"][0]["name"], "Stoic Philosophy")
        subscribed = self.client.post("/api/niches/subscriptions", json={
            "workspace_id": workspace["workspace_id"], "niche_ids": [niche_id, "not-a-real-niche"]
        })
        self.assertEqual(subscribed.status_code, 200, subscribed.text)
        self.assertEqual(subscribed.json()["subscribed_ids"], [niche_id])
        self.assertEqual(self.client.get(f"/api/niches?workspace_id={workspace['workspace_id']}").json()["subscribed_ids"], [niche_id])

    def test_clipper_rejects_long_manual_thumbnail_hook_before_ingestion(self) -> None:
        response = self.client.post("/api/clipper/highlights", json={
            "video_path": str(self.drive / "source_media" / "sample.mp4"),
            "hook": "one two three four",
        })
        self.assertEqual(response.status_code, 422)
        self.assertIn("three words", response.json()["detail"]["message"])

    def test_clipper_renders_only_selected_highlight_indices(self) -> None:
        media_path = self.drive / "source_media" / "sample.mp4"
        candidates = [
            HighlightSegment(0, 20, "first", 0.1, 0.1, 0.1),
            HighlightSegment(20, 40, "second", 0.8, 0.8, 0.8),
            HighlightSegment(40, 60, "third", 0.3, 0.3, 0.3),
        ]
        with patch.object(self.services.clipper, "ingest_local_file", return_value=SimpleNamespace(path=media_path)):
            with patch.object(self.services.clipper, "find_highlights", return_value=candidates):
                with patch.object(self.services.clipper, "render_vertical_short", return_value={"rendered": True}) as render:
                    response = self.client.post("/api/clipper/highlights", json={
                        "video_path": str(media_path),
                        "render": True,
                        "selected_highlight_indices": [1],
                        "framing_mode": "full_screen",
                        "language_mode": "roman_urdu",
                    })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["highlights"]), 3)
        self.assertEqual(len(response.json()["renders"]), 1)
        self.assertEqual(render.call_args.args[1], candidates[1])

    def test_publish_status_and_oauth_authorization_keep_platforms_independent(self) -> None:
        with patch.object(config, "YOUTUBE_OAUTH_CLIENT_ID", "youtube-client"), patch.object(config, "YOUTUBE_OAUTH_CLIENT_SECRET", "youtube-secret"):
            youtube = self.client.get("/api/oauth/youtube/authorize")
            tiktok = self.client.get("/api/oauth/tiktok/authorize")
        self.assertEqual(youtube.status_code, 200, youtube.text)
        self.assertIn("accounts.google.com", youtube.json()["authorization_url"])
        self.assertEqual(tiktok.status_code, 503)
        statuses = self.client.get("/api/publish/status")
        self.assertEqual(statuses.status_code, 200)
        platforms = statuses.json()["platforms"]
        self.assertEqual(set(platforms), {"youtube", "tiktok", "facebook", "instagram"})
        self.assertIn(platforms["facebook"]["status"], {"ready_to_post", "needs_authorization", "needs_configuration"})

    def test_settings_get_only_returns_secret_presence_and_update_is_local(self) -> None:
        with patch.dict("os.environ", {"GEMINI_API_KEY": "never-return-this-value"}):
            response = self.client.get("/api/settings/env")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("never-return-this-value", response.text)
        self.assertTrue(response.json()["values"]["GEMINI_API_KEY"]["configured"])
        blocked = self.client.put(
            "/api/settings/env",
            headers={"Origin": "https://attacker.invalid"},
            json={"updates": {"GEMINI_API_KEY": "blocked"}},
        )
        self.assertEqual(blocked.status_code, 403)

    def test_unconfigured_meta_publish_returns_ready_to_post_bundle(self) -> None:
        video = self.drive / "renders_archive" / "episode.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"rendered-mp4")
        episode = self.database.create_episode(
            self.database.create_workspace("Publisher Studio", slug="publisher-studio")["workspace_id"],
            topic="A tiny compass mystery",
            title="A New Direction",
            script="Two friends solve an original problem together.",
            metadata={"video_path": str(video)},
        )
        publisher = SocialPublisher(
            vault=OAuthTokenVault(Path(self.temporary.name) / "oauth_tokens.json"),
            journal_path=Path(self.temporary.name) / "publish_journal.json",
        )
        self.services.social_publisher = publisher
        with patch.object(config, "META_API_ENABLED", False), patch.object(config, "META_PAGE_ACCESS_TOKEN", ""):
            response = self.client.post(
                f"/api/publish/episode/{episode['episode_id']}",
                json={"platforms": ["facebook", "instagram"]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["status"] for item in response.json()["results"]], ["ready_to_post", "ready_to_post"])
        bundle = self.drive / "renders_archive" / "ready_to_post" / episode["episode_id"]
        self.assertTrue((bundle / "rendered.mp4").is_file())
        self.assertTrue((bundle / "cover.jpg").is_file())
        self.assertTrue((bundle / "captions.srt").is_file())
        self.assertTrue((bundle / "post_copy.txt").is_file())

    def test_scheduler_api_runs_without_unbounded_network_wait(self) -> None:
        response = self.client.post("/api/scheduler/run", json={"wait_for_connectivity": False, "cleanup_scratch": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["due_episode_count"], 0)
        self.assertEqual(self.client.get("/api/scheduler/status").status_code, 200)

    def test_websocket_streams_ring_buffer_activity(self) -> None:
        with self.client.websocket_connect("/ws/logs") as websocket:
            self.app.state.log_hub.emit("phase3 websocket test event")
            received = False
            for _ in range(210):
                message = websocket.receive_json()
                if message.get("message") == "phase3 websocket test event":
                    self.assertIn(message["color"], config.UI_PALETTE.values())
                    received = True
                    break
            self.assertTrue(received)


if __name__ == "__main__":
    unittest.main()
