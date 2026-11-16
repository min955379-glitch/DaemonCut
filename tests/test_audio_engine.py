from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.audio_engine import (
    AudioAssetValidationError,
    AudioDependencyUnavailable,
    AudioEngine,
    AudioRightsError,
    SceneAudioMode,
    SfxEvent,
    VoiceMode,
)


def write_wav(path: Path, *, seconds: float = 2.0, rate: int = 48_000, sample: int = 5_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack("<h", sample) * frames)


class AudioEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.drive = Path(self.temporary.name) / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        config.ensure_drive_layout()
        self.engine = AudioEngine()

    def test_series_characters_resolve_character_driven_voice_presets(self) -> None:
        breezy = self.engine.resolve_voice_profile(character="Breezy")
        mal = self.engine.resolve_voice_profile(character="Mal")
        self.assertEqual(breezy.profile_id, "energetic_teen")
        self.assertEqual(breezy.edge_voice, "en-US-AvaMultilingualNeural")
        self.assertEqual(mal.profile_id, "calm_builder")
        self.assertEqual(mal.rate, "-4%")
        self.assertEqual(self.engine.resolve_voice_profile(profile_id="soft_young_boy").pitch, "+3Hz")

    def test_cloned_voice_sample_is_drive_resolved_and_validated(self) -> None:
        sample = config.resolve_drive_path("voice_vault", "my_voice.wav")
        write_wav(sample, seconds=3.0)
        resolved, metadata = self.engine._validate_voice_reference()
        self.assertEqual(resolved, sample.resolve())
        self.assertEqual(metadata.sample_rate, 48_000)
        self.assertAlmostEqual(metadata.duration_seconds, 3.0)
        self.assertFalse(metadata.silent)

    def test_clone_sample_rejects_silence_and_outside_drive_paths(self) -> None:
        silent = config.resolve_drive_path("voice_vault", "silent.wav")
        write_wav(silent, sample=0)
        with self.assertRaisesRegex(AudioAssetValidationError, "silent"):
            self.engine._validate_voice_reference(silent)
        outside = Path(self.temporary.name) / "outside.wav"
        write_wav(outside)
        with self.assertRaisesRegex(AudioAssetValidationError, "voice_vault"):
            self.engine._validate_voice_reference(outside)

    def test_scene_audio_keep_and_replace_modes_are_explicit(self) -> None:
        keep = self.engine.decide_scene_audio(SceneAudioMode.KEEP_ORIGINAL, original_audio_present=True)
        self.assertTrue(keep.keep_original_audio)
        self.assertFalse(keep.replace_dialogue)
        with self.assertRaisesRegex(ValueError, "requires"):
            self.engine.decide_scene_audio("REPLACE_VOICE", original_audio_present=True)
        replace = self.engine.decide_scene_audio(
            "REPLACE_VOICE",
            original_audio_present=True,
            replacement_dialogue_present=True,
            preserve_ambience=True,
        )
        self.assertFalse(replace.keep_original_audio)
        self.assertTrue(replace.replace_dialogue)
        self.assertTrue(replace.preserve_ambience)

    def test_manual_file_override_must_be_drive_or_scratch_backed(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            manual = Path(other) / "manual.wav"
            write_wav(manual)
            with self.assertRaisesRegex(AudioAssetValidationError, "Google Drive"):
                self.engine.synthesize(
                    "Hello there.",
                    mode=VoiceMode.MANUAL_OVERRIDE,
                    manual_audio_path=manual,
                    output_path=config.resolve_drive_path("voice_vault", "override.wav"),
                )

    def test_manual_drive_file_and_explicit_voice_id_overrides(self) -> None:
        manual = config.resolve_drive_path("voice_vault", "manual.wav")
        output = config.resolve_drive_path("voice_vault", "manual_copy.wav")
        write_wav(manual)
        with patch.object(self.engine, "_transcode_audio", side_effect=lambda _src, dst: write_wav(dst)):
            file_result = self.engine.synthesize(
                "Use this recording.", mode="manual_override", manual_audio_path=manual, output_path=output,
            )
        self.assertEqual(file_result.provider, "manual_audio")
        self.assertIsNone(file_result.voice_id)

        voice_output = config.resolve_drive_path("voice_vault", "manual_voice_id.wav")
        calls: list[str | None] = []

        def fake_edge(text: str, target: Path, profile: object, *, voice_id: str | None = None) -> tuple[str, str]:
            calls.append(voice_id)
            write_wav(target)
            return "edge_tts", voice_id or ""

        with patch.object(self.engine, "_synthesize_edge", side_effect=fake_edge):
            voice_result = self.engine.synthesize(
                "Use a selected voice.", mode="manual_override", voice_id="custom-neural-voice", output_path=voice_output,
            )
        self.assertEqual(calls, ["custom-neural-voice"])
        self.assertEqual(voice_result.voice_id, "custom-neural-voice")

    def test_tts_model_cache_is_resolved_under_drive(self) -> None:
        cache = self.engine._set_drive_model_cache()
        self.assertTrue(cache.is_relative_to(self.drive))
        self.assertIn("models", cache.parts)
        self.assertIn("tts", cache.parts)

    def test_cloning_unavailable_falls_back_to_auto_cast_with_warning(self) -> None:
        reference = config.resolve_drive_path("voice_vault", "my_voice.wav")
        write_wav(reference, seconds=2.0)
        destination = config.resolve_drive_path("voice_vault", "fallback.wav")

        def fake_auto(text: str, target: Path, profile: object, **kwargs: object) -> tuple[str, str, list[str]]:
            write_wav(target, seconds=1.5)
            return "edge_tts", "en-US-AvaMultilingualNeural", []

        with patch.object(self.engine, "_try_clone", side_effect=AudioDependencyUnavailable("local model missing")):
            with patch.object(self.engine, "_auto_cast", side_effect=fake_auto):
                result = self.engine.synthesize(
                    "A local fallback test.",
                    mode=VoiceMode.CLONED_VOICE,
                    character="Breezy",
                    output_path=destination,
                )
        self.assertEqual(result.provider, "edge_tts")
        self.assertFalse(result.voice_cloned)
        self.assertTrue(any("fallback" in warning.casefold() for warning in result.warnings))
        self.assertEqual(result.path, destination)

    def test_bgm_and_sfx_need_drive_assets_and_license_manifest(self) -> None:
        bgm = config.asset_directory("bgm") / "loop.wav"
        sfx = config.asset_directory("sfx") / "ding.wav"
        write_wav(bgm, seconds=3.0, sample=2_000)
        write_wav(sfx, seconds=0.3, sample=1_000)
        manifest = {
            "license": "royalty_free",
            "source": "Original recording by the user",
            "commercial_use": True,
            "rights_confirmed_by_user": True,
            "attribution": "",
            "loop_safe": True,
        }
        bgm.with_suffix(".wav.license.json").write_text(json.dumps(manifest), encoding="utf-8")
        sfx.with_suffix(".wav.license.json").write_text(json.dumps(manifest), encoding="utf-8")
        audit = self.engine.inspect_audio_asset(bgm, kind="bgm")
        self.assertTrue(audit.publish_safe)
        self.assertTrue(audit.loop_safe)
        dialogue = config.resolve_drive_path("voice_vault", "dialogue.wav")
        write_wav(dialogue, seconds=2.0)
        destination = config.resolve_drive_path("renders_archive", "master.wav")
        with patch.object(self.engine, "_ffmpeg", return_value="ffmpeg"):
            with patch.object(self.engine, "_run_process") as run:
                result = self.engine.master_audio(
                    dialogue,
                    output_path=destination,
                    bgm_path=bgm,
                    sfx_events=[SfxEvent(sfx, 0.5, -7.0)],
                    duration_seconds=2.0,
                )
        command = list(run.call_args.args[0])
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("sidechaincompress", filter_graph)
        self.assertIn("adelay=500|500", filter_graph)
        self.assertIn("loudnorm=I=-14.0", filter_graph)
        self.assertIn("aresample=48000", filter_graph)
        self.assertEqual(result.channels, 2)
        self.assertEqual(result.sample_rate, 48_000)
        self.assertEqual(len(result.rights_audits), 2)

    def test_missing_license_sidecar_blocks_bgm_publish(self) -> None:
        bgm = config.asset_directory("bgm") / "unlicensed.wav"
        write_wav(bgm, seconds=2.0)
        dialogue = config.resolve_drive_path("voice_vault", "dialogue.wav")
        write_wav(dialogue, seconds=2.0)
        with patch.object(self.engine, "_ffmpeg", return_value="ffmpeg"):
            with self.assertRaises(AudioRightsError):
                self.engine.master_audio(dialogue, bgm_path=bgm, duration_seconds=2.0)

    def test_final_master_uses_bounded_ffmpeg_filters_and_drive_output(self) -> None:
        dialogue = config.resolve_drive_path("voice_vault", "dialogue.wav")
        write_wav(dialogue)
        output = config.resolve_drive_path("renders_archive", "broadcast.wav")
        with patch.object(self.engine, "_ffmpeg", return_value="ffmpeg"):
            with patch.object(self.engine, "_run_process") as run:
                result = self.engine.master_audio(dialogue, output_path=output)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-filter_threads", command)
        self.assertIn(str(config.FFMPEG_FILTER_THREADS), command)
        self.assertIn("loudnorm=I=-14.0:TP=-1.0", command[command.index("-filter_complex") + 1])
        self.assertEqual(result.path, output)
        self.assertEqual(result.target_lufs, -14.0)

    def test_non_looping_short_bgm_is_not_repeated_without_declaration(self) -> None:
        bgm = config.asset_directory("bgm") / "short.wav"
        write_wav(bgm, seconds=0.5)
        bgm.with_suffix(".wav.license.json").write_text(
            json.dumps({
                "license": "owned", "source": "User recording", "commercial_use": True,
                "rights_confirmed_by_user": True, "loop_safe": False,
            }),
            encoding="utf-8",
        )
        dialogue = config.resolve_drive_path("voice_vault", "long.wav")
        write_wav(dialogue, seconds=2.0)
        with patch.object(self.engine, "_ffmpeg", return_value="ffmpeg"):
            with self.assertRaisesRegex(AudioRightsError, "not verified loop-safe"):
                self.engine.master_audio(dialogue, bgm_path=bgm, duration_seconds=2.0)


if __name__ == "__main__":
    unittest.main()
