"""Sharp-frame selection and zero-green Pillow thumbnail rendering."""
from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from backend import config
from backend.brain_gemini import AIProviderUnavailable, AIResponseError, Brain
from backend.video_editor import VideoEditor, VideoEditorError


class ThumbnailError(RuntimeError):
    """Base error for frame selection and thumbnail creation."""


class ThumbnailDependencyUnavailable(ThumbnailError):
    """Pillow or an optional video/image dependency is missing."""


class ThumbnailAssetError(ThumbnailError):
    """An image/font/output path violates the Drive asset policy."""


@dataclass(frozen=True)
class FrameCandidate:
    image: Any
    timestamp_seconds: float
    sharpness: float
    source: str = "video"


@dataclass(frozen=True)
class ThumbnailResult:
    cover_path: Path
    thumbnail_path: Path
    hook: str
    hook_word_count: int
    selected_frame_timestamp: float | None
    sharpness_score: float
    font_path: Path | None
    colors: tuple[str, ...]


_APPROVED_COLORS = frozenset(
    {
        "#D6AD55",
        "#F4EFE6",
        "#2B5B84",
        "#102B4E",
        "#121316",
        "#C94A4A",
    }
)
_HOOK_TOKEN_RE = re.compile(r"\S+")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}


def _require_pillow() -> tuple[Any, Any, Any, Any]:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:
        raise ThumbnailDependencyUnavailable("Pillow is required for thumbnail image rendering; install Pillow from requirements.txt") from exc
    return Image, ImageDraw, ImageFont, ImageOps


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _approved_color(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a hex color string")
    normalized = value.strip().upper()
    if not normalized.startswith("#"):
        normalized = "#" + normalized
    if normalized not in _APPROVED_COLORS:
        raise ValueError(f"{name} color {normalized!r} is not in the approved zero-green palette")
    return normalized


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


class ThumbnailMaker:
    """Builds 1080x1920 cover art and 1280x720 thumbnail side-by-side."""

    def __init__(self, *, brain: Brain | None = None, video_editor: VideoEditor | None = None) -> None:
        self.brain = brain or Brain()
        self.video_editor = video_editor or VideoEditor()

    @staticmethod
    def normalize_hook(hook: str, *, max_words: int = 3) -> tuple[str, int]:
        if not isinstance(hook, str) or not hook.strip():
            raise ValueError("hook cannot be empty")
        if max_words != 3:
            raise ValueError("DaemonCut thumbnail hooks are strictly limited to three words")
        normalized = re.sub(r"\s+", " ", hook).strip()
        words = _HOOK_TOKEN_RE.findall(normalized)
        if len(words) > 3:
            raise ValueError(f"thumbnail hook must contain at most 3 words, got {len(words)}")
        if any("\x00" <= char <= "\x1f" for char in normalized):
            raise ValueError("thumbnail hook cannot contain control characters")
        return normalized, len(words)

    @staticmethod
    def safe_zone_margin(height: int, requested: int | None = None) -> int:
        if height <= 0:
            raise ValueError("height must be positive")
        scaled = int(math.ceil(config.UI_SAFE_ZONE_MIN_WIDTH_PX * height / 1920))
        if requested is not None and requested < 0:
            raise ValueError("requested margin cannot be negative")
        return max(scaled, int(requested or 0))

    @staticmethod
    def sharpness_score(image: Any, *, prefer_opencv: bool = True) -> float:
        """Laplacian-variance focus score with optional OpenCV acceleration."""
        Image, _, _, ImageOps = _require_pillow()
        if not isinstance(image, Image.Image):
            raise TypeError("image must be a Pillow Image")
        if prefer_opencv:
            try:
                import cv2  # type: ignore[import-not-found]
                import numpy as np  # type: ignore[import-not-found]

                gray = np.asarray(ImageOps.grayscale(image))
                return float(cv2.Laplacian(gray, cv2.CV_64F).var())
            except Exception:
                # A partial/broken optional OpenCV install should never block
                # the pure-Pillow Laplacian fallback.
                pass
        gray_image = ImageOps.grayscale(image)
        gray_image.thumbnail((320, 320))
        pixels = gray_image.load()
        width, height = gray_image.size
        if width < 3 or height < 3:
            return 0.0
        values: list[float] = []
        # The 4-neighbour discrete Laplacian approximates the focus response;
        # downsampling bounds CPU and temporary RAM on the 16 GB target host.
        stride = max(1, min(width, height) // 240)
        for y in range(1, height - 1, stride):
            for x in range(1, width - 1, stride):
                center = int(pixels[x, y])
                laplacian = (
                    int(pixels[x - 1, y]) + int(pixels[x + 1, y])
                    + int(pixels[x, y - 1]) + int(pixels[x, y + 1])
                    - 4 * center
                )
                values.append(float(laplacian))
        if not values:
            return 0.0
        mean = sum(values) / len(values)
        return sum((value - mean) ** 2 for value in values) / len(values)

    @staticmethod
    def _ffmpeg_binary() -> str:
        configured = config.FFMPEG_BINARY
        found = shutil.which(configured)
        if found:
            return found
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise ThumbnailDependencyUnavailable(f"FFmpeg executable {configured!r} is required to sample video frames")

    def sample_video_frames(
        self,
        video_path: str | Path,
        *,
        candidate_count: int = 7,
        timestamps: Sequence[float] | None = None,
    ) -> list[FrameCandidate]:
        """Extract candidate frames into memory (no persistent media scratch files)."""
        Image, _, _, _ = _require_pillow()
        source = Path(video_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"video_path is not a file: {source}")
        if timestamps is None:
            if not 1 <= candidate_count <= 24:
                raise ValueError("candidate_count must be between 1 and 24")
            try:
                duration = self.video_editor.probe_duration(source)
            except VideoEditorError as exc:
                raise ThumbnailError(f"Could not sample video duration: {exc}") from exc
            # Avoid the first/last 5% to reduce black-frame and end-card bias.
            if candidate_count == 1:
                sample_times = [duration * 0.5]
            else:
                sample_times = [duration * (0.05 + 0.9 * i / (candidate_count - 1)) for i in range(candidate_count)]
        else:
            sample_times = [float(value) for value in timestamps]
            if not sample_times or any(not math.isfinite(value) or value < 0 for value in sample_times):
                raise ValueError("timestamps must contain finite non-negative seconds")
        ffmpeg = self._ffmpeg_binary()
        candidates: list[FrameCandidate] = []
        for timestamp in sample_times:
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
                *config.ffmpeg_filter_args(),
                "-i", str(source), "-frames:v", "1", "-vf", "scale=960:960:force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ]
            try:
                result = subprocess.run(command, check=True, capture_output=True, timeout=60)
            except FileNotFoundError as exc:
                raise ThumbnailDependencyUnavailable(f"FFmpeg was not found: {ffmpeg}") from exc
            except (subprocess.SubprocessError, OSError):
                continue
            if not result.stdout:
                continue
            try:
                with Image.open(io.BytesIO(result.stdout)) as opened:
                    frame = opened.convert("RGB").copy()
            except Exception:
                continue
            candidates.append(FrameCandidate(frame, timestamp, self.sharpness_score(frame), "video"))
        if not candidates:
            raise ThumbnailError(f"FFmpeg could not extract a frame from {source.name}")
        return candidates

    @staticmethod
    def _png_bytes(image: Any) -> bytes:
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()

    def _brain_select(self, candidates: Sequence[FrameCandidate], *, context: str = "") -> int | None:
        if len(candidates) < 2:
            return 0 if candidates else None
        try:
            frames = [(self._png_bytes(candidate.image), "image/png") for candidate in candidates]
            system = (
                "You are a cautious visual editor selecting one appealing thumbnail frame from supplied candidate images. "
                "Prefer a clear, expressive subject and readable action; do not infer unsupported story facts. "
                "Return JSON with selected_index as a zero-based integer and a brief reason."
            )
            prompt = (
                f"Choose exactly one of {len(candidates)} candidate frames for an honest, legible short-video thumbnail. "
                f"Context: {context[:1000] or 'No additional context.'} "
                "Indices are in the order of the attached images."
            )
            # Brain's multimodal request path accepts in-memory frames, so no
            # reference image is written to a repo or local scratch folder.
            providers = self.brain._provider_plan()
            failures: list[str] = []
            raw: str | None = None
            for provider in providers:
                try:
                    if provider == "gemini":
                        raw = self.brain._gemini_multimodal(system, prompt, frames, temperature=0.15)
                    elif provider == "ollama":
                        raw = self.brain._ollama_multimodal(system, prompt, frames, temperature=0.15)
                    if raw:
                        break
                except (AIProviderUnavailable, AIResponseError) as exc:
                    failures.append(f"{provider}: {exc}")
            if not raw:
                return None
            parsed = self.brain._parse_json_text(raw)
            if not isinstance(parsed, dict):
                return None
            value = parsed.get("selected_index", parsed.get("index"))
            if isinstance(value, bool):
                return None
            index = int(value)
            return index if 0 <= index < len(candidates) else None
        except (AIProviderUnavailable, AIResponseError, ValueError, TypeError, AttributeError, ImportError):
            return None

    def choose_best_frame(
        self,
        candidates: Sequence[FrameCandidate],
        *,
        use_brain: bool = False,
        context: str = "",
    ) -> FrameCandidate:
        if not candidates:
            raise ValueError("candidates cannot be empty")
        if use_brain:
            selected = self._brain_select(candidates, context=context)
            if selected is not None:
                return candidates[selected]
        return max(candidates, key=lambda candidate: candidate.sharpness)

    @staticmethod
    def _load_frame(frame: Any) -> Any:
        Image, _, _, _ = _require_pillow()
        if isinstance(frame, FrameCandidate):
            frame = frame.image
        if isinstance(frame, Image.Image):
            return frame.convert("RGB").copy()
        if isinstance(frame, (bytes, bytearray)):
            try:
                with Image.open(io.BytesIO(frame)) as opened:
                    return opened.convert("RGB").copy()
            except Exception as exc:
                raise ThumbnailError(f"Could not decode provided image bytes: {exc}") from exc
        path = Path(frame).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.casefold() not in _IMAGE_SUFFIXES:
            raise ValueError("frame must be a Pillow Image, encoded image bytes, or a supported image file")
        try:
            with Image.open(path) as opened:
                return opened.convert("RGB").copy()
        except Exception as exc:
            raise ThumbnailError(f"Could not open image {path.name}: {exc}") from exc

    @staticmethod
    def _font_from_drive(font_path: str | Path | None, *, font_name: str | None = None) -> tuple[Path | None, Any]:
        _, _, ImageFont, _ = _require_pillow()
        fonts_root: Path | None
        try:
            fonts_root = config.asset_directory("fonts").resolve(strict=False)
        except RuntimeError:
            fonts_root = None
        if font_path is not None:
            chosen = Path(font_path).expanduser().resolve(strict=True)
            if fonts_root is None or not _is_within(chosen, fonts_root):
                raise ThumbnailAssetError("Custom fonts must be stored in Google Drive assets/fonts")
            if not chosen.is_file() or chosen.suffix.casefold() not in _FONT_SUFFIXES:
                raise ThumbnailAssetError("Font must be a readable .ttf, .otf, or .ttc file in assets/fonts")
            return chosen, ImageFont.truetype(str(chosen), size=80)
        if fonts_root is not None and fonts_root.is_dir():
            candidates = sorted(path for path in fonts_root.iterdir() if path.is_file() and path.suffix.casefold() in _FONT_SUFFIXES)
            if font_name:
                wanted = font_name.casefold()
                candidates.sort(key=lambda path: (wanted not in path.stem.casefold(), path.name.casefold()))
            else:
                candidates.sort(key=lambda path: ("bold" not in path.stem.casefold(), path.name.casefold()))
            if candidates:
                try:
                    return candidates[0], ImageFont.truetype(str(candidates[0]), size=80)
                except OSError:
                    pass
        return None, ImageFont.load_default()

    @staticmethod
    def _safe_output_directory(output_directory: str | Path | None) -> Path:
        if output_directory is None:
            directory = config.resolve_drive_path("renders_archive")
        else:
            directory = Path(output_directory).expanduser().resolve(strict=False)
        root = config.resolve_drive_path("renders_archive")
        if not _is_within(directory, root):
            raise ThumbnailAssetError("Thumbnail images must be saved inside Google Drive renders_archive")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _wrap_hook(hook: str, font: Any, draw: Any, max_width: int) -> list[str]:
        words = hook.split()
        lines: list[str] = []
        current = ""
        for word in words:
            proposed = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), proposed, font=font, stroke_width=0)
            if current and bbox[2] - bbox[0] > max_width:
                lines.append(current)
                current = word
            else:
                current = proposed
        if current:
            lines.append(current)
        return lines

    @staticmethod
    def _load_font(ImageFont: Any, font_path: Path | None, size: int) -> Any:
        if font_path is not None:
            return ImageFont.truetype(str(font_path), size=size)
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    def _render_one(
        self,
        source_image: Any,
        *,
        size: tuple[int, int],
        hook: str,
        output_path: Path,
        text_color: str,
        stroke_color: str,
        badge_color: str,
        font_path: Path | None,
        font_name: str | None,
        safe_margin_px: int | None,
        badge_text: str | None,
    ) -> Path:
        Image, ImageDraw, ImageFont, ImageOps = _require_pillow()
        width, height = size
        canvas = ImageOps.fit(
            source_image,
            (width, height),
            method=getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "LANCZOS", 1)),
            centering=(0.5, 0.48),
        ).convert("RGB")
        gradient = Image.new("RGBA", (1, height), (18, 19, 22, 0))
        gradient.putdata(
            [
                (
                    18,
                    19,
                    22,
                    int(190 * max(0.0, min(1.0, (y / max(1, height - 1) - 0.44) / 0.56)) ** 1.25),
                )
                for y in range(height)
            ]
        )
        overlay = gradient.resize((width, height))
        composed = Image.alpha_composite(canvas.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(composed)
        chosen_font_path = font_path
        initial_size = max(30, int(height * (0.084 if height > width else 0.135)))
        font = self._load_font(ImageFont, chosen_font_path, initial_size)
        max_width = int(width * 0.84)
        lines: list[str] = []
        font_size = initial_size
        min_font_size = max(18, int(height * 0.02))
        fits = False
        while font_size >= min_font_size:
            font = self._load_font(ImageFont, chosen_font_path, font_size)
            lines = self._wrap_hook(hook, font, draw, max_width)
            bboxes = [draw.textbbox((0, 0), line, font=font, stroke_width=max(1, font_size // 16)) for line in lines]
            total_height = sum(box[3] - box[1] for box in bboxes) + max(0, len(lines) - 1) * int(font_size * 0.16)
            if len(lines) <= 3 and all(box[2] - box[0] <= max_width for box in bboxes) and total_height <= height * 0.23:
                fits = True
                break
            font_size -= 4
        if not fits:
            raise ValueError("thumbnail hook is too long to fit legibly within the three-line layout")
        stroke_width = max(2, font_size // 14)
        line_boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width) for line in lines]
        line_heights = [box[3] - box[1] for box in line_boxes]
        gap = max(3, int(font_size * 0.12))
        block_height = sum(line_heights) + gap * max(0, len(lines) - 1)
        margin = self.safe_zone_margin(height, safe_margin_px)
        panel_bottom = height - margin
        block_bottom = min(panel_bottom - int(height * 0.035), int(height * 0.83))
        block_top = max(int(height * 0.50), block_bottom - block_height)
        panel_x0 = int(width * 0.055)
        panel_x1 = width - panel_x0
        panel_y0 = max(int(height * 0.45), block_top - int(height * 0.03))
        panel_y1 = min(panel_bottom - int(height * 0.015), block_bottom + int(height * 0.035))
        panel_fill = (18, 19, 22, 192)
        draw.rounded_rectangle((panel_x0, panel_y0, panel_x1, panel_y1), radius=max(12, int(height * 0.018)), fill=panel_fill)
        brass = _rgb("#D6AD55")
        stripe_h = max(5, int(height * 0.006))
        draw.rounded_rectangle(
            (panel_x0, panel_y0, panel_x1, panel_y0 + stripe_h),
            radius=max(2, stripe_h // 2),
            fill=(*brass, 255),
        )
        if badge_text:
            badge = re.sub(r"\s+", " ", badge_text).strip()
            if len(badge) > 24:
                raise ValueError("badge_text must be at most 24 characters")
            badge_font_size = max(18, int(height * 0.026))
            badge_font = self._load_font(ImageFont, chosen_font_path, badge_font_size)
            badge_box = draw.textbbox((0, 0), badge, font=badge_font)
            bx = panel_x0 + int(width * 0.035)
            by = max(int(height * 0.04), panel_y0 - badge_box[3] - int(height * 0.025))
            pad_x, pad_y = int(height * 0.014), int(height * 0.01)
            draw.rounded_rectangle(
                (bx - pad_x, by - pad_y, bx + badge_box[2] + pad_x, by + badge_box[3] + pad_y),
                radius=max(8, int(height * 0.012)),
                fill=(*_rgb(badge_color), 255),
            )
            draw.text((bx, by), badge, font=badge_font, fill=(*_rgb("#F4EFE6"), 255))
        y = block_top
        for line, bbox, line_height in zip(lines, line_boxes, line_heights):
            line_width = bbox[2] - bbox[0]
            x = (width - line_width) // 2
            draw.text(
                (x, y), line, font=font, fill=(*_rgb(text_color), 255),
                stroke_width=stroke_width, stroke_fill=(*_rgb(stroke_color), 255),
            )
            y += line_height + gap
        composed.convert("RGB").save(output_path, format="JPEG", quality=93, optimize=True, progressive=True)
        return output_path

    def make_thumbnails(
        self,
        frame: Any,
        hook: str,
        *,
        output_directory: str | Path | None = None,
        font_path: str | Path | None = None,
        font_name: str | None = None,
        text_color: str = "#F4EFE6",
        stroke_color: str = "#121316",
        badge_color: str = "#C94A4A",
        badge_text: str | None = None,
        safe_margin_px: int | None = None,
        filename_prefix: str | None = None,
        selected_timestamp: float | None = None,
    ) -> ThumbnailResult:
        """Render both vertical cover and 16:9 thumbnail under Drive archive."""
        normalized_hook, word_count = self.normalize_hook(hook)
        colors = (
            _approved_color(text_color, name="text_color"),
            _approved_color(stroke_color, name="stroke_color"),
            _approved_color(badge_color, name="badge_color"),
        )
        source = self._load_frame(frame)
        directory = self._safe_output_directory(output_directory)
        resolved_font_path, _ = self._font_from_drive(font_path, font_name=font_name)
        prefix = filename_prefix or f"thumbnail_{uuid.uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", prefix):
            raise ValueError("filename_prefix may contain only letters, numbers, underscores, and hyphens")
        cover_path = directory / f"{prefix}_cover.jpg"
        thumbnail_path = directory / f"{prefix}_thumbnail.jpg"
        self._render_one(
            source,
            size=(1080, 1920),
            hook=normalized_hook,
            output_path=cover_path,
            text_color=colors[0], stroke_color=colors[1], badge_color=colors[2],
            font_path=resolved_font_path, font_name=font_name,
            safe_margin_px=safe_margin_px, badge_text=badge_text,
        )
        self._render_one(
            source,
            size=(1280, 720),
            hook=normalized_hook,
            output_path=thumbnail_path,
            text_color=colors[0], stroke_color=colors[1], badge_color=colors[2],
            font_path=resolved_font_path, font_name=font_name,
            safe_margin_px=safe_margin_px, badge_text=badge_text,
        )
        score = self.sharpness_score(source)
        return ThumbnailResult(
            cover_path=cover_path,
            thumbnail_path=thumbnail_path,
            hook=normalized_hook,
            hook_word_count=word_count,
            selected_frame_timestamp=selected_timestamp,
            sharpness_score=score,
            font_path=resolved_font_path,
            colors=colors,
        )

    def make_from_video(
        self,
        video_path: str | Path,
        hook: str,
        *,
        candidate_count: int = 7,
        use_brain: bool = False,
        selection_context: str = "",
        **thumbnail_options: Any,
    ) -> ThumbnailResult:
        candidates = self.sample_video_frames(video_path, candidate_count=candidate_count)
        selected = self.choose_best_frame(candidates, use_brain=use_brain, context=selection_context)
        return self.make_thumbnails(
            selected,
            hook,
            selected_timestamp=selected.timestamp_seconds,
            **thumbnail_options,
        )


__all__ = [
    "FrameCandidate",
    "ThumbnailAssetError",
    "ThumbnailDependencyUnavailable",
    "ThumbnailError",
    "ThumbnailMaker",
    "ThumbnailResult",
]
