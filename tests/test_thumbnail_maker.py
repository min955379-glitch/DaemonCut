from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.thumbnail_maker import FrameCandidate, ThumbnailAssetError, ThumbnailMaker

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None


@unittest.skipUnless(PIL_AVAILABLE, "Pillow is installed from requirements.txt for image rendering")
class ThumbnailMakerTests(unittest.TestCase):
    def setUp(self) -> None:
        from PIL import Image, ImageDraw, ImageFilter

        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFilter = ImageFilter
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.drive = Path(self.temporary.name) / "DaemonCut Development"
        self.drive.mkdir()
        self.drive_patch = patch.object(config, "GDRIVE_DAEMONCUT_PATH", self.drive)
        self.drive_patch.start()
        self.addCleanup(self.drive_patch.stop)
        config.ensure_drive_layout()
        self.maker = ThumbnailMaker()
        self.image = Image.new("RGB", (640, 360), "#2B5B84")
        draw = ImageDraw.Draw(self.image)
        draw.rectangle((180, 50, 460, 330), fill="#D6AD55")
        draw.ellipse((250, 100, 390, 240), fill="#F4EFE6")

    def test_hook_is_limited_to_three_words(self) -> None:
        clean, count = self.maker.normalize_hook("  The  clever  fix!  ")
        self.assertEqual(clean, "The clever fix!")
        self.assertEqual(count, 3)
        with self.assertRaisesRegex(ValueError, "at most 3 words"):
            self.maker.normalize_hook("This hook has four")
        with self.assertRaisesRegex(ValueError, "strictly limited"):
            self.maker.normalize_hook("One hook", max_words=4)

    def test_zero_green_palette_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero-green"):
            self.maker.make_thumbnails(self.image, "Keep it simple", text_color="#00FF00")
        with self.assertRaisesRegex(ValueError, "approved"):
            self.maker.make_thumbnails(self.image, "Keep it simple", badge_color="#32CD32")

    def test_laplacian_fallback_scores_sharp_frame_above_blur(self) -> None:
        sharp = self.Image.new("L", (256, 256), 0)
        draw = self.ImageDraw.Draw(sharp)
        for offset in range(0, 256, 8):
            draw.line((offset, 0, offset, 255), fill=255 if (offset // 8) % 2 else 0, width=4)
        blurred = sharp.filter(self.ImageFilter.GaussianBlur(radius=6))
        sharp_score = self.maker.sharpness_score(sharp, prefer_opencv=False)
        blurred_score = self.maker.sharpness_score(blurred, prefer_opencv=False)
        self.assertGreater(sharp_score, blurred_score)

    def test_thumbnail_renders_cover_and_1280x720_to_drive(self) -> None:
        result = self.maker.make_thumbnails(
            self.image,
            "A brilliant idea",
            filename_prefix="unit_test",
            badge_text="NEW",
        )
        self.assertTrue(result.cover_path.is_file())
        self.assertTrue(result.thumbnail_path.is_file())
        self.assertTrue(result.cover_path.is_relative_to(self.drive))
        self.assertEqual(result.hook_word_count, 3)
        with self.Image.open(result.cover_path) as cover:
            self.assertEqual(cover.size, (1080, 1920))
        with self.Image.open(result.thumbnail_path) as thumbnail:
            self.assertEqual(thumbnail.size, (1280, 720))
        self.assertEqual(result.colors, ("#F4EFE6", "#121316", "#C94A4A"))

    def test_safe_zone_scales_and_custom_font_must_be_in_drive(self) -> None:
        self.assertEqual(self.maker.safe_zone_margin(1920), 380)
        self.assertEqual(self.maker.safe_zone_margin(720), 143)
        outside_font = Path(self.temporary.name) / "local.ttf"
        outside_font.write_bytes(b"not a real font")
        with self.assertRaisesRegex(ThumbnailAssetError, "assets/fonts"):
            self.maker.make_thumbnails(self.image, "A safe hook", font_path=outside_font)

    def test_optional_brain_can_select_an_expressive_candidate(self) -> None:
        class FakeBrain:
            def _provider_plan(self) -> list[str]:
                return ["ollama"]

            def _ollama_multimodal(self, *_args: object, **_kwargs: object) -> str:
                return '{"selected_index": 0, "reason": "clear expression"}'

            @staticmethod
            def _parse_json_text(text: str) -> dict[str, object]:
                import json

                return json.loads(text)

        maker = ThumbnailMaker(brain=FakeBrain())  # type: ignore[arg-type]
        blurry = FrameCandidate(self.image, 1.0, 2.0)
        sharp = FrameCandidate(self.image, 2.0, 100.0)
        selected = maker.choose_best_frame([blurry, sharp], use_brain=True, context="Choose a happy reaction")
        self.assertIs(selected, blurry)
        fallback = maker.choose_best_frame([blurry, sharp], use_brain=False)
        self.assertIs(fallback, sharp)

    def test_thumbnail_output_directory_cannot_escape_drive(self) -> None:
        outside = Path(self.temporary.name) / "unapproved"
        with self.assertRaisesRegex(ThumbnailAssetError, "inside Google Drive"):
            self.maker.make_thumbnails(self.image, "Archive safely", output_directory=outside)


if __name__ == "__main__":
    unittest.main()
