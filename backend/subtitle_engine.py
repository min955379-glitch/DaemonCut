"""Karaoke ASS/SRT generation with word timing and strict palette/safe-zone guards."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend import config
from backend.brain_gemini import AIProviderUnavailable, Brain


class SubtitleError(RuntimeError):
    """Base error for subtitle creation and timing validation."""


@dataclass(frozen=True)
class WordTiming:
    word: str
    start: float
    end: float
    highlight_color: str | None = None


@dataclass(frozen=True)
class SubtitleSidecars:
    ass_path: Path
    srt_path: Path
    caption_text: str
    language_mode: str
    word_count: int
    safe_margin_px: int


_TOKEN_RE = re.compile(r"\S+")
_ALLOWED_CAPTION_COLORS = frozenset(
    {
        "#D6AD55",  # muted brass
        "#F4EFE6",  # warm parchment
        "#2B5B84",  # deep steel blue
        "#102B4E",  # deep navy
        "#121316",  # matte obsidian
        "#C94A4A",  # crimson
    }
)


def _color_hex(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Subtitle colors must be hex strings")
    value = value.strip().upper()
    if not value.startswith("#"):
        value = "#" + value
    if value not in _ALLOWED_CAPTION_COLORS:
        raise ValueError(f"Color {value!r} is not in the approved zero-green subtitle palette")
    return value


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    normalized = _color_hex(hex_color).lstrip("#")
    red, green, blue = normalized[0:2], normalized[2:4], normalized[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}"


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{cs:02d}"


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{ms:03d}"


def _safe_subtitle_text(text: str) -> str:
    # Remove ASS override delimiters and control characters so captions cannot
    # inject formatting tags into the generated subtitle file.
    cleaned = text.replace("{", "(").replace("}", ")").replace("\\", "")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


class SubtitleEngine:
    """Build word-timed dual-layer ASS karaoke and clean SRT sidecars."""

    def __init__(self, *, brain: Brain | None = None) -> None:
        self.brain = brain or Brain()
        self._whisper_models: dict[str, Any] = {}
        self._whisper_lock = threading.RLock()

    @staticmethod
    def safe_zone_margin(height: int, requested_margin: int | None = None) -> int:
        if height <= 0:
            raise ValueError("resolution height must be positive")
        minimum = int(math.ceil(config.UI_SAFE_ZONE_MIN_WIDTH_PX * height / 1920))
        if requested_margin is not None and requested_margin < 0:
            raise ValueError("requested_margin cannot be negative")
        return max(minimum, int(requested_margin or 0))

    @staticmethod
    def _syllable_weight(word: str) -> float:
        letters = re.sub(r"[^A-Za-z]", "", word).casefold()
        if not letters:
            return 1.0
        vowel_groups = re.findall(r"[aeiouy]+", letters)
        syllables = max(1, len(vowel_groups))
        if len(letters) > 8:
            syllables += 0.5
        if word.rstrip().endswith(('.', '!', '?', ':', ';', ',')):
            syllables += 0.35
        return float(syllables)

    @classmethod
    def estimate_word_timings(
        cls,
        text: str,
        duration_seconds: float,
        *,
        highlight_color: str | None = None,
    ) -> list[WordTiming]:
        """Estimate deterministic word timing from TTS duration when Whisper is absent."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text cannot be empty")
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be a positive finite number")
        tokens = _TOKEN_RE.findall(text.strip())
        if not tokens:
            return []
        if highlight_color is not None:
            highlight_color = _color_hex(highlight_color)
        pause_per_word = min(0.06, duration_seconds / max(1, len(tokens) * 4))
        pause_total = pause_per_word * max(0, len(tokens) - 1)
        available = max(duration_seconds * 0.6, duration_seconds - pause_total)
        weights = [cls._syllable_weight(token) for token in tokens]
        weight_sum = sum(weights) or 1.0
        result: list[WordTiming] = []
        cursor = 0.0
        for index, (token, weight) in enumerate(zip(tokens, weights)):
            span = available * weight / weight_sum
            end = duration_seconds if index == len(tokens) - 1 else min(duration_seconds, cursor + span)
            if end <= cursor:
                end = min(duration_seconds, cursor + max(0.01, duration_seconds / len(tokens)))
            result.append(WordTiming(token, cursor, end, highlight_color))
            cursor = end + (pause_per_word if index < len(tokens) - 1 else 0.0)
        if result:
            last = result[-1]
            result[-1] = WordTiming(last.word, last.start, max(last.start + 0.01, min(duration_seconds, last.end)), last.highlight_color)
        return result

    @staticmethod
    def _parse_whisper_time(value: Any, fallback_ms: Any = None) -> float | None:
        if isinstance(fallback_ms, (int, float)) and math.isfinite(float(fallback_ms)):
            return max(0.0, float(fallback_ms) / 1000.0)
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})", value.strip())
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        milliseconds = int(match.group(4).ljust(3, "0"))
        return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0

    @classmethod
    def _whisper_cpp_words(cls, payload: Mapping[str, Any]) -> list[WordTiming]:
        segments = payload.get("transcription", payload.get("segments", []))
        if not isinstance(segments, list):
            return []
        timings: list[WordTiming] = []
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            tokens = segment.get("tokens")
            if not isinstance(tokens, list):
                continue
            current_text = ""
            current_start: float | None = None
            current_end: float | None = None
            for token in tokens:
                if not isinstance(token, Mapping):
                    continue
                raw = token.get("text", "")
                if not isinstance(raw, str) or not raw:
                    continue
                offsets = token.get("offsets", {})
                timestamps = token.get("timestamps", {})
                start = cls._parse_whisper_time(
                    timestamps.get("from") if isinstance(timestamps, Mapping) else None,
                    offsets.get("from") if isinstance(offsets, Mapping) else None,
                )
                end = cls._parse_whisper_time(
                    timestamps.get("to") if isinstance(timestamps, Mapping) else None,
                    offsets.get("to") if isinstance(offsets, Mapping) else None,
                )
                starts_word = bool(raw[:1].isspace()) and bool(current_text.strip())
                if starts_word and current_start is not None and current_end is not None and current_end > current_start:
                    word = _safe_subtitle_text(current_text)
                    if word:
                        timings.append(WordTiming(word, current_start, current_end))
                    current_text = ""
                    current_start = None
                    current_end = None
                if current_start is None:
                    current_start = start
                current_text += raw
                if end is not None:
                    current_end = end
            if current_text.strip() and current_start is not None and current_end is not None and current_end > current_start:
                timings.append(WordTiming(_safe_subtitle_text(current_text), current_start, current_end))
        return timings

    def _whisper_cpp_timings(
        self,
        audio_path: Path,
        *,
        model_name: str,
    ) -> list[WordTiming]:
        try:
            model_root = config.resolve_drive_path("models", "whisper")
        except (RuntimeError, ValueError):
            return []
        normalized = Path(model_name)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("model_name must be a Drive-relative Whisper model name")
        model_candidates = [
            config.resolve_drive_path("models", "whisper", model_name),
            config.resolve_drive_path("models", "whisper", f"ggml-{model_name}.bin"),
        ]
        model_path = next((candidate for candidate in model_candidates if candidate.is_file()), None)
        if model_path is None:
            return []
        binary = shutil.which(config.WHISPER_CPP_BINARY)
        if binary is None:
            candidate_binary = model_root / config.WHISPER_CPP_BINARY
            if candidate_binary.is_file():
                binary = str(candidate_binary)
        if binary is None:
            return []
        config.ensure_runtime_dirs()
        output_prefix = config.TEMP_DIR / f"whisper-{uuid.uuid4().hex}"
        command = [
            binary, "-m", str(model_path), "-f", str(audio_path),
            *config.whisper_cpp_runtime_args(), "-oj", "-of", str(output_prefix),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=1800)
            json_path = output_prefix.with_suffix(".json")
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return []
        finally:
            output_prefix.with_suffix(".json").unlink(missing_ok=True)
        if not isinstance(payload, Mapping):
            return []
        return self._whisper_cpp_words(payload)

    def _faster_whisper_timings(
        self,
        audio_path: Path,
        *,
        model_name: str,
        language: str | None,
    ) -> list[WordTiming]:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError:
            return []
        try:
            model_root = config.resolve_drive_path("models", "whisper")
        except (RuntimeError, ValueError):
            return []
        candidates = [model_root / model_name, model_root / f"faster-whisper-{model_name}"]
        model_path = next((candidate for candidate in candidates if candidate.is_dir()), None)
        if model_path is None:
            return []
        cache_key = str(model_path.resolve())
        with self._whisper_lock:
            model = self._whisper_models.get(cache_key)
            if model is None:
                cache = config.resolve_drive_path("models", "whisper", "huggingface")
                cache.mkdir(parents=True, exist_ok=True)
                os.environ["HF_HOME"] = str(cache)
                try:
                    model = WhisperModel(
                        str(model_path), device="cpu", compute_type="int8",
                        cpu_threads=config.WHISPER_CPP_THREADS, num_workers=1,
                        download_root=str(cache), local_files_only=True,
                    )
                except Exception:
                    return []
                self._whisper_models[cache_key] = model
        try:
            segments, _info = model.transcribe(
                str(audio_path), language=language, word_timestamps=True,
                beam_size=1, vad_filter=False,
            )
            timings: list[WordTiming] = []
            for segment in segments:
                for word in getattr(segment, "words", None) or []:
                    text_value = _safe_subtitle_text(str(getattr(word, "word", "")))
                    start, end = getattr(word, "start", None), getattr(word, "end", None)
                    if text_value and isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
                        timings.append(WordTiming(text_value, float(start), float(end)))
            return timings
        except Exception:
            return []

    def transcribe_audio(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        whisper_model: str = "base.en",
    ) -> dict[str, Any]:
        """Transcribe a local audio chunk with Drive-local Whisper models only."""
        source = Path(audio_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"audio_path is not a file: {source}")
        timings = self._whisper_cpp_timings(source, model_name=whisper_model)
        backend = "whisper.cpp-vulkan" if timings else ""
        if not timings:
            timings = self._faster_whisper_timings(source, model_name=whisper_model, language=language)
            if timings:
                backend = "faster-whisper-cpu"
        if not timings:
            raise SubtitleError("No installed Drive-local Whisper model is available for audio transcription")
        text = " ".join(item.word for item in timings).strip()
        if not text:
            raise SubtitleError("Whisper returned no transcript text")
        return {
            "text": re.sub(r"\s+", " ", text),
            "word_timings": timings,
            "backend": backend,
            "language": language,
        }

    def word_timings_from_audio(
        self,
        audio_path: str | Path,
        text: str,
        *,
        duration_seconds: float | None = None,
        language: str | None = None,
        whisper_model: str = "base.en",
        prefer_whisper: bool = True,
    ) -> list[WordTiming]:
        """Use Drive-local Vulkan whisper.cpp / CPU faster-whisper when present.

        No weights are downloaded. If the binary, local model, or optional Python
        runtime is absent, deterministic syllable-weight timing is returned.
        """
        source = Path(audio_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"audio_path is not a file: {source}")
        if duration_seconds is None:
            try:
                from backend.audio_engine import AudioEngine

                duration_seconds = AudioEngine().probe_audio(source).duration_seconds
            except Exception as exc:
                raise ValueError("duration_seconds is required when audio metadata cannot be read") from exc
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text cannot be empty")
        if prefer_whisper:
            timings = self._whisper_cpp_timings(source, model_name=whisper_model)
            if not timings:
                timings = self._faster_whisper_timings(source, model_name=whisper_model, language=language)
            if timings:
                normalized = self.normalize_word_timings(timings, duration_seconds=duration_seconds)
                expected_words = _TOKEN_RE.findall(text.strip())
                if len(normalized) == len(expected_words):
                    return [
                        WordTiming(word, item.start, item.end, item.highlight_color)
                        for word, item in zip(expected_words, normalized)
                    ]
        return self.estimate_word_timings(text, duration_seconds)

    @staticmethod
    def _raw_timing_end(item: WordTiming | Mapping[str, Any]) -> float:
        if isinstance(item, WordTiming):
            return float(item.end)
        if isinstance(item, Mapping):
            return float(item.get("end", item.get("end_seconds", 0.0)))
        raise ValueError("word timings must be WordTiming objects or mappings")

    @staticmethod
    def normalize_word_timings(
        word_timings: Sequence[WordTiming | Mapping[str, Any]],
        *,
        duration_seconds: float,
    ) -> list[WordTiming]:
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if isinstance(word_timings, (str, bytes)) or not isinstance(word_timings, Sequence):
            raise ValueError("word_timings must be a sequence")
        parsed: list[WordTiming] = []
        for index, item in enumerate(word_timings):
            if isinstance(item, WordTiming):
                word, start, end, color = item.word, item.start, item.end, item.highlight_color
            elif isinstance(item, Mapping):
                word = str(item.get("word", item.get("text", ""))).strip()
                try:
                    start = float(item.get("start", item.get("start_seconds")))
                    end = float(item.get("end", item.get("end_seconds")))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"word_timings[{index}] needs numeric start/end values") from exc
                color = item.get("highlight_color")
            else:
                raise ValueError(f"word_timings[{index}] must be WordTiming or a mapping")
            word = _safe_subtitle_text(str(word))
            if not word:
                continue
            try:
                start, end = float(start), float(end)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"word_timings[{index}] start/end must be numeric") from exc
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError(f"word_timings[{index}] must satisfy 0 <= start < end")
            start = min(start, duration_seconds)
            end = min(end, duration_seconds)
            if end <= start:
                continue
            if color is not None:
                color = _color_hex(str(color))
            parsed.append(WordTiming(word, start, end, color))
        parsed.sort(key=lambda timing: (timing.start, timing.end))
        normalized: list[WordTiming] = []
        previous_end = 0.0
        for item in parsed:
            start = max(item.start, previous_end)
            end = max(start + 0.01, item.end)
            end = min(end, duration_seconds)
            if end <= start:
                continue
            normalized.append(WordTiming(item.word, start, end, item.highlight_color))
            previous_end = end
        return normalized

    def caption_text(self, text: str, *, language_mode: str = "english", use_brain: bool = True) -> str:
        mode = language_mode.strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {"romanurdu": "roman_urdu", "romanhindi": "roman_hindi"}
        mode = aliases.get(mode, mode)
        if mode not in {"english", "roman_urdu", "roman_hindi"}:
            raise ValueError("language_mode must be english, roman_urdu, or roman_hindi")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text cannot be empty")
        if not use_brain:
            if mode != "english":
                raise SubtitleError("Roman Urdu/Hindi conversion requires Brain or supplied pre-translated text")
            return _safe_subtitle_text(text)
        try:
            result = self.brain.generate_captions(text, mode=mode)
        except AIProviderUnavailable as exc:
            if mode == "english":
                return _safe_subtitle_text(text)
            raise SubtitleError("No AI provider is available for Roman Urdu/Hindi captions") from exc
        caption = result.get("caption") if isinstance(result, Mapping) else None
        if not isinstance(caption, str) or not caption.strip():
            raise SubtitleError("Brain.generate_captions returned empty caption text")
        return _safe_subtitle_text(caption)

    def _group_words(
        self,
        timings: Sequence[WordTiming],
        *,
        max_words_per_event: int,
        max_event_seconds: float,
    ) -> list[list[WordTiming]]:
        if not 1 <= max_words_per_event <= 12:
            raise ValueError("max_words_per_event must be between 1 and 12")
        if not 0.3 <= max_event_seconds <= 8.0:
            raise ValueError("max_event_seconds must be between 0.3 and 8 seconds")
        groups: list[list[WordTiming]] = []
        current: list[WordTiming] = []
        for item in timings:
            current_duration = item.end - current[0].start if current else 0.0
            punctuation_break = bool(current and re.search(r"[.!?]$", current[-1].word))
            if current and (
                len(current) >= max_words_per_event
                or item.end - current[0].start > max_event_seconds
                or punctuation_break
            ):
                groups.append(current)
                current = []
            current.append(item)
        if current:
            groups.append(current)
        return groups

    def build_ass(
        self,
        word_timings: Sequence[WordTiming | Mapping[str, Any]],
        *,
        resolution: tuple[int, int] = (1080, 1920),
        base_color: str = "#F4EFE6",
        highlight_colors: Sequence[str] = ("#D6AD55", "#C94A4A"),
        safe_margin_px: int | None = None,
        font_name: str = config.UI_FONT_SANS,
        font_size: int | None = None,
        max_words_per_event: int = 6,
        max_event_seconds: float = 2.6,
        duration_seconds: float | None = None,
    ) -> str:
        width, height = resolution
        if (width, height) not in {(1080, 1920), (1440, 2560)}:
            raise ValueError("ASS output resolution must be 1080x1920 or 1440x2560")
        base = _color_hex(base_color)
        highlights = tuple(_color_hex(color) for color in highlight_colors)
        if not highlights:
            raise ValueError("highlight_colors must contain at least one approved color")
        if base not in {"#F4EFE6", "#D6AD55", "#2B5B84", "#C94A4A"}:
            raise ValueError("base_color must be ivory, brass, steel blue, or crimson")
        margin = self.safe_zone_margin(height, safe_margin_px)
        nominal_font = font_size or int(round(64 * height / 1920))
        if not 24 <= nominal_font <= 160:
            raise ValueError("font_size must be between 24 and 160")
        if not font_name or any(char in font_name for char in ",\r\n"):
            raise ValueError("font_name must be a non-empty ASS-safe font family name")
        if not word_timings:
            raise ValueError("word_timings cannot be empty")
        inferred_duration = duration_seconds if duration_seconds is not None else max(self._raw_timing_end(item) for item in word_timings)
        timings = self.normalize_word_timings(word_timings, duration_seconds=float(inferred_duration))
        groups = self._group_words(timings, max_words_per_event=max_words_per_event, max_event_seconds=max_event_seconds)
        style_primary = _ass_color(base)
        secondary = _ass_color(highlights[0])
        outline = _ass_color("#121316")
        back = _ass_color("#121316", alpha=80)
        lines = [
            "[Script Info]",
            "; Script generated by DaemonCut SubtitleEngine",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
            f"Style: Glow,{font_name},{nominal_font},{style_primary},{secondary},{outline},{back},1,0,0,0,100,100,0,0,1,13,0,2,80,80,{margin},1",
            f"Style: Karaoke,{font_name},{nominal_font},{style_primary},{secondary},{outline},{back},1,0,0,0,100,100,0,0,1,4,2,2,80,80,{margin},1",
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        ]
        for group_index, group in enumerate(groups):
            event_start = group[0].start
            event_end = min(float(inferred_duration), group[-1].end + 0.12)
            encoded_words: list[str] = []
            cursor = event_start
            for word_index, timing in enumerate(group):
                gap_cs = max(0, int(round((timing.start - cursor) * 100)))
                if gap_cs:
                    encoded_words.append(f"{{\\k{gap_cs}}} ")
                color = timing.highlight_color or highlights[(group_index + word_index) % len(highlights)]
                color_tag = _ass_color(_color_hex(color))
                duration_cs = max(1, int(round((timing.end - timing.start) * 100)))
                rel_start_ms = max(0, int(round((timing.start - event_start) * 1000)))
                rel_end_ms = max(rel_start_ms + 1, int(round((timing.end - event_start) * 1000)))
                bounce = (
                    f"\\t({rel_start_ms},{rel_end_ms},\\fscx108\\fscy108)"
                    f"\\t({rel_end_ms},{rel_end_ms + 90},\\fscx100\\fscy100)"
                )
                encoded_words.append(f"{{\\2c{color_tag}\\kf{duration_cs}{bounce}}}{_safe_subtitle_text(timing.word)}")
                cursor = timing.end
            event_text = " ".join(part.strip() for part in encoded_words if part.strip())
            start_stamp, end_stamp = _ass_time(event_start), _ass_time(event_end)
            lines.append(f"Dialogue: 0,{start_stamp},{end_stamp},Glow,,0,0,{margin},,{{\\blur4\\bord13}}{event_text}")
            lines.append(f"Dialogue: 1,{start_stamp},{end_stamp},Karaoke,,0,0,{margin},,{{\\bord4\\shad2}}{event_text}")
        return "\n".join(lines) + "\n"

    def build_srt(
        self,
        word_timings: Sequence[WordTiming | Mapping[str, Any]],
        *,
        duration_seconds: float | None = None,
        max_words_per_cue: int = 8,
        max_cue_seconds: float = 4.0,
    ) -> str:
        if not word_timings:
            raise ValueError("word_timings cannot be empty")
        if duration_seconds is None:
            duration_seconds = max(
                float(item.end if isinstance(item, WordTiming) else item.get("end", item.get("end_seconds", 0.0)))
                for item in word_timings
            )
        timings = self.normalize_word_timings(word_timings, duration_seconds=float(duration_seconds))
        groups = self._group_words(timings, max_words_per_event=max_words_per_cue, max_event_seconds=max_cue_seconds)
        blocks: list[str] = []
        for index, group in enumerate(groups, start=1):
            start_time = group[0].start
            end_time = min(float(duration_seconds), group[-1].end)
            text = " ".join(_safe_subtitle_text(item.word) for item in group)
            blocks.append(f"{index}\n{_srt_time(start_time)} --> {_srt_time(end_time)}\n{text}")
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    @staticmethod
    def _prepare_output(path: str | Path | None, *, extension: str) -> Path:
        if path is None:
            target = config.resolve_drive_path("renders_archive", f"captions_{uuid.uuid4().hex}{extension}")
        else:
            target = Path(path).expanduser().resolve(strict=False)
        allowed = (
            config.resolve_drive_path("renders_archive"),
            config.TEMP_DIR.resolve(strict=False),
            config.CACHE_DIR.resolve(strict=False),
        )
        if not any(_is_within(target, root) for root in allowed):
            raise SubtitleError("Subtitle sidecars must be stored in Google Drive renders_archive or approved transient folders")
        if target.suffix.casefold() != extension:
            raise ValueError(f"Output path must end in {extension}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def export_sidecars(
        self,
        text: str,
        *,
        duration_seconds: float,
        word_timings: Sequence[WordTiming | Mapping[str, Any]] | None = None,
        language_mode: str = "english",
        resolution: tuple[int, int] = (1080, 1920),
        output_ass: str | Path | None = None,
        output_srt: str | Path | None = None,
        audio_path: str | Path | None = None,
        whisper_model: str = "base.en",
        whisper_language: str | None = None,
        prefer_whisper: bool = True,
        use_brain: bool = True,
        base_color: str = "#F4EFE6",
        highlight_colors: Sequence[str] = ("#D6AD55", "#C94A4A"),
    ) -> SubtitleSidecars:
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be a positive finite number")
        caption = self.caption_text(text, language_mode=language_mode, use_brain=use_brain)
        if word_timings:
            timings = self.normalize_word_timings(word_timings, duration_seconds=duration_seconds)
            expected_words = _TOKEN_RE.findall(caption)
            if len(timings) != len(expected_words):
                timings = self.estimate_word_timings(caption, duration_seconds)
            else:
                timings = [
                    WordTiming(word, timing.start, timing.end, timing.highlight_color)
                    for word, timing in zip(expected_words, timings)
                ]
        elif audio_path is not None:
            timings = self.word_timings_from_audio(
                audio_path,
                caption,
                duration_seconds=duration_seconds,
                language=whisper_language,
                whisper_model=whisper_model,
                prefer_whisper=prefer_whisper,
            )
        else:
            timings = self.estimate_word_timings(caption, duration_seconds)
        if output_ass is None and output_srt is None:
            basename = f"captions_{uuid.uuid4().hex}"
            ass_target = self._prepare_output(config.resolve_drive_path("renders_archive", basename + ".ass"), extension=".ass")
            srt_target = self._prepare_output(config.resolve_drive_path("renders_archive", basename + ".srt"), extension=".srt")
        else:
            ass_target = self._prepare_output(output_ass, extension=".ass") if output_ass is not None else self._prepare_output(str(output_srt).rsplit(".", 1)[0] + ".ass", extension=".ass")
            srt_target = self._prepare_output(output_srt, extension=".srt") if output_srt is not None else ass_target.with_suffix(".srt")
            if not srt_target.parent.exists():
                srt_target.parent.mkdir(parents=True, exist_ok=True)
        ass_target.write_text(
            self.build_ass(
                timings,
                resolution=resolution,
                base_color=base_color,
                highlight_colors=highlight_colors,
                duration_seconds=duration_seconds,
            ),
            encoding="utf-8",
        )
        srt_target.write_text(self.build_srt(timings, duration_seconds=duration_seconds), encoding="utf-8")
        margin = self.safe_zone_margin(resolution[1])
        return SubtitleSidecars(
            ass_path=ass_target,
            srt_path=srt_target,
            caption_text=caption,
            language_mode=language_mode,
            word_count=len(timings),
            safe_margin_px=margin,
        )


__all__ = ["SubtitleEngine", "SubtitleError", "SubtitleSidecars", "WordTiming"]
