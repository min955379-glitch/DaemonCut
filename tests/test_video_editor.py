from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.video_editor import (
    ClipSpec,
    FramingMode,
    VideoEditor,
    VideoEditorError,
    VideoRenderError,
)


class VideoEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temp_root = Path(self.temporary.name)
        self.drive = self.temp_root / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        config.ensure_drive_layout()
        self.source_a = self.temp_root / "scene one.mp4"
        self.source_b = self.temp_root / "scene two.mp4"
        self.source_a.write_bytes(b"source A")
        self.source_b.write_bytes(b"source B")
        self.editor = VideoEditor(ffmpeg="ffmpeg", ffprobe="ffprobe")
        self.ffmpeg_patch = patch.object(self.editor, "_resolve_ffmpeg", return_value="ffmpeg")
        self.ffmpeg_patch.start()
        self.addCleanup(self.ffmpeg_patch.stop)
        self.output = config.resolve_drive_path("renders_archive", "episode.mp4")

    def build(self, clips: list[ClipSpec], **kwargs: object) -> list[str]:
        return self.editor.build_ffmpeg_command(clips, output_path=self.output, **kwargs)

    def test_command_uses_frame_accurate_trim_pitch_preserving_speed_and_amf(self) -> None:
        command = self.build(
            [ClipSpec(self.source_a, start_seconds=0.25, end_seconds=4.0, speed=1.25, include_audio=True)],
            transition_duration=0.0,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("trim=start=0.250000:end=4.000000", graph)
        self.assertIn("setpts=(PTS-STARTPTS)/1.250000", graph)
        self.assertIn("atempo=1.250000", graph)
        self.assertIn("format=yuv420p", graph)
        self.assertIn("-c:v", command)
        self.assertIn("h264_amf", command)
        self.assertIn("-filter_threads", command)
        self.assertIn(str(config.FFMPEG_FILTER_THREADS), command)
        self.assertIn("-threads", command)
        self.assertIn(str(config.FFMPEG_THREADS), command)
        self.assertEqual(command[-1], str(self.output))

    def test_all_vertical_framing_modes_construct_expected_filters(self) -> None:
        cases = (
            (FramingMode.WHOLE_BLUR, "boxblur", "overlay"),
            (FramingMode.ZOOM_BLUR, "boxblur", "scale=iw*1.08"),
            (FramingMode.FULL_SCREEN, "crop=1080:1920", "(iw-ow)*0.25000"),
        )
        for mode, needle_a, needle_b in cases:
            with self.subTest(mode=mode):
                command = self.build(
                    [ClipSpec(self.source_a, end_seconds=4.0, framing_mode=mode, center_x=0.25)],
                )
                graph = command[command.index("-filter_complex") + 1]
                self.assertIn(needle_a, graph)
                self.assertIn(needle_b, graph)

    def test_speed_ramp_uses_pitch_preservation_and_audio_mix_transitions(self) -> None:
        clips = [
            ClipSpec(self.source_a, end_seconds=4, speed=1.5, include_audio=True),
            ClipSpec(self.source_b, end_seconds=4, speed=0.75, include_audio=True),
        ]
        command = self.build(clips, transition="wipeleft", transition_duration=0.4)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=wipeleft", graph)
        self.assertIn("atempo=1.500000", graph)
        self.assertIn("atempo=0.750000", graph)
        self.assertIn("acrossfade=d=0.400000", graph)

    def test_lut_must_be_loaded_from_drive_asset_directory(self) -> None:
        lut = config.asset_directory("luts") / "warm.cube"
        lut.write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
        command = self.build([ClipSpec(self.source_a, end_seconds=2)], lut_name="warm.cube")
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("lut3d=file=", graph)
        self.assertIn(str(lut), graph)
        with self.assertRaisesRegex(VideoEditorError, "inside Google Drive"):
            self.build([ClipSpec(self.source_a, end_seconds=2)], lut_name=str(self.source_b))

    def test_drive_archive_and_prompt_law_remain_enforced(self) -> None:
        outside = self.temp_root / "outside.mp4"
        with self.assertRaisesRegex(VideoEditorError, "Google Drive"):
            self.editor.build_ffmpeg_command(
                [ClipSpec(self.source_a, end_seconds=2)], output_path=outside,
            )
        self.assertEqual(config.MAX_YT_CREATE_CHARS, 880)
        command = self.build([ClipSpec(self.source_a, end_seconds=2)], flash_forward_seconds=3.0)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[flashforward][vprogram]concat", graph)
        with self.assertRaisesRegex(ValueError, "3-second"):
            self.build([ClipSpec(self.source_a, end_seconds=2)], flash_forward_seconds=3.1)

    def test_motion_effects_and_full_screen_subject_offset_are_bounded(self) -> None:
        punch = self.build([ClipSpec(self.source_a, end_seconds=2, motion_effect="punch_in")])
        self.assertIn("zoompan=", punch[punch.index("-filter_complex") + 1])
        shake = self.build([ClipSpec(self.source_a, end_seconds=2, motion_effect="shake")])
        self.assertIn("sin(t*13)", shake[shake.index("-filter_complex") + 1])
        with self.assertRaisesRegex(ValueError, "center_x"):
            self.build([ClipSpec(self.source_a, end_seconds=2, center_x=1.5)])
        with self.assertRaisesRegex(ValueError, "0.75 and 1.5"):
            self.build([ClipSpec(self.source_a, end_seconds=2, speed=2.0)])

    def test_amf_failure_retries_cleanly_with_libx264(self) -> None:
        def fake_run(command: list[str], *, label: str, timeout: int = 7200) -> object:
            if "h264_amf" in command:
                raise VideoRenderError("AMF h264_amf no capable devices / encoder unavailable")
            Path(command[-1]).write_bytes(b"encoded with libx264")
            return object()

        with patch.object(self.editor, "_run_process", side_effect=fake_run) as run:
            result = self.editor.render(
                [ClipSpec(self.source_a, end_seconds=2)],
                output_path=self.output,
            )
        self.assertTrue(result.used_software_fallback)
        self.assertEqual(result.video_encoder, "libx264")
        self.assertIn("libx264", result.command)
        self.assertTrue(self.output.is_file())
        self.assertEqual(run.call_count, 2)

    def test_lanczos_upscaler_fallback_archives_to_drive(self) -> None:
        input_video = self.temp_root / "input.mp4"
        input_video.write_bytes(b"small video")
        with patch.object(self.editor, "build_realesrgan_command", return_value=None):
            with patch.object(self.editor, "_run_process", side_effect=lambda command, **_kwargs: Path(command[-1]).write_bytes(b"upscaled")) as run:
                result = self.editor.upscale_to_1440p(input_video, output_path=self.output, prefer_vulkan=True)
        self.assertEqual(result.method, "lanczos")
        self.assertFalse(result.used_vulkan)
        self.assertTrue(self.output.is_relative_to(self.drive))
        command = run.call_args.args[0]
        self.assertIn("scale=1440:2560:flags=lanczos,fps=60", command)
        self.assertIn("-threads", command)

    def test_vulkan_upscaler_resolves_drive_model_folder(self) -> None:
        model_root = config.resolve_drive_path("models", "realesrgan")
        model_root.mkdir(parents=True, exist_ok=True)
        binary = model_root / "realesrgan-ncnn-vulkan"
        binary.write_text("binary placeholder", encoding="utf-8")
        command = self.editor.build_realesrgan_command(self.source_a, self.output)
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command[0], str(binary))
        self.assertIn(str(model_root), command)
        self.assertIn("-s", command)
        self.assertIn("2", command)
        self.assertIn("-f", command)
        self.assertIn("jpg", command)

    def test_vulkan_upscaler_processes_frames_in_temp_and_falls_back_from_amf(self) -> None:
        input_video = self.temp_root / "rendered.mp4"
        input_video.write_bytes(b"video")
        calls: list[str] = []

        def fake_run(command: list[str], *, label: str, timeout: int = 7200) -> object:
            calls.append(label)
            if label == "1440p upscaler frame extraction":
                frame_pattern = Path(command[-1])
                frame_pattern.parent.mkdir(parents=True, exist_ok=True)
                (frame_pattern.parent / "00000001.jpg").write_bytes(b"frame")
            elif label == "Vulkan Real-ESRGAN frame upscale":
                output_dir = Path(command[command.index("-o") + 1])
                (output_dir / "00000001.jpg").write_bytes(b"upscaled frame")
            elif label == "AMF Real-ESRGAN 1440p encode":
                raise VideoRenderError("AMF h264_amf no capable devices")
            elif label == "libx264 Real-ESRGAN 1440p encode":
                Path(command[-1]).write_bytes(b"1440p master")
            return object()

        def fake_esrgan(input_dir: Path, output_dir: Path, **_kwargs: object) -> list[str]:
            return ["realesrgan-ncnn-vulkan", "-i", str(input_dir), "-o", str(output_dir)]

        with patch.object(self.editor, "build_realesrgan_command", side_effect=fake_esrgan):
            with patch.object(self.editor, "_run_process", side_effect=fake_run):
                result = self.editor.upscale_to_1440p(input_video, output_path=self.output, prefer_vulkan=True)
        self.assertEqual(result.method, "realesrgan_ncnn_vulkan")
        self.assertTrue(result.used_vulkan)
        self.assertTrue(any("AMF unavailable" in warning for warning in result.warnings))
        self.assertIn("1440p upscaler frame extraction", calls)
        self.assertIn("Vulkan Real-ESRGAN frame upscale", calls)
        self.assertIn("libx264 Real-ESRGAN 1440p encode", calls)
        self.assertTrue(self.output.is_file())


if __name__ == "__main__":
    unittest.main()
