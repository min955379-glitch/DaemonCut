from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.brain_gemini import AIProviderUnavailable
from backend.browser_cloner import (
    BrowserCloner,
    BrowserClonerValidationError,
    NarrativeStructure,
    validate_public_url,
)


class _YDL:
    def __init__(self, options: dict[str, object], factory: "_YDLFactory") -> None:
        self.options = options
        self.factory = factory

    def __enter__(self) -> "_YDL":
        self.factory.options_seen.append(self.options)
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
        if download:
            folder = Path(str(self.options["outtmpl"])).parent
            (folder / "source.en.vtt").write_text(self.factory.vtt, encoding="utf-8")
        return dict(self.factory.info)


class _YDLFactory:
    def __init__(self) -> None:
        self.options_seen: list[dict[str, object]] = []
        self.info: dict[str, object] = {
            "id": "vid-1",
            "title": "Public example",
            "description": "A short public video.",
            "duration": 24,
            "uploader": "Creator",
            "extractor_key": "Youtube",
            "formats": [{"url": "private format details"}],
        }
        self.vtt = """WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA new beginning\n\n00:00:01.900 --> 00:00:03.000\nA new beginning\n\n00:00:03.000 --> 00:00:05.000\n<v Creator>What happens next?</v>\n"""

    def __call__(self, options: dict[str, object]) -> _YDL:
        return _YDL(options, self)


class _FallbackBrain:
    def __init__(self) -> None:
        self.request: dict[str, object] | None = None

    def generate_script(self, **kwargs: object) -> dict[str, object]:
        self.request = kwargs
        raise AIProviderUnavailable("offline")

    @staticmethod
    def enforce_video_scene_prompt_limit(prompt: str, *, max_retries: int = 0) -> str:
        del max_retries
        return prompt[: config.MAX_YT_CREATE_CHARS]


class _Response:
    status = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b"""<html><head><title>Fallback title</title><meta name='description' content='Page summary'></head>
        <body><main>Visible creator text</main><script>secret javascript text</script></body></html>"""


class _Opener:
    def open(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response()


class BrowserClonerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        runtime = Path(self.temporary.name) / ".daemoncut"
        (runtime / "tmp").mkdir(parents=True)
        self.runtime_patches = [
            patch.object(config, "RUNTIME_DIR", runtime),
            patch.object(config, "TEMP_DIR", runtime / "tmp"),
        ]
        for item in self.runtime_patches:
            item.start()
            self.addCleanup(item.stop)

    def test_url_allowlist_and_credentials_are_checked(self) -> None:
        self.assertEqual(validate_public_url("https://www.tiktok.com/@creator/video/123"), "https://www.tiktok.com/@creator/video/123")
        self.assertEqual(validate_public_url("https://instagram.com/reel/abc"), "https://instagram.com/reel/abc")
        for url in (
            "file:///etc/passwd",
            "http://localhost/",
            "https://creator.example/video",
            "https://youtube.com.attacker.example/shorts/1",
            "https://name:password@youtube.com/watch?v=1",
            "https://youtube.com:9000/watch?v=1",
            "https://youtube.com:not-a-port/watch?v=1",
        ):
            with self.subTest(url=url), self.assertRaises(BrowserClonerValidationError):
                validate_public_url(url)

    def test_vtt_parser_handles_timestamps_tags_and_duplicate_captions(self) -> None:
        parsed = BrowserCloner.parse_vtt(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n<c.yellow>Hello &amp; welcome</c>\n"
            "\n00:00:00.950 --> 00:00:02.000\n<c.yellow>Hello &amp; welcome</c>\n"
            "\n00:00:02.100 --> 00:00:03.000\nA new caption\n"
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].text, "Hello & welcome")
        self.assertEqual(parsed[0].end_seconds, 2.0)
        self.assertEqual(parsed[1].start_seconds, 2.1)

    def test_ytdlp_metadata_and_captions_are_extracted_to_temp_not_returned_as_media(self) -> None:
        factory = _YDLFactory()
        cloner = BrowserCloner(ydl_factory=factory)
        with patch.object(cloner, "_inspect_urllib", return_value=None):
            summary = cloner.inspect_url("https://www.youtube.com/shorts/abc", inspect_dom=False)
        self.assertEqual(summary.title, "Public example")
        self.assertEqual(summary.creator, "Creator")
        self.assertEqual(summary.duration_seconds, 24)
        self.assertEqual(len(summary.transcript_segments), 2)
        self.assertNotIn("formats", summary.metadata)
        self.assertTrue(factory.options_seen[0]["ignoreconfig"])
        self.assertTrue(factory.options_seen[0]["skip_download"])
        self.assertTrue(factory.options_seen[0]["writesubtitles"])
        self.assertTrue(Path(config.TEMP_DIR).is_dir())

    def test_urllib_fallback_extracts_only_visible_page_metadata(self) -> None:
        cloner = BrowserCloner(ydl_factory=lambda _options: (_ for _ in ()).throw(RuntimeError("offline")))
        with patch("backend.browser_cloner.urllib.request.build_opener", return_value=_Opener()):
            page = cloner._inspect_urllib("https://www.tiktok.com/@creator")
        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page[0], "Fallback title")
        self.assertEqual(page[1], "Page summary")
        self.assertIn("Visible creator text", page[2])
        self.assertNotIn("secret javascript text", page[2])

    def test_structure_is_abstract_and_contains_no_source_dialogue(self) -> None:
        factory = _YDLFactory()
        cloner = BrowserCloner(ydl_factory=factory)
        with patch.object(cloner, "_inspect_urllib", return_value=None):
            source = cloner.inspect_url("https://www.youtube.com/shorts/abc", inspect_dom=False)
        structure = cloner.analyze_structure(source)
        self.assertIsInstance(structure, NarrativeStructure)
        self.assertIn(structure.hook_pattern, {"question", "surprise", "direct_address", "curiosity_or_visual_claim"})
        self.assertIn(structure.pacing, {"fast", "measured", "varied"})
        self.assertEqual(len(structure.beats), 5)
        self.assertTrue(all("transcript_hint" not in beat for beat in structure.beats))

    def test_clone_story_uses_only_abstract_structure_and_fallback_is_original(self) -> None:
        factory = _YDLFactory()
        brain = _FallbackBrain()
        cloner = BrowserCloner(brain=brain, ydl_factory=factory)
        with patch.object(cloner, "_inspect_urllib", return_value=None):
            result = cloner.clone_story(
                "https://www.youtube.com/shorts/abc",
                niche="craft animation",
                duration_seconds=45,
                scene_count=5,
                inspect_dom=False,
            )
        self.assertTrue(result.original_content_only)
        self.assertTrue(result.script["originality_guard"])
        self.assertEqual(len(result.script["scenes"]), 5)
        self.assertTrue(all(len(scene["video_prompt"]) <= config.MAX_YT_CREATE_CHARS for scene in result.script["scenes"]))
        captured_context = brain.request["workspace_context"] if brain.request else {}
        serialized = str(captured_context)
        self.assertIn("hook_pattern", serialized)
        self.assertNotIn("A new beginning", serialized)
        self.assertNotIn("What happens next", serialized)
        self.assertNotIn("Public example", serialized)
        self.assertIn("reference_structure", result.script)

    def test_vtt_parser_caps_segment_count(self) -> None:
        payload = "WEBVTT\n\n" + "\n\n".join(
            f"00:00:{index:02d}.000 --> 00:00:{index + 1:02d}.000\nCaption {index}"
            for index in range(30)
        )
        self.assertEqual(len(BrowserCloner.parse_vtt(payload, max_segments=5)), 5)


if __name__ == "__main__":
    unittest.main()
