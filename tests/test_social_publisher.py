from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.social_publisher import (
    OAuthFlowError,
    OAuthStateStore,
    OAuthTokenVault,
    PlatformNotConfigured,
    SocialPublisher,
    SocialPublisherError,
)


class FakeResponse:
    def __init__(self, payload: dict | None = None, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.body = json.dumps(payload or {}).encode("utf-8")
        self.closed = False

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def read(self, _size: int = -1) -> bytes:
        return self.body


class FakeTransport:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []

    def __call__(self, request, _timeout: float) -> FakeResponse:
        headers = {str(key).casefold(): str(value) for key, value in request.header_items()}
        body = request.data or b""
        call = (request.full_url, request.get_method(), headers, body)
        self.calls.append(call)
        return self.handler(*call)


class SocialPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.drive = self.root / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        self.vault = OAuthTokenVault(self.root / ".daemoncut" / "oauth_tokens.json")
        self.video = self.drive / "renders_archive" / "episode.mp4"
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b"video-bytes-for-provider")
        self.cover = self.drive / "renders_archive" / "cover.jpg"
        self.cover.write_bytes(b"jpeg-cover")
        self.srt = self.drive / "renders_archive" / "captions.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nAn original caption.\n", encoding="utf-8")
        self.episode = {
            "episode_id": "episode-123",
            "title": "The Tiny Workshop Mystery",
            "topic": "A missing compass",
            "hook": "The compass points somewhere new.",
            "script": "Two friends discover an original clue and solve the mystery together.",
            "metadata": {
                "video_path": str(self.video),
                "cover_path": str(self.cover),
                "srt_path": str(self.srt),
                "caption": "An original short about teamwork.",
                "hashtags": ["#OriginalStories", "#Shorts"],
                "first_comment": "Which clue did you notice first?",
            },
        }

    def _publisher(self, handler=None, *, sleep=lambda _seconds: None) -> tuple[SocialPublisher, FakeTransport | None]:
        transport = FakeTransport(handler) if handler else None
        publisher = SocialPublisher(
            vault=self.vault,
            transport=transport,
            sleep=sleep,
            journal_path=self.root / ".daemoncut" / "publish_operations.json",
        )
        return publisher, transport

    def test_token_vault_encrypts_data_and_restricts_file_permissions(self) -> None:
        token = {"access_token": "not-for-git-secret", "refresh_token": "refresh-private", "expires_at": 0}
        self.vault.store("youtube", token)
        contents = self.vault.path.read_text(encoding="utf-8")
        self.assertNotIn("not-for-git-secret", contents)
        self.assertNotIn("refresh-private", contents)
        self.assertEqual(self.vault.get("youtube"), token)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.vault.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.vault.key_path.stat().st_mode), 0o600)
        self.vault.delete("youtube")
        self.assertIsNone(self.vault.get("youtube"))

    def test_oauth_state_is_single_use_and_tiktok_uses_pkce(self) -> None:
        state_store = OAuthStateStore()
        state = state_store.begin("tiktok", verifier="verifier")
        self.assertEqual(state_store.consume(state).verifier, "verifier")
        with self.assertRaises(OAuthFlowError):
            state_store.consume(state)
        with patch.object(config, "TIKTOK_CLIENT_KEY", "client-key"), patch.object(config, "TIKTOK_CLIENT_SECRET", "client-secret"):
            publisher, _ = self._publisher()
            result = publisher.authorization_url("tiktok")
        self.assertIn("code_challenge_method=S256", result["authorization_url"])
        self.assertIn("video.publish", result["authorization_url"])
        self.assertEqual(result["platform"], "tiktok")

    def test_youtube_and_tiktok_authorize_independently_of_unconfigured_meta(self) -> None:
        with (
            patch.object(config, "YOUTUBE_OAUTH_CLIENT_ID", "yt-client"),
            patch.object(config, "YOUTUBE_OAUTH_CLIENT_SECRET", "yt-secret"),
            patch.object(config, "TIKTOK_CLIENT_KEY", "tt-key"),
            patch.object(config, "TIKTOK_CLIENT_SECRET", "tt-secret"),
            patch.object(config, "META_APP_ID", ""),
            patch.object(config, "META_APP_SECRET", ""),
        ):
            publisher, _ = self._publisher()
            youtube = publisher.authorization_url("youtube")
            tiktok = publisher.authorization_url("tiktok")
            self.assertIn("accounts.google.com", youtube["authorization_url"])
            self.assertIn("tiktok.com", tiktok["authorization_url"])
            with self.assertRaises(PlatformNotConfigured):
                publisher.authorization_url("facebook")

    def test_youtube_oauth_code_is_exchanged_and_channel_is_discovered(self) -> None:
        def response(url, method, _headers, _body):
            if "oauth2.googleapis.com/token" in url:
                return FakeResponse({"access_token": "yt-access", "refresh_token": "yt-refresh", "expires_in": 3_600})
            if "youtube/v3/channels" in url:
                return FakeResponse({"items": [{"id": "UC-ORIGINAL", "snippet": {"title": "Studio"}}]})
            raise AssertionError(f"Unexpected provider call: {method} {url}")

        with (
            patch.object(config, "YOUTUBE_OAUTH_CLIENT_ID", "yt-client"),
            patch.object(config, "YOUTUBE_OAUTH_CLIENT_SECRET", "yt-secret"),
        ):
            publisher, transport = self._publisher(response)
            authorize = publisher.authorization_url("youtube")
            result = publisher.complete_oauth(state=authorize["state"], code="authorization-code")
        self.assertTrue(result["connected"])
        self.assertEqual(result["account_id"], "UC-ORIGINAL")
        stored = self.vault.get("youtube")
        self.assertEqual(stored["refresh_token"], "yt-refresh")
        self.assertEqual(stored["channel_id"], "UC-ORIGINAL")
        self.assertEqual(len(transport.calls), 2)

    def test_youtube_resumable_upload_thumbnail_captions_comment_and_idempotency(self) -> None:
        def response(url, method, _headers, body):
            if "/videos?uploadType=resumable" in url:
                return FakeResponse({}, headers={"Location": "https://upload.youtube.test/session/1"})
            if "upload.youtube.test/session" in url:
                self.assertEqual(method, "PUT")
                self.assertIn(b"video-bytes", body)
                return FakeResponse({"id": "yt-video-77"}, status=201)
            if "/thumbnails/set" in url:
                return FakeResponse({"items": []})
            if "/captions?part=snippet" in url:
                self.assertIn(b"An original caption.", body)
                return FakeResponse({"id": "caption-id"}, status=201)
            if "/commentThreads?part=snippet" in url:
                self.assertIn(b"Which clue did you notice first?", body)
                return FakeResponse({"id": "comment-id"}, status=201)
            raise AssertionError(f"Unexpected provider call: {method} {url}")

        self.vault.store("youtube", {"access_token": "yt-access"})
        with patch.object(config, "YOUTUBE_PRIVACY_STATUS", "private"):
            publisher, transport = self._publisher(response)
            first = publisher.publish_episode(self.episode, platforms=["youtube"], idempotency_key="weekly-episode-123")
            duplicate = publisher.publish_episode(self.episode, platforms=["youtube"], idempotency_key="weekly-episode-123")
        self.assertEqual(first[0]["status"], "published")
        self.assertEqual(first[0]["remote_id"], "yt-video-77")
        self.assertEqual(duplicate[0]["status"], "already_published")
        self.assertEqual(len(transport.calls), 5)

    def test_tiktok_file_upload_and_status_are_independent(self) -> None:
        def response(url, method, _headers, body):
            if url.endswith("/video/init/"):
                payload = json.loads(body)
                self.assertEqual(payload["post_info"]["privacy_level"], "PUBLIC_TO_EVERYONE")
                self.assertIn("#OriginalStories", payload["post_info"]["title"])
                return FakeResponse({"data": {"publish_id": "tt-publish-9", "upload_url": "https://upload.tiktokapis.com/session/9"}})
            if "upload.tiktokapis.com" in url:
                self.assertEqual(method, "PUT")
                self.assertTrue(body)
                return FakeResponse({}, status=201)
            if url.endswith("/status/fetch/"):
                return FakeResponse({"data": {"status": "PUBLISH_COMPLETE"}})
            raise AssertionError(f"Unexpected provider call: {method} {url}")

        self.vault.store("tiktok", {"access_token": "tt-access"})
        publisher, transport = self._publisher(response)
        result = publisher.publish_episode(self.episode, platforms=["tiktok"])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["remote_id"], "tt-publish-9")
        self.assertEqual(len(transport.calls), 3)

    def test_unconfigured_meta_exports_all_ready_to_post_assets(self) -> None:
        with (
            patch.object(config, "META_API_ENABLED", False),
            patch.object(config, "META_PAGE_ACCESS_TOKEN", ""),
            patch.object(config, "META_APP_ID", ""),
            patch.object(config, "META_APP_SECRET", ""),
        ):
            publisher, _ = self._publisher()
            results = publisher.publish_episode(self.episode, platforms=["facebook", "instagram"])
        self.assertEqual([item["status"] for item in results], ["ready_to_post", "ready_to_post"])
        bundle = self.drive / "renders_archive" / "ready_to_post" / "episode-123"
        self.assertTrue((bundle / "rendered.mp4").is_file())
        self.assertTrue((bundle / "cover.jpg").is_file())
        self.assertTrue((bundle / "captions.srt").is_file())
        post_copy = (bundle / "post_copy.txt").read_text(encoding="utf-8")
        self.assertIn("The Tiny Workshop Mystery", post_copy)
        self.assertIn("#OriginalStories", post_copy)
        self.assertIn("Which clue did you notice first?", post_copy)
        self.assertEqual((bundle / "rendered.mp4").read_bytes(), self.video.read_bytes())

    def test_facebook_page_reel_upload_does_not_depend_on_instagram_settings(self) -> None:
        def response(url, method, _headers, body):
            if url.endswith("/facebook-page/video_reels"):
                form = body.decode("utf-8")
                if "upload_phase=start" in form:
                    return FakeResponse({"video_id": "fb-video-1", "upload_url": "https://upload.facebook.com/video/1"})
                self.assertIn("upload_phase=finish", form)
                return FakeResponse({"video_id": "fb-video-1"})
            if "upload.facebook.com" in url:
                return FakeResponse({"start_offset": "0", "end_offset": str(len(body))})
            if url.endswith("/fb-video-1/comments"):
                return FakeResponse({"id": "fb-comment-1"})
            if url.endswith("/fb-video-1/thumbnails"):
                return FakeResponse({"data": []})
            raise AssertionError(f"Unexpected provider call: {method} {url}")

        with (
            patch.object(config, "META_API_ENABLED", True),
            patch.object(config, "META_PAGE_ACCESS_TOKEN", "page-access"),
            patch.object(config, "META_PAGE_ID", "facebook-page"),
            patch.object(config, "META_INSTAGRAM_USER_ID", ""),
        ):
            publisher, transport = self._publisher(response)
            result = publisher.publish_episode(self.episode, platforms=["facebook"])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["remote_id"], "fb-video-1")
        self.assertEqual(len(transport.calls), 5)

    def test_instagram_reel_uses_resumable_container_and_is_independent(self) -> None:
        def response(url, method, _headers, body):
            if url.endswith("/instagram-business/media"):
                self.assertIn(b"media_type=REELS", body)
                return FakeResponse({"id": "ig-container-1"})
            if "rupload.facebook.com" in url:
                return FakeResponse({"start_offset": "0", "end_offset": str(len(body))})
            if url.endswith("/ig-container-1?fields=status_code"):
                return FakeResponse({"status_code": "FINISHED"})
            if url.endswith("/instagram-business/media_publish"):
                return FakeResponse({"id": "ig-reel-1"})
            if url.endswith("/ig-reel-1/comments"):
                return FakeResponse({"id": "ig-comment-1"})
            raise AssertionError(f"Unexpected provider call: {method} {url}")

        with (
            patch.object(config, "META_API_ENABLED", True),
            patch.object(config, "META_PAGE_ACCESS_TOKEN", "page-access"),
            patch.object(config, "META_PAGE_ID", ""),
            patch.object(config, "META_INSTAGRAM_USER_ID", "instagram-business"),
        ):
            publisher, transport = self._publisher(response)
            result = publisher.publish_episode(self.episode, platforms=["instagram"])
        self.assertEqual(result[0]["status"], "published")
        self.assertEqual(result[0]["remote_id"], "ig-reel-1")
        self.assertEqual(len(transport.calls), 5)

    def test_metadata_paths_outside_drive_are_rejected(self) -> None:
        unsafe = {**self.episode, "metadata": {**self.episode["metadata"], "video_path": str(self.root / "outside.mp4")}}
        publisher, _ = self._publisher()
        with self.assertRaisesRegex(SocialPublisherError, "Google Drive"):
            publisher.export_ready_to_post_bundle(unsafe)


if __name__ == "__main__":
    unittest.main()
