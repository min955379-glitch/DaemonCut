"""FFmpeg video compositor tuned for the DaemonCut AMD editing workstation.

Source clips are supplied by the caller. LUTs and Real-ESRGAN models live in
Google Drive; final masters are archived to Drive. Intermediate render files
are restricted to ``config.TEMP_DIR`` and are safe for the project TTL cleaner.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend import config


class VideoEditorError(RuntimeError):
    """Base error for video planning, rendering, and archival."""


class VideoDependencyUnavailable(VideoEditorError):
    """FFmpeg or an optional GPU tool is not installed/available."""


class VideoRenderError(VideoEditorError):
    """FFmpeg or an external renderer failed."""


class FramingMode(str, Enum):
    WHOLE_BLUR = "whole_blur"
    ZOOM_BLUR = "zoom_blur"
    FULL_SCREEN = "full_screen"


@dataclass(frozen=True)
class ClipSpec:
    path: str | Path
    start_seconds: float = 0.0
    end_seconds: float | None = None
    speed: float = 1.0
    framing_mode: str | FramingMode = FramingMode.WHOLE_BLUR
    center_x: float = 0.5
    motion_effect: str = "none"
    include_audio: bool = False
    audio_gain_db: float = 0.0
    label: str = ""


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration_seconds: float
    resolution: tuple[int, int]
    fps: int
    video_encoder: str
    used_software_fallback: bool
    warnings: tuple[str, ...] = ()
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpscaleResult:
    path: Path
    method: str
    used_vulkan: bool
    warnings: tuple[str, ...] = ()
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedClip:
    source: Path
    start: float
    end: float
    speed: float
    duration: float
    framing: FramingMode
    center_x: float
    motion: str
    include_audio: bool
    audio_gain_db: float


_ALLOWED_TRANSITIONS = frozenset(
    {
        "fade",
        "fadeblack",
        "fadewhite",
        "wipeleft",
        "wiperight",
        "wipeup",
        "wipedown",
        "slideleft",
        "slideright",
        "slideup",
        "slidedown",
        "circleopen",
        "circleclose",
        "dissolve",
        "pixelize",
        "smoothleft",
        "smoothright",
        "smoothup",
        "smoothdown",
    }
)
_ALLOWED_MOTION = frozenset({"none", "punch_in", "shake"})
_RESOLUTIONS = {(1080, 1920), (1440, 2560)}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _seconds(value: float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return parsed


class VideoEditor:
    """Builds safe FFmpeg command lines and archives vertically-framed masters."""

    def __init__(self, *, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        self.ffmpeg = ffmpeg or config.FFMPEG_BINARY
        self.ffprobe = ffprobe or config.FFPROBE_BINARY

    def _resolve_ffmpeg(self) -> str:
        found = shutil.which(self.ffmpeg)
        if found:
            return found
        candidate = Path(self.ffmpeg).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise VideoDependencyUnavailable(f"FFmpeg executable {self.ffmpeg!r} was not found")

    def _resolve_ffprobe(self) -> str | None:
        found = shutil.which(self.ffprobe)
        if found:
            return found
        candidate = Path(self.ffprobe).expanduser()
        return str(candidate) if candidate.is_file() else None

    @staticmethod
    def _archive_path(path: str | Path | None = None, *, extension: str = ".mp4") -> Path:
        if path is None:
            destination = config.resolve_drive_path("renders_archive", f"render_{uuid.uuid4().hex}{extension}")
        else:
            destination = Path(path).expanduser().resolve(strict=False)
        archive_root = config.resolve_drive_path("renders_archive")
        if not _is_within(destination, archive_root):
            raise VideoEditorError("Final video masters must be archived inside Google Drive renders_archive")
        if destination.suffix.casefold() != extension:
            raise ValueError(f"Output path must end in {extension}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    @staticmethod
    def _scratch_path(*, suffix: str = ".mp4", prefix: str = "video-") -> Path:
        config.ensure_runtime_dirs()
        return config.TEMP_DIR / f"{prefix}{uuid.uuid4().hex}{suffix}"

    def probe_duration(self, path: str | Path) -> float:
        source = Path(path).expanduser().resolve(strict=True)
        ffprobe = self._resolve_ffprobe()
        if ffprobe is None:
            raise VideoDependencyUnavailable("ffprobe is required when a clip end time is not supplied")
        command = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(source),
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
            duration = float(json.loads(result.stdout)["format"]["duration"])
        except FileNotFoundError as exc:
            raise VideoDependencyUnavailable(f"ffprobe was not found: {ffprobe}") from exc
        except (subprocess.SubprocessError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise VideoRenderError(f"Could not read clip duration for {source.name}: {exc}") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise VideoRenderError(f"Clip {source.name} has no positive duration")
        return duration

    def _prepare_clips(self, clips: Sequence[ClipSpec | Mapping[str, Any]]) -> list[_PreparedClip]:
        if isinstance(clips, (str, bytes)) or not clips:
            raise ValueError("clips must be a non-empty sequence of ClipSpec objects or mappings")
        prepared: list[_PreparedClip] = []
        for index, value in enumerate(clips):
            if isinstance(value, ClipSpec):
                clip = value
            elif isinstance(value, Mapping):
                try:
                    clip = ClipSpec(**value)
                except TypeError as exc:
                    raise ValueError(f"clips[{index}] has an unknown or missing field: {exc}") from exc
            else:
                raise ValueError(f"clips[{index}] must be a ClipSpec or mapping")
            source = Path(clip.path).expanduser().resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"clips[{index}] is not a file: {source}")
            start = _seconds(clip.start_seconds, f"clips[{index}].start_seconds")
            source_duration = self.probe_duration(source) if clip.end_seconds is None else None
            end = source_duration if clip.end_seconds is None else _seconds(clip.end_seconds, f"clips[{index}].end_seconds")
            if end <= start:
                raise ValueError(f"clips[{index}] end_seconds must be greater than start_seconds")
            if source_duration is not None and end > source_duration + 0.05:
                raise ValueError(f"clips[{index}] end_seconds exceeds source duration {source_duration:.3f}s")
            try:
                speed = float(clip.speed)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"clips[{index}].speed must be numeric") from exc
            if not math.isfinite(speed) or not 0.75 <= speed <= 1.5:
                raise ValueError(f"clips[{index}].speed must be between 0.75 and 1.5")
            try:
                framing = clip.framing_mode if isinstance(clip.framing_mode, FramingMode) else FramingMode(str(clip.framing_mode).casefold())
            except ValueError as exc:
                raise ValueError(f"clips[{index}].framing_mode must be whole_blur, zoom_blur, or full_screen") from exc
            try:
                center_x = float(clip.center_x)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"clips[{index}].center_x must be numeric") from exc
            if not math.isfinite(center_x) or not 0.0 <= center_x <= 1.0:
                raise ValueError(f"clips[{index}].center_x must be between 0 and 1")
            motion = str(clip.motion_effect).casefold().strip()
            if motion not in _ALLOWED_MOTION:
                raise ValueError(f"clips[{index}].motion_effect must be one of {sorted(_ALLOWED_MOTION)}")
            try:
                audio_gain = float(clip.audio_gain_db)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"clips[{index}].audio_gain_db must be numeric") from exc
            if not math.isfinite(audio_gain) or not -48.0 <= audio_gain <= 12.0:
                raise ValueError(f"clips[{index}].audio_gain_db must be between -48 and +12 dB")
            prepared.append(
                _PreparedClip(
                    source=source,
                    start=start,
                    end=end,
                    speed=speed,
                    duration=(end - start) / speed,
                    framing=framing,
                    center_x=center_x,
                    motion=motion,
                    include_audio=bool(clip.include_audio),
                    audio_gain_db=audio_gain,
                )
            )
        audio_flags = {item.include_audio for item in prepared}
        if len(audio_flags) > 1:
            raise ValueError("Either every clip must include source audio or none may; pre-mix missing audio before editing")
        return prepared

    @staticmethod
    def _filter_path(path: str | Path) -> str:
        text = str(Path(path).resolve(strict=False))
        # Escaping for FFmpeg filter option values (not shell quoting; commands
        # are always passed as argument arrays).
        return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")

    @staticmethod
    def _framing_filters(
        label: str,
        *,
        output_label: str,
        framing: FramingMode,
        width: int,
        height: int,
        center_x: float,
        motion: str,
        fps: int,
        clip_duration: float,
    ) -> list[str]:
        filters: list[str] = []
        suffix = uuid.uuid4().hex[:6]
        if framing is FramingMode.WHOLE_BLUR:
            bg, fg, bg_scaled, fg_scaled = (f"{name}{suffix}" for name in ("bg", "fg", "bgs", "fgs"))
            filters.extend(
                [
                    f"[{label}]split=2[{bg}][{fg}]",
                    f"[{bg}]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},boxblur=24:8[{bg_scaled}]",
                    f"[{fg}]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos[{fg_scaled}]",
                    f"[{bg_scaled}][{fg_scaled}]overlay=(W-w)/2:(H-h)/2:shortest=1[{output_label}]",
                ]
            )
        elif framing is FramingMode.ZOOM_BLUR:
            bg, fg, bg_scaled, fg_zoomed = (f"{name}{suffix}" for name in ("bg", "fg", "bgs", "fgz"))
            filters.extend(
                [
                    f"[{label}]split=2[{bg}][{fg}]",
                    f"[{bg}]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},boxblur=24:8[{bg_scaled}]",
                    f"[{fg}]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,scale=iw*1.08:ih*1.08[{fg_zoomed}]",
                    f"[{bg_scaled}][{fg_zoomed}]overlay=(W-w)/2:(H-h)/2:shortest=1[{output_label}]",
                ]
            )
        else:
            filters.append(
                f"[{label}]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height}:x='(iw-ow)*{center_x:.5f}':y='(ih-oh)/2'[{output_label}]"
            )

        if motion == "punch_in":
            total_frames = max(1, int(round(clip_duration * fps)))
            zoomed = f"punch{suffix}"
            filters.append(
                f"[{output_label}]zoompan=z='min(1+0.08*on/{total_frames},1.08)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}[{zoomed}]"
            )
            filters.append(f"[{zoomed}]setsar=1,format=yuv420p[{output_label}out]")
            return filters
        if motion == "shake":
            shaken = f"shake{suffix}"
            shake_width = int(round(width * 1.04 / 2)) * 2
            shake_height = int(round(height * 1.04 / 2)) * 2
            filters.append(
                f"[{output_label}]scale={shake_width}:{shake_height}:flags=lanczos,"
                f"crop={width}:{height}:x='(iw-ow)/2+2*sin(t*13)':y='(ih-oh)/2+2*cos(t*17)'[{shaken}]"
            )
            filters.append(f"[{shaken}]setsar=1,format=yuv420p[{output_label}out]")
            return filters
        filters.append(f"[{output_label}]setsar=1,format=yuv420p[{output_label}out]")
        return filters

    def build_ffmpeg_command(
        self,
        clips: Sequence[ClipSpec | Mapping[str, Any]],
        *,
        output_path: str | Path | None = None,
        resolution: tuple[int, int] = (1080, 1920),
        fps: int = 60,
        transition: str = "fade",
        transition_duration: float = 0.0,
        j_cut_duration: float = 0.0,
        lut_name: str | None = None,
        flash_forward_seconds: float = 0.0,
        hook_scene_index: int = -1,
        seamless_loop_trim_seconds: float = 0.0,
        encoder: str = "amf",
    ) -> list[str]:
        """Construct an auditable FFmpeg command without invoking the renderer."""
        width, height = resolution
        if (width, height) not in _RESOLUTIONS:
            raise ValueError("resolution must be 1080x1920 or 1440x2560")
        try:
            fps_value = float(fps)
        except (TypeError, ValueError) as exc:
            raise ValueError("fps must be an integer from 24 to 60") from exc
        if isinstance(fps, bool) or not math.isfinite(fps_value) or not fps_value.is_integer() or not 24 <= fps_value <= 60:
            raise ValueError("fps must be an integer from 24 to 60")
        fps = int(fps_value)
        try:
            transition_duration = float(transition_duration)
            j_cut_duration = float(j_cut_duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("transition_duration and j_cut_duration must be numeric") from exc
        prepared = self._prepare_clips(clips)
        if len(prepared) > 1 and transition_duration > 0:
            if transition not in _ALLOWED_TRANSITIONS:
                raise ValueError(f"Unsupported transition {transition!r}")
            if any(transition_duration >= item.duration for item in prepared):
                raise ValueError("transition_duration must be shorter than every speed-adjusted clip")
        elif transition_duration < 0 or not math.isfinite(transition_duration):
            raise ValueError("transition_duration must be a finite, non-negative number")
        if transition_duration == 0 and len(prepared) > 1 and transition not in _ALLOWED_TRANSITIONS:
            raise ValueError(f"Unsupported transition {transition!r}")
        if not math.isfinite(j_cut_duration) or j_cut_duration < 0:
            raise ValueError("j_cut_duration must be a finite, non-negative number")
        if j_cut_duration and not prepared[0].include_audio:
            raise ValueError("j_cut_duration requires source audio in every scene")
        if j_cut_duration >= min(item.duration for item in prepared):
            raise ValueError("j_cut_duration must be shorter than every scene")
        hook_seconds = _seconds(flash_forward_seconds, "flash_forward_seconds")
        if hook_seconds > 3.0:
            raise ValueError("flash_forward_seconds cannot exceed the 3-second hook limit")
        if hook_seconds and len(prepared) < 1:
            raise ValueError("flash-forward requires at least one scene")
        if hook_seconds:
            if hook_scene_index < 0:
                hook_scene_index = len(prepared) - 1
            if not 0 <= hook_scene_index < len(prepared):
                raise ValueError("hook_scene_index is outside the clip list")
            hook_seconds = min(hook_seconds, prepared[hook_scene_index].duration)
        loop_trim = _seconds(seamless_loop_trim_seconds, "seamless_loop_trim_seconds")
        natural_duration = sum(item.duration for item in prepared) - transition_duration * max(0, len(prepared) - 1)
        if loop_trim >= natural_duration:
            raise ValueError("seamless_loop_trim_seconds must be shorter than the render")
        if lut_name is not None:
            lut_path = Path(lut_name).expanduser()
            if not lut_path.is_absolute():
                lut_path = config.asset_directory("luts") / lut_path
            lut_path = lut_path.resolve(strict=True)
            if not _is_within(lut_path, config.asset_directory("luts")) or not lut_path.is_file() or lut_path.suffix.casefold() != ".cube":
                raise VideoEditorError("LUT must be a .cube file inside Google Drive assets/luts")
        else:
            lut_path = None
        destination = self._archive_path(output_path)
        if encoder not in {"amf", "libx264"}:
            raise ValueError("encoder must be 'amf' or 'libx264'")
        ffmpeg = self._resolve_ffmpeg()
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for clip in prepared:
            command.extend(["-i", str(clip.source)])
        filters: list[str] = []
        video_labels: list[str] = []
        for index, clip in enumerate(prepared):
            trimmed = f"trim{index}"
            staged = f"stage{index}"
            filters.append(
                f"[{index}:v]trim=start={clip.start:.6f}:end={clip.end:.6f},"
                f"setpts=(PTS-STARTPTS)/{clip.speed:.6f},fps={fps},settb=AVTB[{trimmed}]"
            )
            filters.extend(
                self._framing_filters(
                    trimmed,
                    output_label=staged,
                    framing=clip.framing,
                    width=width,
                    height=height,
                    center_x=clip.center_x,
                    motion=clip.motion,
                    fps=fps,
                    clip_duration=clip.duration,
                )
            )
            video_labels.append(f"{staged}out")

        if len(video_labels) == 1:
            filters.append(f"[{video_labels[0]}]null[vprogram]")
            program_duration = prepared[0].duration
        elif transition_duration > 0:
            previous = video_labels[0]
            accumulated = prepared[0].duration
            for index in range(1, len(video_labels)):
                output_label = f"xf{index}"
                offset = max(0.0, accumulated - transition_duration)
                filters.append(
                    f"[{previous}][{video_labels[index]}]xfade=transition={transition}:"
                    f"duration={transition_duration:.6f}:offset={offset:.6f}[{output_label}]"
                )
                previous = output_label
                accumulated += prepared[index].duration - transition_duration
            filters.append(f"[{previous}]null[vprogram]")
            program_duration = accumulated
        else:
            labels = "".join(f"[{label}]" for label in video_labels)
            filters.append(f"{labels}concat=n={len(video_labels)}:v=1:a=0[vprogram]")
            program_duration = sum(item.duration for item in prepared)

        if hook_seconds:
            hook_label = "flashforward"
            hook_clip = prepared[hook_scene_index]
            hook_start = max(0.0, hook_clip.duration - hook_seconds)
            filters.append(
                f"[{video_labels[hook_scene_index]}]trim=start={hook_start:.6f}:duration={hook_seconds:.6f},"
                f"setpts=PTS-STARTPTS[{hook_label}]"
            )
            filters.append(f"[{hook_label}][vprogram]concat=n=2:v=1:a=0[vwithhook]")
            video_program = "vwithhook"
        else:
            video_program = "vprogram"
        if lut_path is not None:
            filters.append(f"[{video_program}]lut3d=file='{self._filter_path(lut_path)}'[vfinal]")
        else:
            filters.append(f"[{video_program}]null[vfinal]")

        audio_enabled = prepared[0].include_audio
        if audio_enabled:
            audio_labels: list[str] = []
            for index, clip in enumerate(prepared):
                audio_label = f"audio{index}"
                tempo = self._atempo_filter(clip.speed)
                gain = 10 ** (clip.audio_gain_db / 20.0)
                filters.append(
                    f"[{index}:a]atrim=start={clip.start:.6f}:end={clip.end:.6f},"
                    f"asetpts=PTS-STARTPTS,{tempo},aresample=48000,"
                    f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                    f"volume={gain:.8f}[{audio_label}]"
                )
                audio_labels.append(audio_label)
            audio_overlap = j_cut_duration if j_cut_duration > 0 else transition_duration
            if len(audio_labels) == 1:
                filters.append(f"[{audio_labels[0]}]anull[audio_program]")
            elif audio_overlap > 0:
                previous_audio = audio_labels[0]
                for index in range(1, len(audio_labels)):
                    output_audio = f"across{index}"
                    filters.append(
                        f"[{previous_audio}][{audio_labels[index]}]acrossfade="
                        f"d={audio_overlap:.6f}:c1=tri:c2=tri[{output_audio}]"
                    )
                    previous_audio = output_audio
                filters.append(f"[{previous_audio}]anull[audio_program]")
            else:
                audio_joined = "".join(f"[{label}]" for label in audio_labels)
                filters.append(f"{audio_joined}concat=n={len(audio_labels)}:v=0:a=1[audio_program]")
            if hook_seconds:
                delay = int(round(hook_seconds * 1000))
                filters.append(f"[audio_program]adelay={delay}|{delay}[audiofinal]")
            elif loop_trim:
                filters.append(f"[audio_program]atrim=duration={program_duration - loop_trim:.6f}[audiofinal]")
            else:
                filters.append("[audio_program]anull[audiofinal]")

        filter_graph = ";".join(filters)
        command += config.ffmpeg_filter_args()
        command += ["-filter_complex", filter_graph, "-map", "[vfinal]"]
        if audio_enabled:
            command += ["-map", "[audiofinal]", "-c:a", "aac", "-b:a", "192k"]
        else:
            command.append("-an")
        if encoder == "amf":
            command += config.ffmpeg_encoder_args("h264")
        else:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", str(config.FFMPEG_THREADS)]
        output_duration = program_duration + hook_seconds - loop_trim
        command += [
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-t", f"{output_duration:.6f}",
            "-movflags", "+faststart",
            str(destination),
        ]
        return command

    @staticmethod
    def _atempo_filter(speed: float) -> str:
        """Return a legal pitch-preserving FFmpeg tempo filter chain."""
        if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
            raise ValueError("atempo speed must be between 0.5 and 2.0")
        factors: list[float] = []
        remaining = speed
        while remaining > 2.0:
            factors.append(2.0)
            remaining /= 2.0
        while remaining < 0.5:
            factors.append(0.5)
            remaining /= 0.5
        factors.append(remaining)
        return ",".join(f"atempo={factor:.6f}" for factor in factors)

    @staticmethod
    def _amf_unavailable(error: str) -> bool:
        lowered = error.casefold()
        markers = (
            "h264_amf",
            "amf",
            "unknown encoder",
            "encoder not found",
            "no capable devices",
            "failed to initialise",
            "failed to initialize",
            "device initialization",
            "device initialization failed",
            "not supported",
            "cannot load amf",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _run_process(command: Sequence[str], *, label: str, timeout: int = 7200) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(list(command), check=True, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise VideoDependencyUnavailable(f"Required executable was not found for {label}: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoRenderError(f"{label} exceeded its {timeout}-second safety timeout") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[-3_000:]
            raise VideoRenderError(f"{label} failed (exit {exc.returncode}): {stderr}") from exc

    def render(
        self,
        clips: Sequence[ClipSpec | Mapping[str, Any]],
        *,
        output_path: str | Path | None = None,
        resolution: tuple[int, int] = (1080, 1920),
        fps: int = 60,
        transition: str = "fade",
        transition_duration: float = 0.0,
        j_cut_duration: float = 0.0,
        lut_name: str | None = None,
        flash_forward_seconds: float = 0.0,
        hook_scene_index: int = -1,
        seamless_loop_trim_seconds: float = 0.0,
        prefer_amf: bool = True,
    ) -> RenderResult:
        try:
            fps = int(fps)
            transition_duration = float(transition_duration)
            j_cut_duration = float(j_cut_duration)
            flash_forward_seconds = float(flash_forward_seconds)
            seamless_loop_trim_seconds = float(seamless_loop_trim_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("fps and render timing options must be numeric") from exc
        destination = self._archive_path(output_path)
        common = {
            "output_path": destination,
            "resolution": resolution,
            "fps": fps,
            "transition": transition,
            "transition_duration": transition_duration,
            "j_cut_duration": j_cut_duration,
            "lut_name": lut_name,
            "flash_forward_seconds": flash_forward_seconds,
            "hook_scene_index": hook_scene_index,
            "seamless_loop_trim_seconds": seamless_loop_trim_seconds,
        }
        encoder = "amf" if prefer_amf else "libx264"
        command = self.build_ffmpeg_command(clips, encoder=encoder, **common)
        warnings: list[str] = []
        fallback = False
        try:
            self._run_process(command, label=f"{encoder} video render")
        except VideoRenderError as exc:
            if encoder != "amf" or not self._amf_unavailable(str(exc)):
                raise
            destination.unlink(missing_ok=True)
            encoder = "libx264"
            fallback = True
            warnings.append(f"AMF encoder unavailable; retried with CPU libx264: {str(exc)[-500:]}")
            command = self.build_ffmpeg_command(clips, encoder=encoder, **common)
            self._run_process(command, label="libx264 fallback render")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise VideoRenderError("FFmpeg reported success but no final MP4 was created")
        prepared = self._prepare_clips(clips)
        actual_hook = 0.0
        if flash_forward_seconds:
            selected_hook_index = hook_scene_index if hook_scene_index >= 0 else len(prepared) - 1
            actual_hook = min(flash_forward_seconds, prepared[selected_hook_index].duration)
        duration = (
            sum(clip.duration for clip in prepared)
            - transition_duration * max(0, len(prepared) - 1)
            + actual_hook
            - seamless_loop_trim_seconds
        )
        return RenderResult(
            path=destination,
            duration_seconds=duration,
            resolution=resolution,
            fps=fps,
            video_encoder="h264_amf" if encoder == "amf" else "libx264",
            used_software_fallback=fallback,
            warnings=tuple(warnings),
            command=tuple(command),
        )

    @staticmethod
    def _find_realesrgan_binary(model_root: Path) -> str | None:
        env_binary = (os.getenv("REALESRGAN_NCNN_VULKAN_BINARY", "").strip() or config.REALESRGAN_NCNN_VULKAN_BINARY)
        if env_binary:
            candidate = Path(env_binary).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
            located = shutil.which(env_binary)
            if located:
                return located
        for name in ("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan"):
            candidate = model_root / name
            if candidate.is_file():
                return str(candidate)
        return shutil.which("realesrgan-ncnn-vulkan")

    def build_realesrgan_command(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        model_name: str = "realesrgan-x4plus-anime",
    ) -> list[str] | None:
        """Resolve an optional Vulkan upscaler from Drive's models/realesrgan."""
        model_root = config.resolve_drive_path("models", "realesrgan")
        binary = self._find_realesrgan_binary(model_root)
        if binary is None:
            return None
        if not model_name or any(character in model_name for character in "\r\n"):
            raise ValueError("model_name must be a non-empty model identifier")
        return [
            binary,
            "-i", str(Path(input_path).resolve(strict=True)),
            "-o", str(Path(output_path).resolve(strict=False)),
            "-s", "2",
            "-n", model_name,
            "-m", str(model_root),
            "-f", "jpg",
        ]

    def upscale_to_1440p(
        self,
        input_path: str | Path,
        *,
        output_path: str | Path | None = None,
        prefer_vulkan: bool = True,
        fps: int = 60,
        model_name: str = "realesrgan-x4plus-anime",
    ) -> UpscaleResult:
        """Use Drive-installed Vulkan Real-ESRGAN frame-by-frame, else Lanczos.

        Real-ESRGAN NCNN Vulkan is primarily an image upscaler, so video is
        unpacked to bounded-size JPEG frames in ``config.TEMP_DIR``, upscaled
        from the Drive model folder, and reassembled with the original audio.
        The temporary frame directories are deleted at the end of this call.
        """
        source = Path(input_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"input video does not exist: {source}")
        try:
            fps_value = int(fps)
        except (TypeError, ValueError) as exc:
            raise ValueError("fps must be an integer from 24 to 60") from exc
        if isinstance(fps, bool) or fps_value != float(fps) or not 24 <= fps_value <= 60:
            raise ValueError("fps must be an integer from 24 to 60")
        destination = self._archive_path(output_path)
        ffmpeg = self._resolve_ffmpeg()
        warnings: list[str] = []
        external_command: list[str] | None = None
        if prefer_vulkan:
            config.ensure_runtime_dirs()
            try:
                with tempfile.TemporaryDirectory(prefix="realesrgan-", dir=config.TEMP_DIR) as scratch_name:
                    scratch = Path(scratch_name)
                    input_frames = scratch / "input_frames"
                    output_frames = scratch / "output_frames"
                    input_frames.mkdir()
                    output_frames.mkdir()
                    external_command = self.build_realesrgan_command(
                        input_frames, output_frames, model_name=model_name,
                    )
                    if external_command is None:
                        warnings.append("Real-ESRGAN Vulkan binary is not installed in Drive models/realesrgan or PATH.")
                    else:
                        extract_command = [
                            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                            *config.ffmpeg_filter_args(),
                            "-vf", f"scale=1080:1920:flags=lanczos,fps={fps_value}",
                            "-q:v", "2", str(input_frames / "%08d.jpg"),
                        ]
                        self._run_process(extract_command, label="1440p upscaler frame extraction", timeout=7200)
                        input_count = len(list(input_frames.glob("*.jpg")))
                        if input_count == 0:
                            raise VideoRenderError("FFmpeg extracted no frames for Real-ESRGAN")
                        self._run_process(external_command, label="Vulkan Real-ESRGAN frame upscale", timeout=7200)
                        output_count = len(list(output_frames.glob("*.jpg")))
                        if output_count != input_count:
                            raise VideoRenderError(
                                f"Real-ESRGAN produced {output_count} frames for {input_count} inputs"
                            )
                        sequence = output_frames / "%08d.jpg"
                        base_command = [
                            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-framerate", str(fps_value), "-start_number", "1", "-i", str(sequence),
                            "-i", str(source), *config.ffmpeg_filter_args(),
                            "-map", "0:v:0", "-map", "1:a?",
                            "-vf", f"scale=1440:2560:flags=lanczos,fps={fps_value},setsar=1",
                            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                            "-pix_fmt", "yuv420p", "-r", str(fps_value), "-shortest",
                            "-movflags", "+faststart",
                        ]
                        amf_command = base_command + config.ffmpeg_encoder_args("h264") + [str(destination)]
                        try:
                            self._run_process(amf_command, label="AMF Real-ESRGAN 1440p encode", timeout=7200)
                            command_used = amf_command
                        except VideoRenderError as exc:
                            if not self._amf_unavailable(str(exc)):
                                raise
                            destination.unlink(missing_ok=True)
                            warnings.append(f"AMF unavailable during upscaling; used libx264: {str(exc)[-400:]}")
                            command_used = base_command + [
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                "-threads", str(config.FFMPEG_THREADS), str(destination),
                            ]
                            self._run_process(command_used, label="libx264 Real-ESRGAN 1440p encode", timeout=7200)
                        if not destination.is_file() or not destination.stat().st_size:
                            raise VideoRenderError("Real-ESRGAN final 1440p encode created no MP4")
                        return UpscaleResult(
                            destination,
                            "realesrgan_ncnn_vulkan",
                            True,
                            tuple(warnings),
                            tuple(external_command),
                        )
            except (VideoEditorError, OSError, ValueError) as exc:
                destination.unlink(missing_ok=True)
                warnings.append(f"Real-ESRGAN unavailable; using Lanczos: {str(exc)[:400]}")

        fallback_command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            *config.ffmpeg_filter_args(),
            "-vf", f"scale=1440:2560:flags=lanczos,fps={fps_value}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-threads", str(config.FFMPEG_THREADS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination),
        ]
        self._run_process(fallback_command, label="Lanczos 1440p fallback upscale", timeout=7200)
        if not destination.is_file() or not destination.stat().st_size:
            raise VideoRenderError("Lanczos upscaler completed without creating an MP4")
        return UpscaleResult(destination, "lanczos", False, tuple(warnings), tuple(fallback_command))


__all__ = [
    "ClipSpec",
    "FramingMode",
    "RenderResult",
    "UpscaleResult",
    "VideoDependencyUnavailable",
    "VideoEditor",
    "VideoEditorError",
    "VideoRenderError",
]
