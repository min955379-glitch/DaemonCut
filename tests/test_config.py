from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config


class ConfigTests(unittest.TestCase):
    def test_drive_layout_and_asset_resolver_are_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "DaemonCut Development"
            with patch.object(config, "GDRIVE_DAEMONCUT_PATH", root):
                folders = config.ensure_drive_layout()
                self.assertTrue(folders["models/ollama"].is_dir())
                self.assertTrue(folders["assets/bgm"].is_dir())
                self.assertEqual(config.asset_directory("sfx"), root / "assets" / "sfx")
                with self.assertRaisesRegex(ValueError, "escapes"):
                    config.resolve_drive_path("..", "outside")

    def test_drive_path_is_required_and_must_use_an_approved_root_name(self) -> None:
        with patch.object(config, "GDRIVE_DAEMONCUT_PATH", None):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                config.resolve_drive_path("models")
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(config, "GDRIVE_DAEMONCUT_PATH", Path(temporary) / "My Drive"):
                with self.assertRaisesRegex(ValueError, "DaemonCut"):
                    config.resolve_drive_path("models")

    def test_hardware_and_prompt_defaults(self) -> None:
        self.assertEqual(config.MAX_YT_CREATE_CHARS, 880)
        self.assertEqual(config.GEMINI_MODEL_ID, "gemini-4-hydrogen")
        self.assertEqual(config.FFMPEG_H264_ENCODER, "h264_amf")
        self.assertEqual(config.FFMPEG_HEVC_ENCODER, "hevc_amf")
        self.assertEqual(config.UI_SAFE_ZONE_MIN_WIDTH_PX, 380)
        self.assertTrue(config.UI_ZERO_GREEN_POLICY)
        self.assertEqual(config.UI_PALETTE["muted_brass"], "#D6AD55")
        self.assertEqual(config.UI_STATUS_COLORS["success"], "#D6AD55")
        self.assertEqual(config.UI_STATUS_COLORS["alert"], "#C94A4A")
        self.assertEqual(config.UI_FONT_SANS, "IBM Plex Sans")
        self.assertEqual(config.UI_FONT_MONO, "IBM Plex Mono")
        self.assertEqual(config.ffmpeg_encoder_args("h264")[1], "h264_amf")
        self.assertEqual(config.ollama_options()["num_thread"], 6)


if __name__ == "__main__":
    unittest.main()
