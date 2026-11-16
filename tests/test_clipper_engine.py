from __future__ import annotations

import io
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import config
from backend.clipper_engine import (
    ClipperEngine,
    ClipperValidationError,
    HighlightSegment,
    RMSWindow,
    validate_public_video_url,
)
from backend.video_editor import RenderResult


class _FakeVideoEditor:
    def __init__(self, duration: float = 120.0) -> None:
        self.duration = duration
        self.render_calls: list[tuple[object, dict[str, object]]] = []
        self.render_result: RenderResult | None = None

    def probe_duration(self, _path: str | Path) -> float:
        return self.duration

    def render(self, clips: object, **kwargs: object) -> RenderResult:
        self.render_calls.append((clips, kwargs))
        assert self.render_result is not None
        return self.render_result


class _FakeYDL:
    def __init__(self, options: dict[str, object], owner: "_FakeYDLFactory") -> None:
        self.options = options
        self.owner = owner

    def __enter__(self) -> "_FakeYDL":
        self.owner.options_seen.append(self.options)
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
        if not download:
            return dict(self.owner.info)
        template = str(self.options["outtmpl"])
        output = Path(template.replace("%(extractor_key)s", "youtube").replace("%(id)s", "publicid").replace("%(ext)s", "mp4"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake video")
        self.owner.output = output
        return {"requested_downloads": [{"filepath": str(output)}]}

    def prepare_filename(self, _info: object) -> str:
        return str(self.owner.output)


class _FakeYDLFactory:
    def __init__(self, info: dict[str, object] | None = None) -> None:
        self.info = info or {"id": "publicid", "title": "Public VOD", "duration": 120, "extractor_key": "Youtube"}
        self.options_seen: list[dict[str, object]] = []
        self.output = Path("unused.mp4")

    def __call__(self, options: dict[str, object]) -> _FakeYDL:
        return _FakeYDL(options, self)


class ClipperEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.drive = root / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        runtime = root / ".daemoncut"
        for name in ("tmp", "cache", "logs"):
            (runtime / name).mkdir(parents=True, exist_ok=True)
        self.runtime_patch = patch.object(config, "RUNTIME_DIR", runtime), patch.object(config, "TEMP_DIR", runtime / "tmp")
        for item in self.runtime_patch:
            item.start()
            self.addCleanup(item.stop)
        self.video = _FakeVideoEditor()
        self.engine = ClipperEngine(video_editor=self.video, ydl_factory=_FakeYDLFactory())

    def test_public_url_allowlist_blocks_local_and_untrusted_hosts(self) -> None:
        self.assertEqual(validate_public_video_url("https://youtu.be/abc"), "https://youtu.be/abc")
        self.assertEqual(validate_public_video_url("https://www.twitch.tv/channel/videos/123"), "https://www.twitch.tv/channel/videos/123")
        for url in (
            "file:///tmp/a.mp4",
            "http://127.0.0.1/video.mp4",
            "https://attacker.example/video",
            "https://user:pass@youtube.com/watch?v=1",
            "https://youtube.com:8443/watch?v=1",
            "https://youtube.com:not-a-port/watch?v=1",
        ):
            with self.subTest(url=url), self.assertRaises(ClipperValidationError):
                validate_public_video_url(url)

    def test_ytdlp_download_is_bounded_and_archived_in_drive(self) -> None:
        factory = _FakeYDLFactory()
        engine = ClipperEngine(video_editor=self.video, ydl_factory=factory)
        media = engine.ingest_url(
            "https://www.youtube.com/watch?v=abc",
            start_seconds=30,
            duration_seconds=45,
            workspace_id="workspace-1",
        )
        self.assertTrue(media.path.is_file())
        self.assertTrue(media.path.is_relative_to(self.drive))
        self.assertEqual(media.duration_seconds, 45)
        self.assertEqual(media.source_kind, "vod")
        self.assertTrue(factory.options_seen[0]["skip_download"])
        download_options = factory.options_seen[1]
        self.assertFalse(download_options["skip_download"])
        self.assertEqual(download_options["concurrent_fragment_downloads"], config.YT_DLP_FRAGMENT_WORKERS)
        self.assertEqual(download_options["max_filesize"], config.YT_DLP_MAX_FILESIZE_BYTES)
        self.assertIn("download_ranges", download_options)
        self.assertTrue(download_options["force_keyframes_at_cuts"])
        self.assertIn("workspaces", str(media.path))
        self.assertNotIn("formats", media.metadata)

    def test_live_stream_requires_a_bounded_capture_duration(self) -> None:
        factory = _FakeYDLFactory({"id": "live", "title": "Live", "duration": None, "is_live": True})
        engine = ClipperEngine(video_editor=self.video, ydl_factory=factory)
        with self.assertRaisesRegex(ClipperValidationError, "explicit duration"):
            engine.ingest_url("https://www.twitch.tv/channel")
        media = engine.ingest_url("https://www.twitch.tv/channel", start_seconds=10, duration_seconds=30)
        self.assertEqual(media.duration_seconds, 30)
        self.assertEqual(media.source_kind, "live_capture")

    def test_rejects_vod_over_duration_cap_before_download(self) -> None:
        factory = _FakeYDLFactory({"id": "long", "title": "Long", "duration": 999})
        engine = ClipperEngine(video_editor=self.video, ydl_factory=factory)
        with patch.object(config, "YT_DLP_MAX_DURATION_SECONDS", 100):
            with self.assertRaisesRegex(ClipperValidationError, "maximum"):
                engine.ingest_url("https://www.youtube.com/watch?v=long")
        self.assertEqual(len(factory.options_seen), 1)

    def test_local_file_is_copied_to_drive_without_touching_source(self) -> None:
        source = Path(self.temporary.name) / "creator-recording.mp4"
        source.write_bytes(b"local media")
        media = self.engine.ingest_local_file(source, title="Local test")
        self.assertEqual(source.read_bytes(), b"local media")
        self.assertTrue(media.path.is_relative_to(self.drive))
        self.assertNotEqual(media.path, source)
        self.assertEqual(media.source_kind, "local_file")
        self.assertEqual(media.title, "Local test")

    def test_rms_windows_and_ffmpeg_command_are_bounded(self) -> None:
        pcm = struct.pack("<hhhh", 0, 32767, 0, -32768)
        windows = ClipperEngine.rms_windows(pcm, sample_rate=2, window_seconds=1)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].start_seconds, 0)
        self.assertEqual(windows[1].end_seconds, 2)
        self.assertAlmostEqual(windows[0].rms, 32767 / (2 ** 0.5) / 32768, places=5)
        source = self.drive / "source_media" / "vods" / "x.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        command = ClipperEngine.build_audio_energy_command(source, ffmpeg="ffmpeg", sample_rate=8_000)
        self.assertIn("-filter_threads", command)
        self.assertIn(str(config.FFMPEG_FILTER_THREADS), command)
        self.assertIn("s16le", command)
        self.assertEqual(command[-1], "pipe:1")

    def test_audio_analysis_streams_pcm_instead_of_capture_output(self) -> None:
        source = self.drive / "source_media" / "vods" / "stream.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
        self.video.duration = 2
        pcm = struct.pack("<h", 16_000) * 16_000

        class _Process:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(pcm)
                self.returncode = 0

            def wait(self, timeout: int) -> int:
                self.timeout = timeout
                return 0

            def kill(self) -> None:
                self.returncode = -9

        process = _Process()
        with patch("backend.clipper_engine.subprocess.Popen", return_value=process) as popen:
            windows = self.engine.analyze_audio(source, window_seconds=0.5)
        self.assertEqual(len(windows), 4)
        self.assertAlmostEqual(windows[0].rms, 16_000 / 32768, places=5)
        self.assertGreaterEqual(process.timeout, 120)
        self.assertFalse(popen.call_args.kwargs["stderr"] is subprocess.PIPE)

    def test_highlights_combine_audio_and_transcript_hooks_offline(self) -> None:
        source = self.drive / "source_media" / "vods" / "long.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        self.video.duration = 90
        energy = [RMSWindow(float(index), float(index + 1), 0.2, -12.0 if 25 <= index < 40 else -40.0) for index in range(90)]
        with patch.object(self.engine, "analyze_audio", return_value=energy):
            highlights = self.engine.find_highlights(
                source,
                transcript_segments=[
                    {"start": 26, "end": 29, "text": "Wait, what happened? This is the unexpected answer!"},
                    {"start": 62, "end": 68, "text": "How did the clever repair finally work?"},
                ],
                window_seconds=15,
                step_seconds=10,
                max_highlights=2,
                use_brain=False,
            )
        self.assertEqual(len(highlights), 2)
        self.assertTrue(all(15 <= item.duration_seconds <= 60 for item in highlights))
        self.assertGreater(highlights[0].audio_score, 0)
        self.assertGreater(highlights[0].hook_score, 0)
        self.assertTrue(all(left.end_seconds <= right.start_seconds or right.end_seconds <= left.start_seconds for index, left in enumerate(highlights) for right in highlights[index + 1:]))
        self.assertLessEqual(ClipperEngine.suggest_hook(highlights[0].transcript).count(" "), 2)

    def test_highlight_window_count_is_capped(self) -> None:
        source = self.drive / "source_media" / "vods" / "large.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
        self.video.duration = config.YT_DLP_MAX_DURATION_SECONDS
        with self.assertRaisesRegex(ClipperValidationError, "5000"):
            self.engine.find_highlights(source, window_seconds=15, step_seconds=1, use_brain=False)

    def test_vertical_short_connects_video_subtitles_and_thumbnail_engines(self) -> None:
        source = self.drive / "source_media" / "vods" / "source.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
        archive = config.resolve_drive_path("renders_archive", "short.mp4")
        self.video.render_result = RenderResult(archive, 20, (1080, 1920), 60, "h264_amf", False)
        subtitle = SimpleNamespace(ass_path=Path("captions.ass"), srt_path=Path("captions.srt"))
        thumbnail = SimpleNamespace(cover_path=Path("cover.jpg"), thumbnail_path=Path("thumbnail.jpg"))
        self.engine.subtitle_engine.export_sidecars = lambda *_args, **_kwargs: subtitle
        self.engine.thumbnail_maker.make_from_video = lambda *_args, **_kwargs: thumbnail
        highlight = HighlightSegment(5, 25, "A fresh ending!", 75, 80, 77)
        result = self.engine.render_vertical_short(source, highlight, framing_mode="full_screen", center_x=0.6)
        self.assertEqual(result.render.path, archive)
        self.assertIs(result.subtitles, subtitle)
        self.assertIs(result.thumbnails, thumbnail)
        clips, options = self.video.render_calls[0]
        self.assertEqual(options["resolution"], (1080, 1920))
        self.assertEqual(clips[0].framing_mode, "full_screen")
        self.assertEqual(clips[0].center_x, 0.6)
        self.assertTrue(clips[0].include_audio)


if __name__ == "__main__":
    unittest.main()
