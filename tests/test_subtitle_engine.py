from __future__ import annotations

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.subtitle_engine import SubtitleEngine, SubtitleError, WordTiming


class FakeBrain:
    def __init__(self, caption: str = "A careful caption for everyone") -> None:
        self.caption = caption
        self.calls: list[tuple[str, str]] = []

    def generate_captions(self, text: str, *, mode: str) -> dict[str, str]:
        self.calls.append((text, mode))
        return {"caption": self.caption, "mode": mode}


def make_wav(path: Path, seconds: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(struct.pack("<h", 3_000) * int(16_000 * seconds))


class SubtitleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.drive = Path(self.temporary.name) / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        config.ensure_drive_layout()
        self.brain = FakeBrain()
        self.engine = SubtitleEngine(brain=self.brain)  # type: ignore[arg-type]

    def test_safe_zone_scales_to_both_vertical_resolutions(self) -> None:
        self.assertEqual(self.engine.safe_zone_margin(1920), 380)
        self.assertGreaterEqual(self.engine.safe_zone_margin(2560), 380)
        self.assertEqual(self.engine.safe_zone_margin(2560), 507)
        self.assertEqual(self.engine.safe_zone_margin(1920, 450), 450)

    def test_word_estimates_are_deterministic_and_within_duration(self) -> None:
        first = self.engine.estimate_word_timings("This is an animation.", 2.0)
        second = self.engine.estimate_word_timings("This is an animation.", 2.0)
        self.assertEqual(first, second)
        self.assertEqual([item.word for item in first], ["This", "is", "an", "animation."])
        self.assertEqual(first[0].start, 0.0)
        self.assertAlmostEqual(first[-1].end, 2.0)
        self.assertTrue(all(0 <= item.start < item.end <= 2.0 for item in first))

    def test_whisper_cpp_token_json_is_combined_into_word_timings(self) -> None:
        payload = {
            "transcription": [
                {
                    "tokens": [
                        {"text": " Hello", "offsets": {"from": 0, "to": 400}},
                        {"text": " there", "offsets": {"from": 400, "to": 800}},
                        {"text": "!", "offsets": {"from": 800, "to": 900}},
                    ]
                }
            ]
        }
        words = self.engine._whisper_cpp_words(payload)
        self.assertEqual(len(words), 2)
        self.assertEqual(words[0].word, "Hello")
        self.assertEqual(words[1].word, "there!")
        self.assertAlmostEqual(words[1].end, 0.9)

    def test_missing_whisper_models_use_deterministic_audio_timing(self) -> None:
        audio = Path(self.temporary.name) / "dialogue.wav"
        make_wav(audio)
        words = self.engine.word_timings_from_audio(
            audio,
            "A deterministic fallback.",
            duration_seconds=2.5,
            prefer_whisper=False,
        )
        self.assertEqual([item.word for item in words], ["A", "deterministic", "fallback."])
        self.assertAlmostEqual(words[-1].end, 2.5)

    def test_whisper_timestamps_are_retagged_with_translated_caption_words(self) -> None:
        audio = Path(self.temporary.name) / "dialogue.wav"
        make_wav(audio)
        whisper_words = [WordTiming("Hello", 0.0, 0.4), WordTiming("there", 0.45, 0.9)]
        with patch.object(self.engine, "_whisper_cpp_timings", return_value=whisper_words):
            aligned = self.engine.word_timings_from_audio(
                audio, "Salaam dosto", duration_seconds=1.2, prefer_whisper=True,
            )
        self.assertEqual([item.word for item in aligned], ["Salaam", "dosto"])
        self.assertAlmostEqual(aligned[1].start, 0.45)

    def test_ass_has_two_layers_karaoke_bounce_and_no_green(self) -> None:
        timings = [WordTiming("Hello", 0.0, 0.55), WordTiming("friend", 0.6, 1.2)]
        ass = self.engine.build_ass(timings, resolution=(1080, 1920), duration_seconds=1.4)
        self.assertIn("Style: Glow", ass)
        self.assertIn("Style: Karaoke", ass)
        self.assertIn("Dialogue: 0,", ass)
        self.assertIn("Dialogue: 1,", ass)
        self.assertIn("\\kf", ass)
        self.assertIn("\\t(", ass)
        self.assertIn(",380,1", ass)
        self.assertNotIn("00FF00", ass.upper())
        self.assertNotIn("#00FF00", ass.upper())

    def test_ass_scales_safe_zone_and_rejects_unapproved_green_color(self) -> None:
        timings = [WordTiming("Safe", 0.0, 1.0)]
        ass = self.engine.build_ass(timings, resolution=(1440, 2560), duration_seconds=1.2)
        self.assertIn(",507,1", ass)
        with self.assertRaisesRegex(ValueError, "zero-green"):
            self.engine.build_ass(timings, base_color="#00FF00", duration_seconds=1.2)
        with self.assertRaisesRegex(ValueError, "approved"):
            self.engine.build_ass(timings, highlight_colors=("#00FF00",), duration_seconds=1.2)

    def test_srt_sidecar_exports_plain_text_and_both_files_to_drive(self) -> None:
        output_ass = config.resolve_drive_path("renders_archive", "captions.ass")
        output_srt = config.resolve_drive_path("renders_archive", "captions.srt")
        result = self.engine.export_sidecars(
            "Ignore this caption because the fake brain returns its own.",
            duration_seconds=3.0,
            word_timings=[
                WordTiming("A", 0.0, 0.7),
                WordTiming("careful", 0.75, 1.8),
                WordTiming("caption", 1.85, 2.8),
                WordTiming("everyone", 2.8, 3.0),
            ],
            output_ass=output_ass,
            output_srt=output_srt,
            language_mode="english",
        )
        self.assertTrue(result.ass_path.is_file())
        self.assertTrue(result.srt_path.is_file())
        self.assertEqual(result.caption_text, self.brain.caption)
        self.assertEqual(result.safe_margin_px, 380)
        srt = output_srt.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 -->", srt)
        self.assertIn("A careful caption for everyone", srt)
        self.assertNotIn("\\kf", srt)
        self.assertTrue(output_ass.is_relative_to(self.drive))

    def test_roman_urdu_and_hindi_modes_route_through_brain(self) -> None:
        engine = SubtitleEngine(brain=FakeBrain("Yeh ek caption hai"))  # type: ignore[arg-type]
        urdu = engine.caption_text("This is a caption.", language_mode="roman_urdu")
        hindi = engine.caption_text("This is a caption.", language_mode="roman_hindi")
        self.assertEqual(urdu, "Yeh ek caption hai")
        self.assertEqual(hindi, "Yeh ek caption hai")
        self.assertEqual([call[1] for call in engine.brain.calls], ["roman_urdu", "roman_hindi"])

    def test_srt_timing_mapping_supports_end_seconds(self) -> None:
        srt = self.engine.build_srt(
            [{"text": "Hi", "start_seconds": 0.0, "end_seconds": 0.7}],
            duration_seconds=1.0,
        )
        self.assertIn("00:00:00,000 --> 00:00:00,700", srt)
        self.assertTrue(srt.endswith("Hi\n"))

    def test_non_english_captions_fail_closed_if_brain_is_unavailable(self) -> None:
        class UnavailableBrain:
            def generate_captions(self, text: str, *, mode: str) -> dict[str, str]:
                from backend.brain_gemini import AIProviderUnavailable

                raise AIProviderUnavailable("offline")

        engine = SubtitleEngine(brain=UnavailableBrain())  # type: ignore[arg-type]
        with self.assertRaisesRegex(SubtitleError, "No AI provider"):
            engine.caption_text("Hello", language_mode="roman_urdu")
        self.assertEqual(engine.caption_text("Hello", language_mode="english"), "Hello")


if __name__ == "__main__":
    unittest.main()
