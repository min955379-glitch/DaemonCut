"""PeakCut ingestion, highlight scoring, and vertical-short orchestration.

Downloaded VODs and rendered masters are archived under the configured Drive
workspace. Only disposable analysis data belongs under ``config.TEMP_DIR``.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from backend import config
from backend.brain_gemini import AIProviderUnavailable, AIResponseError, Brain
from backend.subtitle_engine import SubtitleEngine, SubtitleSidecars
from backend.thumbnail_maker import ThumbnailMaker, ThumbnailResult
from backend.video_editor import ClipSpec, FramingMode, RenderResult, VideoEditor, VideoEditorError


class ClipperError(RuntimeError):
    """Base exception for ingestion, scoring, and short rendering."""


class ClipperDependencyUnavailable(ClipperError):
    """An optional downloader, FFmpeg binary, or AI provider is not available."""


class ClipperValidationError(ClipperError, ValueError):
    """An unsafe URL, duration, or render request was rejected."""


@dataclass(frozen=True)
class IngestedMedia:
    path: Path
    source_url: str | None
    title: str
    duration_seconds: float
    source_kind: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class RMSWindow:
    start_seconds: float
    end_seconds: float
    rms: float
    dbfs: float


@dataclass(frozen=True)
class HighlightSegment:
    start_seconds: float
    end_seconds: float
    transcript: str
    audio_score: float
    hook_score: float
    viral_score: float
    explanation: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class VerticalShortResult:
    highlight: HighlightSegment
    render: RenderResult
    subtitles: SubtitleSidecars | None
    thumbnails: ThumbnailResult | None


_ALLOWED_HOSTS = frozenset(
    {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
        "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
        "twitch.tv", "www.twitch.tv", "m.twitch.tv", "clips.twitch.tv",
        "kick.com", "www.kick.com", "m.kick.com",
    }
)
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts"})
_HOOK_CUES = (
    "wait", "what", "why", "how", "finally", "unexpected", "secret", "impossible",
    "revealed", "fails", "failed", "never", "first", "last", "surprise", "plot twist",
    "turns out", "but then", "until", "watch this", "did you see", "the answer",
)


def _dbfs(value: float) -> float:
    return -120.0 if value <= 0 else 20.0 * math.log10(value)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def validate_public_video_url(url: str) -> str:
    """Allow only known public platforms; reject credentials and local hosts."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2_048:
        raise ClipperValidationError("url must be a non-empty public platform URL under 2048 characters")
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ClipperValidationError("Only credential-free http(s) links to supported public platforms are accepted")
    if host not in _ALLOWED_HOSTS:
        raise ClipperValidationError("Supported ingestion hosts are YouTube, Twitch, and Kick")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ClipperValidationError("URL port is malformed") from exc
    if port not in {None, 80, 443}:
        raise ClipperValidationError("Non-standard URL ports are not allowed")
    return url.strip()


class ClipperEngine:
    """Safely ingest VODs, rank highlight windows, and render vertical Shorts."""

    def __init__(
        self,
        *,
        brain: Brain | None = None,
        video_editor: VideoEditor | None = None,
        subtitle_engine: SubtitleEngine | None = None,
        thumbnail_maker: ThumbnailMaker | None = None,
        ydl_factory: Any = None,
    ) -> None:
        self.brain = brain or Brain()
        self.video_editor = video_editor or VideoEditor()
        self.subtitle_engine = subtitle_engine or SubtitleEngine(brain=self.brain)
        self.thumbnail_maker = thumbnail_maker or ThumbnailMaker(brain=self.brain, video_editor=self.video_editor)
        self._ydl_factory = ydl_factory

    def _ydl(self, options: Mapping[str, Any]) -> Any:
        factory = self._ydl_factory
        if factory is None:
            try:
                import yt_dlp  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ClipperDependencyUnavailable("yt-dlp is not installed; install it from requirements.txt") from exc
            factory = yt_dlp.YoutubeDL
        return factory(dict(options))

    @staticmethod
    def _duration(info: Mapping[str, Any]) -> float | None:
        raw = info.get("duration")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    @staticmethod
    def _public_info(info: Mapping[str, Any]) -> dict[str, Any]:
        allowed = (
            "id", "title", "description", "duration", "uploader", "channel", "channel_id",
            "webpage_url", "extractor_key", "live_status", "is_live", "upload_date",
        )
        return {key: info.get(key) for key in allowed if info.get(key) is not None}

    @staticmethod
    def _set_download_range(options: dict[str, Any], start: float, end: float) -> None:
        try:
            from yt_dlp.utils import download_range_func  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ClipperDependencyUnavailable("This yt-dlp build lacks bounded download ranges") from exc
        options["download_ranges"] = download_range_func(None, [(start, end)])
        options["force_keyframes_at_cuts"] = True

    def ingest_url(
        self,
        url: str,
        *,
        start_seconds: float = 0.0,
        duration_seconds: float | None = None,
        workspace_id: str | None = None,
    ) -> IngestedMedia:
        """Download a bounded VOD or explicitly time-bounded live segment to Drive."""
        safe_url = validate_public_video_url(url)
        if isinstance(start_seconds, bool):
            raise ClipperValidationError("start_seconds must be numeric")
        try:
            start = float(start_seconds)
        except (TypeError, ValueError) as exc:
            raise ClipperValidationError("start_seconds must be numeric") from exc
        if not math.isfinite(start) or start < 0:
            raise ClipperValidationError("start_seconds must be finite and non-negative")
        requested_duration: float | None = None
        if duration_seconds is not None:
            if isinstance(duration_seconds, bool):
                raise ClipperValidationError("duration_seconds must be numeric")
            try:
                requested_duration = float(duration_seconds)
            except (TypeError, ValueError) as exc:
                raise ClipperValidationError("duration_seconds must be numeric") from exc
            if not math.isfinite(requested_duration) or requested_duration < 15:
                raise ClipperValidationError("duration_seconds must be finite and at least 15 seconds")
        download_root = config.resolve_drive_path("source_media", "vods")
        if workspace_id:
            workspace_component = re.sub(r"[^A-Za-z0-9_-]+", "-", workspace_id).strip("-_")[:80]
            if not workspace_component:
                raise ClipperValidationError("workspace_id contains no safe characters")
            download_root = config.resolve_drive_path("source_media", "workspaces", workspace_component, "vods")
        download_root.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "concurrent_fragment_downloads": config.YT_DLP_FRAGMENT_WORKERS,
            "max_filesize": config.YT_DLP_MAX_FILESIZE_BYTES,
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "outtmpl": str(download_root / "%(extractor_key)s_%(id)s.%(ext)s"),
            "overwrites": False,
            "continuedl": True,
        }
        try:
            with self._ydl(options) as downloader:
                info = downloader.extract_info(safe_url, download=False)
            if not isinstance(info, Mapping):
                raise ClipperError("yt-dlp returned malformed metadata")
            live = bool(info.get("is_live")) or info.get("live_status") in {"is_live", "is_upcoming"}
            duration = self._duration(info)
            if live:
                if requested_duration is None:
                    raise ClipperValidationError("Live-stream capture requires an explicit duration_seconds limit")
                live_duration = requested_duration
                if not 15 <= live_duration <= min(3_600, config.YT_DLP_MAX_DURATION_SECONDS):
                    raise ClipperValidationError("Live capture duration must be 15–3600 seconds and within the configured cap")
                options["skip_download"] = False
                self._set_download_range(options, start, start + live_duration)
                duration = live_duration
            else:
                if duration is None:
                    raise ClipperValidationError("VOD metadata has no finite duration")
                if duration > config.YT_DLP_MAX_DURATION_SECONDS:
                    raise ClipperValidationError(
                        f"VOD is {duration:.0f}s; configured maximum is {config.YT_DLP_MAX_DURATION_SECONDS}s"
                    )
                if requested_duration is not None:
                    if requested_duration > config.YT_DLP_MAX_DURATION_SECONDS:
                        raise ClipperValidationError("duration_seconds exceeds the configured download cap")
                    if start + requested_duration > duration:
                        raise ClipperValidationError("requested range extends beyond the VOD duration")
                    self._set_download_range(options, start, start + requested_duration)
                    duration = requested_duration
                elif start > 0:
                    raise ClipperValidationError("start_seconds requires a bounded duration_seconds range for a VOD")
                options["skip_download"] = False
            options["max_downloads"] = 1
            with self._ydl(options) as downloader:
                downloaded = downloader.extract_info(safe_url, download=True)
                candidates: list[Path] = []
                if isinstance(downloaded, Mapping):
                    requested_downloads = downloaded.get("requested_downloads", [])
                    for item in requested_downloads if isinstance(requested_downloads, list) else []:
                        if isinstance(item, Mapping) and item.get("filepath"):
                            candidates.append(Path(str(item["filepath"])))
                    try:
                        candidates.append(Path(downloader.prepare_filename(downloaded)))
                    except (AttributeError, OSError, TypeError):
                        pass
            candidates.extend(sorted(download_root.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True))
            media_path = next((path.resolve(strict=True) for path in candidates if path.is_file()), None)
            if media_path is None or not _is_within(media_path, download_root):
                raise ClipperError("yt-dlp completed but no Drive-contained media file was found")
            if media_path.stat().st_size > config.YT_DLP_MAX_FILESIZE_BYTES:
                media_path.unlink(missing_ok=True)
                raise ClipperValidationError("Downloaded file exceeds the configured size limit")
            final_duration = duration or self.video_editor.probe_duration(media_path)
            return IngestedMedia(
                path=media_path,
                source_url=safe_url,
                title=str(info.get("title") or media_path.stem)[:300],
                duration_seconds=float(final_duration),
                source_kind="live_capture" if live else "vod",
                metadata=self._public_info(info),
            )
        except (ClipperError, ClipperValidationError):
            raise
        except Exception as exc:
            raise ClipperDependencyUnavailable(f"yt-dlp ingestion failed: {type(exc).__name__}: {str(exc)[:300]}") from exc

    def ingest_local_file(self, file_path: str | Path, *, title: str | None = None) -> IngestedMedia:
        """Copy a local input into Drive, retaining the user's original file."""
        source = Path(file_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.casefold() not in _VIDEO_SUFFIXES:
            raise ClipperValidationError(f"Unsupported local video file: {source}")
        if source.stat().st_size > config.YT_DLP_MAX_FILESIZE_BYTES:
            raise ClipperValidationError("Local video exceeds the configured size limit")
        drive_root = config.resolve_drive_path("source_media", "vods")
        drive_root.mkdir(parents=True, exist_ok=True)
        if _is_within(source, drive_root):
            destination = source
        else:
            destination = drive_root / f"local_{uuid.uuid4().hex}{source.suffix.casefold()}"
            shutil.copy2(source, destination)
        try:
            duration = self.video_editor.probe_duration(destination)
        except VideoEditorError as exc:
            if destination != source:
                destination.unlink(missing_ok=True)
            raise ClipperDependencyUnavailable(f"Could not inspect local media duration: {exc}") from exc
        if duration > config.YT_DLP_MAX_DURATION_SECONDS:
            if destination != source:
                destination.unlink(missing_ok=True)
            raise ClipperValidationError("Local video exceeds the configured duration limit")
        return IngestedMedia(
            path=destination,
            source_url=None,
            title=(title or source.stem)[:300],
            duration_seconds=duration,
            source_kind="local_file",
            metadata={"file_size_bytes": destination.stat().st_size, "suffix": destination.suffix.casefold()},
        )

    @staticmethod
    def build_audio_energy_command(path: str | Path, *, ffmpeg: str | None = None, sample_rate: int = 8_000) -> list[str]:
        if not 1_000 <= sample_rate <= 16_000:
            raise ValueError("sample_rate must be between 1000 and 16000")
        source = Path(path).expanduser().resolve(strict=True)
        return [
            ffmpeg or config.FFMPEG_BINARY,
            "-hide_banner", "-loglevel", "error", "-threads", str(config.FFMPEG_THREADS),
            "-i", str(source), "-vn", *config.ffmpeg_filter_args(),
            "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
        ]

    @staticmethod
    def _rms_window(pcm_s16le: bytes | bytearray, *, sample_rate: int, start_sample: int) -> RMSWindow | None:
        usable = len(pcm_s16le) // 2 * 2
        count = usable // 2
        if count == 0:
            return None
        total_square = sum(sample[0] * sample[0] for sample in struct.iter_unpack("<h", memoryview(pcm_s16le)[:usable]))
        rms = math.sqrt(total_square / count) / 32768.0
        start = start_sample / sample_rate
        end = (start_sample + count) / sample_rate
        return RMSWindow(start, end, rms, _dbfs(rms))

    @staticmethod
    def rms_windows(pcm_s16le: bytes, *, sample_rate: int = 8_000, window_seconds: float = 1.0) -> list[RMSWindow]:
        if sample_rate <= 0 or not math.isfinite(window_seconds) or not 0.1 <= window_seconds <= 10.0:
            raise ValueError("sample_rate and window_seconds are out of range")
        usable = len(pcm_s16le) // 2 * 2
        if usable < 2:
            return []
        window_samples = max(1, int(round(sample_rate * window_seconds)))
        window_bytes = window_samples * 2
        windows: list[RMSWindow] = []
        # Keep memory bounded to one window even when this helper is given a
        # complete multi-hour PCM buffer by a caller.
        for begin in range(0, usable, window_bytes):
            result = ClipperEngine._rms_window(
                pcm_s16le[begin : min(usable, begin + window_bytes)],
                sample_rate=sample_rate,
                start_sample=begin // 2,
            )
            if result is not None:
                windows.append(result)
        return windows

    def analyze_audio(self, video_path: str | Path, *, window_seconds: float = 1.0) -> list[RMSWindow]:
        """Stream FFmpeg PCM into one-second RMS windows without buffering a VOD."""
        source = Path(video_path).expanduser().resolve(strict=True)
        duration = self.video_editor.probe_duration(source)
        if duration > config.YT_DLP_MAX_DURATION_SECONDS:
            raise ClipperValidationError("Input video exceeds the configured duration cap")
        if not math.isfinite(window_seconds) or not 0.1 <= window_seconds <= 10.0:
            raise ValueError("window_seconds must be between 0.1 and 10 seconds")
        sample_rate = 8_000
        window_samples = max(1, int(round(sample_rate * window_seconds)))
        window_bytes = window_samples * 2
        command = self.build_audio_energy_command(source, sample_rate=sample_rate)
        timeout = max(120, min(7_200, int(duration * 2)))
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise ClipperDependencyUnavailable(f"FFmpeg executable {command[0]!r} was not found") from exc
        if process.stdout is None:
            process.kill()
            raise ClipperError("FFmpeg did not provide an audio-analysis output stream")
        windows: list[RMSWindow] = []
        read_errors: list[BaseException] = []

        def consume_pcm() -> None:
            pending = bytearray()
            sample_cursor = 0
            try:
                while True:
                    chunk = process.stdout.read(65_536)
                    if not chunk:
                        break
                    pending.extend(chunk)
                    while len(pending) >= window_bytes:
                        current = pending[:window_bytes]
                        del pending[:window_bytes]
                        item = self._rms_window(current, sample_rate=sample_rate, start_sample=sample_cursor)
                        if item is not None:
                            windows.append(item)
                            sample_cursor += window_samples
                usable = len(pending) // 2 * 2
                if usable:
                    item = self._rms_window(pending[:usable], sample_rate=sample_rate, start_sample=sample_cursor)
                    if item is not None:
                        windows.append(item)
            except BaseException as exc:
                read_errors.append(exc)

        reader = threading.Thread(target=consume_pcm, name="DaemonCutRMSReader", daemon=True)
        reader.start()
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            reader.join(timeout=5.0)
            raise ClipperError("FFmpeg audio analysis exceeded its bounded timeout") from exc
        finally:
            reader.join(timeout=5.0)
            if reader.is_alive():
                process.kill()
                if process.stdout:
                    process.stdout.close()
                reader.join(timeout=1.0)
            elif process.stdout:
                process.stdout.close()
        if reader.is_alive():
            raise ClipperError("FFmpeg RMS reader did not stop after the encoder exited")
        if read_errors:
            raise ClipperError(f"Could not parse FFmpeg PCM output: {type(read_errors[0]).__name__}") from read_errors[0]
        if return_code != 0:
            raise ClipperError(f"FFmpeg audio analysis failed with exit code {return_code}")
        if not windows:
            raise ClipperError("Input media has no decodable audio samples")
        return windows

    @staticmethod
    def _transcript_text(segments: Sequence[Mapping[str, Any]], start: float, end: float) -> str:
        pieces: list[str] = []
        for segment in segments:
            try:
                segment_start = float(segment.get("start", 0.0))
                segment_end = float(segment.get("end", segment_start))
            except (TypeError, ValueError):
                continue
            text = segment.get("text", segment.get("narration", ""))
            if isinstance(text, str) and segment_end >= start and segment_start <= end:
                pieces.append(text.strip())
        return re.sub(r"\s+", " ", " ".join(piece for piece in pieces if piece)).strip()

    @staticmethod
    def heuristic_hook_score(text: str) -> float:
        """Score novelty, conflict, emotion, and question/answer cues offline."""
        if not text.strip():
            return 0.0
        normalized = text.casefold()
        score = 8.0
        score += 24.0 if "?" in text else 0.0
        score += 18.0 if "!" in text else 0.0
        score += min(24.0, sum(8.0 for cue in _HOOK_CUES if cue in normalized))
        words = re.findall(r"[A-Za-z0-9']+", normalized)
        unique_ratio = len(set(words)) / max(1, len(words))
        score += min(14.0, unique_ratio * 14.0)
        score += min(12.0, len(words) / 12.0 * 12.0)
        return max(0.0, min(100.0, score))

    @staticmethod
    def _audio_score(windows: Sequence[RMSWindow], start: float, end: float) -> float:
        selected = [window for window in windows if window.end_seconds > start and window.start_seconds < end]
        if not selected:
            return 0.0
        values = sorted(max(0.0, min(1.0, (window.dbfs + 55.0) / 55.0)) for window in selected)
        peak = values[-1]
        average = sum(values) / len(values)
        upper = values[max(0, int(0.8 * (len(values) - 1)))]
        return max(0.0, min(100.0, (0.45 * average + 0.35 * upper + 0.20 * peak) * 100))

    def _brain_hook_scores(self, candidates: Sequence[HighlightSegment]) -> dict[int, tuple[float, str]]:
        if not candidates:
            return {}
        payload = [
            {"index": index, "transcript": segment.transcript[:600], "duration_seconds": round(segment.duration_seconds, 2)}
            for index, segment in enumerate(candidates[:12])
            if segment.transcript
        ]
        if not payload:
            return {}
        try:
            response = self.brain.generate_json(
                "Score short-video highlight hooks for curiosity, emotional clarity, novelty, and payoff. "
                "Do not reward copied slogans or claims not supported by the transcript. Return a JSON array "
                "of objects with index (integer), score (0-100), and reason (short string).",
                json.dumps(payload, ensure_ascii=False),
                expected_type=list,
                temperature=0.1,
            )
        except (AIProviderUnavailable, AIResponseError, ValueError, TypeError, AttributeError):
            return {}
        scores: dict[int, tuple[float, str]] = {}
        for item in response:
            if not isinstance(item, Mapping):
                continue
            try:
                index = int(item.get("index"))
                score = max(0.0, min(100.0, float(item.get("score"))))
            except (TypeError, ValueError):
                continue
            if 0 <= index < min(len(candidates), 12):
                scores[index] = (score, str(item.get("reason", "AI hook score"))[:240])
        return scores

    def find_highlights(
        self,
        video_path: str | Path,
        *,
        transcript_segments: Sequence[Mapping[str, Any]] = (),
        window_seconds: float = 30.0,
        step_seconds: float = 8.0,
        max_highlights: int = 5,
        use_brain: bool = True,
    ) -> list[HighlightSegment]:
        """Rank 15–60 second windows using loudness peaks plus transcript hooks."""
        if isinstance(window_seconds, bool) or isinstance(step_seconds, bool):
            raise ValueError("window_seconds and step_seconds must be numeric")
        try:
            window_seconds = float(window_seconds)
            step_seconds = float(step_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("window_seconds and step_seconds must be numeric") from exc
        if not math.isfinite(window_seconds) or not 15 <= window_seconds <= 60:
            raise ValueError("window_seconds must be between 15 and 60")
        if isinstance(max_highlights, bool) or not isinstance(max_highlights, int) or not 1 <= max_highlights <= 20 or not math.isfinite(step_seconds) or not 1 <= step_seconds <= 60:
            raise ValueError("max_highlights or step_seconds is outside the allowed range")
        source = Path(video_path).expanduser().resolve(strict=True)
        duration = self.video_editor.probe_duration(source)
        if duration < 15:
            raise ClipperValidationError("Video must be at least 15 seconds long for highlight scoring")
        if duration > config.YT_DLP_MAX_DURATION_SECONDS:
            raise ClipperValidationError("Video exceeds the configured maximum duration")
        window = min(float(window_seconds), duration, 60.0)
        final_start = max(0.0, duration - window)
        estimated_candidates = int(math.ceil(final_start / step_seconds)) + 1
        if estimated_candidates > 5_000:
            raise ClipperValidationError("step_seconds would create more than 5000 highlight windows; increase the step size")
        starts: list[float] = []
        cursor = 0.0
        while cursor <= final_start + 1e-9:
            starts.append(cursor)
            cursor += step_seconds
        if not starts or final_start - starts[-1] > 1e-6:
            starts.append(final_start)
        windows = self.analyze_audio(source)
        candidates: list[HighlightSegment] = []
        for start in starts:
            end = min(duration, start + window)
            transcript = self._transcript_text(transcript_segments, start, end)
            audio_score = self._audio_score(windows, start, end)
            heuristic = self.heuristic_hook_score(transcript)
            candidates.append(HighlightSegment(start, end, transcript, audio_score, heuristic, 0.65 * audio_score + 0.35 * heuristic))
        ai_scores = self._brain_hook_scores(candidates[:12]) if use_brain else {}
        scored: list[HighlightSegment] = []
        for index, candidate in enumerate(candidates):
            ai_score, reason = ai_scores.get(index, (candidate.hook_score, "Deterministic loudness and transcript heuristic"))
            hook_score = 0.55 * candidate.hook_score + 0.45 * ai_score if index in ai_scores else candidate.hook_score
            final_score = 0.62 * candidate.audio_score + 0.38 * hook_score
            scored.append(HighlightSegment(
                candidate.start_seconds, candidate.end_seconds, candidate.transcript,
                candidate.audio_score, hook_score, final_score, reason,
            ))
        scored.sort(key=lambda item: (-item.viral_score, item.start_seconds))
        selected: list[HighlightSegment] = []
        for candidate in scored:
            if all(candidate.end_seconds <= item.start_seconds or candidate.start_seconds >= item.end_seconds for item in selected):
                selected.append(candidate)
                if len(selected) >= max_highlights:
                    break
        return selected

    @staticmethod
    def suggest_hook(text: str, *, max_words: int = 3) -> str:
        if max_words != 3:
            raise ValueError("PeakCut hook overlays are limited to exactly three words")
        words = re.findall(r"\S+", re.sub(r"\s+", " ", text).strip())
        if not words:
            return "A NEW TWIST"
        # Three words max; preserves source meaning without using a long quote.
        return " ".join(words[:3]).strip(" .,!?:;")

    def render_vertical_short(
        self,
        video_path: str | Path,
        highlight: HighlightSegment,
        *,
        hook: str | None = None,
        framing_mode: str | FramingMode = FramingMode.WHOLE_BLUR,
        center_x: float = 0.5,
        language_mode: str = "english",
        use_brain_captions: bool = True,
        make_thumbnail: bool = True,
        lut_name: str | None = None,
    ) -> VerticalShortResult:
        if not 15 <= highlight.duration_seconds <= 60:
            raise ClipperValidationError("Highlight duration must be 15–60 seconds")
        source = Path(video_path).expanduser().resolve(strict=True)
        chosen_hook = hook or self.suggest_hook(highlight.transcript)
        clips = [ClipSpec(
            source,
            start_seconds=highlight.start_seconds,
            end_seconds=highlight.end_seconds,
            framing_mode=framing_mode,
            center_x=center_x,
            include_audio=True,
        )]
        render = self.video_editor.render(clips, resolution=(1080, 1920), fps=60, lut_name=lut_name)
        subtitles: SubtitleSidecars | None = None
        if highlight.transcript:
            subtitles = self.subtitle_engine.export_sidecars(
                highlight.transcript,
                duration_seconds=highlight.duration_seconds,
                language_mode=language_mode,
                resolution=(1080, 1920),
                use_brain=use_brain_captions,
            )
        thumbnail_result = self.thumbnail_maker.make_from_video(
            render.path,
            chosen_hook,
            candidate_count=5,
            use_brain=make_thumbnail,
            selection_context=highlight.transcript[:1_000],
        ) if make_thumbnail else None
        return VerticalShortResult(highlight, render, subtitles, thumbnail_result)


__all__ = [
    "ClipperDependencyUnavailable",
    "ClipperEngine",
    "ClipperError",
    "ClipperValidationError",
    "HighlightSegment",
    "IngestedMedia",
    "RMSWindow",
    "VerticalShortResult",
    "validate_public_video_url",
]
