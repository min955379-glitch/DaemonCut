"""Local-first speech synthesis, voice-sample validation, and audio mastering.

Persistent audio and voice assets are kept in the configured Google Drive
folder. Intermediate provider files live only in ``config.TEMP_DIR``. The
copyright guard is intentionally conservative: metadata can document a user's
rights, but software cannot guarantee that a platform will not mute or strike a
track.
"""
from __future__ import annotations

import array
import asyncio
import base64
import json
import math
import os
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend import config


class AudioEngineError(RuntimeError):
    """Base error for synthesis, audio inspection, and mastering."""


class AudioDependencyUnavailable(AudioEngineError):
    """An optional local or network TTS provider is not installed/available."""


class AudioAssetValidationError(AudioEngineError):
    """An audio asset is missing, malformed, silent, or outside allowed storage."""


class AudioRightsError(AudioEngineError):
    """A BGM/SFX track lacks the required rights metadata or loop conditions."""


class AudioProcessError(AudioEngineError):
    """FFmpeg or a configured local synthesis process failed."""


class VoiceMode(str, Enum):
    AUTO_CAST = "auto_cast"
    CLONED_VOICE = "cloned_voice"
    MANUAL_OVERRIDE = "manual_override"


class SceneAudioMode(str, Enum):
    KEEP_ORIGINAL = "KEEP_ORIGINAL"
    REPLACE_VOICE = "REPLACE_VOICE"


@dataclass(frozen=True)
class VoiceProfile:
    profile_id: str
    kokoro_voice: str
    kokoro_language_code: str
    edge_voice: str
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    description: str = ""


@dataclass(frozen=True)
class AudioMetadata:
    path: str
    duration_seconds: float
    sample_rate: int | None
    channels: int | None
    codec: str | None
    file_size_bytes: int
    rms_dbfs: float | None = None
    silent: bool | None = None
    loop_safe: bool | None = None
    loop_seam_dbfs: float | None = None


@dataclass(frozen=True)
class AudioRightsAudit:
    path: str
    kind: str
    duration_seconds: float
    sample_rate: int | None
    channels: int | None
    license: str | None
    source: str | None
    attribution: str | None
    commercial_use: bool
    rights_confirmed_by_user: bool
    publish_safe: bool
    loop_safe: bool | None
    warnings: tuple[str, ...] = ()
    legal_note: str = "Rights metadata is user-provided and is not a legal opinion or platform guarantee."


@dataclass(frozen=True)
class AudioSynthesisResult:
    path: Path
    mode: VoiceMode
    provider: str
    voice_id: str | None
    profile_id: str
    voice_cloned: bool
    duration_seconds: float | None
    sample_rate: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SfxEvent:
    path: str | Path
    start_seconds: float
    gain_db: float = -8.0


@dataclass(frozen=True)
class SceneAudioDecision:
    mode: SceneAudioMode
    keep_original_audio: bool
    replace_dialogue: bool
    preserve_ambience: bool
    explanation: str


@dataclass(frozen=True)
class AudioMasterResult:
    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    target_lufs: float
    true_peak_dbfs: float
    encoder: str
    rights_audits: tuple[AudioRightsAudit, ...] = ()
    command: tuple[str, ...] = ()


_DEFAULT_VOICE_PROFILES: dict[str, VoiceProfile] = {
    "energetic_teen": VoiceProfile(
        "energetic_teen", "af_heart", "a", "en-US-AvaMultilingualNeural", "+8%", "+2Hz", "+0%",
        "Bright, lively delivery with clear articulation and moderate pace.",
    ),
    "calm_builder": VoiceProfile(
        "calm_builder", "am_michael", "a", "en-US-GuyNeural", "-4%", "-2Hz", "+0%",
        "Grounded, measured delivery with a gentle lower pitch.",
    ),
    "narrator": VoiceProfile(
        "narrator", "af_bella", "a", "en-US-AriaNeural", "+0%", "+0Hz", "+0%",
        "Warm, centered narration with even pace and crisp consonants.",
    ),
    "soft_young_boy": VoiceProfile(
        "soft_young_boy", "am_adam", "a", "en-US-ChristopherNeural", "-6%", "+3Hz", "-2%",
        "Soft, gentle, youthful delivery without imitating a real child.",
    ),
}

_SUPPORTED_AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
_LICENSE_ALIASES = {
    "owned": "owned",
    "original": "owned",
    "public domain": "public_domain",
    "public_domain": "public_domain",
    "cc0": "cc0",
    "cc-by": "cc_by",
    "cc_by": "cc_by",
    "cc-by-sa": "cc_by_sa",
    "cc_by_sa": "cc_by_sa",
    "royalty-free": "royalty_free",
    "royalty_free": "royalty_free",
    "licensed": "licensed",
}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _dbfs(value: float) -> float:
    if value <= 0.0:
        return -120.0
    return 20.0 * math.log10(value)


def _float_samples_to_wav(path: Path, values: Sequence[float], sample_rate: int) -> None:
    if not values:
        raise AudioProcessError("TTS provider returned no audio samples")
    pcm = array.array("h")
    for sample in values:
        value = float(sample)
        if not math.isfinite(value):
            value = 0.0
        value = max(-1.0, min(1.0, value))
        pcm.append(int(round(value * 32767.0)))
    if pcm.itemsize != 2:
        raise AudioProcessError("This Python build does not provide a 16-bit short array")
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        pcm.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def _audio_values(audio: Any) -> list[float]:
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu()
    if hasattr(audio, "numpy"):
        audio = audio.numpy()
    if hasattr(audio, "reshape"):
        audio = audio.reshape(-1)
    if hasattr(audio, "tolist"):
        audio = audio.tolist()
    if isinstance(audio, (int, float)):
        return [float(audio)]
    values: list[float] = []
    for item in audio:
        if isinstance(item, (list, tuple)):
            values.extend(_audio_values(item))
        else:
            values.append(float(item))
    return values


class AudioEngine:
    """Three-mode speech engine plus safety-audited FFmpeg mastering."""

    def __init__(self, *, voice_profiles_path: str | Path = config.SERIES_BIBLE_PATH) -> None:
        self.voice_profiles_path = Path(voice_profiles_path)
        self._kokoro_pipelines: dict[str, Any] = {}
        self._kokoro_lock = threading.RLock()

    @staticmethod
    def _ffmpeg() -> str:
        executable = shutil.which(config.FFMPEG_BINARY)
        if executable:
            return executable
        candidate = Path(config.FFMPEG_BINARY).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise AudioDependencyUnavailable(
            f"FFmpeg executable {config.FFMPEG_BINARY!r} was not found on PATH. Install FFmpeg and retry."
        )

    @staticmethod
    def _ffprobe() -> str | None:
        executable = shutil.which(config.FFPROBE_BINARY)
        if executable:
            return executable
        candidate = Path(config.FFPROBE_BINARY).expanduser()
        return str(candidate) if candidate.is_file() else None

    @staticmethod
    def _require_drive_or_scratch(path: str | Path, *, label: str) -> Path:
        resolved = Path(path).expanduser().resolve(strict=False)
        drive = config.resolve_drive_path()
        allowed = (drive, config.TEMP_DIR.resolve(strict=False), config.CACHE_DIR.resolve(strict=False))
        if not any(_is_within(resolved, root) for root in allowed):
            raise AudioAssetValidationError(
                f"{label} must be stored under Google Drive or DaemonCut's approved transient folders: {resolved}"
            )
        return resolved

    @staticmethod
    def _require_asset_path(path: str | Path, kind: str) -> Path:
        if kind not in {"bgm", "sfx"}:
            raise ValueError("kind must be 'bgm' or 'sfx'")
        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise AudioAssetValidationError(f"Missing {kind.upper()} audio asset: {path}") from exc
        root = config.asset_directory(kind)
        if not _is_within(resolved, root):
            raise AudioAssetValidationError(f"{kind.upper()} file must be inside Google Drive assets/{kind}: {resolved}")
        if not resolved.is_file() or resolved.suffix.casefold() not in _SUPPORTED_AUDIO:
            raise AudioAssetValidationError(f"Unsupported or missing {kind} audio file: {resolved}")
        return resolved

    @staticmethod
    def _prepare_output_path(path: str | Path | None, *, subdir: str, suffix: str = ".wav") -> Path:
        if path is None:
            target = config.resolve_drive_path(subdir, f"daemoncut_{uuid.uuid4().hex}{suffix}")
        else:
            target = Path(path).expanduser().resolve(strict=False)
        target = AudioEngine._require_drive_or_scratch(target, label="Audio output")
        if target.suffix.casefold() not in {".wav", ".mp3"}:
            raise ValueError("Audio output path must end in .wav or .mp3")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _safe_temp_path(*, suffix: str) -> Path:
        config.ensure_runtime_dirs()
        handle = tempfile.NamedTemporaryFile(prefix="audio-", suffix=suffix, dir=config.TEMP_DIR, delete=False)
        handle.close()
        return Path(handle.name)

    def _read_profiles(self) -> dict[str, VoiceProfile]:
        profiles = dict(_DEFAULT_VOICE_PROFILES)
        try:
            data = json.loads(self.voice_profiles_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return profiles
        raw_profiles = data.get("voice_profiles", {}) if isinstance(data, dict) else {}
        if not isinstance(raw_profiles, Mapping):
            return profiles
        for key, values in raw_profiles.items():
            if not isinstance(values, Mapping):
                continue
            profile_id = str(key).strip()
            base = profiles.get(profile_id, _DEFAULT_VOICE_PROFILES["narrator"])
            profiles[profile_id] = VoiceProfile(
                profile_id=profile_id,
                kokoro_voice=str(values.get("kokoro_voice", base.kokoro_voice)),
                kokoro_language_code=str(values.get("kokoro_language_code", base.kokoro_language_code)),
                edge_voice=str(values.get("edge_voice", base.edge_voice)),
                rate=str(values.get("rate", base.rate)),
                pitch=str(values.get("pitch", base.pitch)),
                volume=str(values.get("volume", base.volume)),
                description=str(values.get("description", base.description)),
            )
        return profiles

    def resolve_voice_profile(self, character: str | None = None, profile_id: str | None = None) -> VoiceProfile:
        profiles = self._read_profiles()
        if profile_id:
            try:
                return profiles[profile_id]
            except KeyError as exc:
                raise ValueError(f"Unknown voice profile {profile_id!r}") from exc
        if character:
            try:
                data = json.loads(self.voice_profiles_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            wanted = character.strip().casefold()
            series = data.get("series", {}) if isinstance(data, dict) else {}
            if isinstance(series, Mapping):
                for series_data in series.values():
                    chars = series_data.get("characters", []) if isinstance(series_data, Mapping) else []
                    for item in chars:
                        if isinstance(item, Mapping) and str(item.get("name", "")).casefold() == wanted:
                            key = item.get("voice_profile_id")
                            if key in profiles:
                                return profiles[key]
        return profiles["narrator"]

    @staticmethod
    def decide_scene_audio(
        mode: str | SceneAudioMode,
        *,
        original_audio_present: bool,
        replacement_dialogue_present: bool = False,
        preserve_ambience: bool = True,
    ) -> SceneAudioDecision:
        try:
            chosen = mode if isinstance(mode, SceneAudioMode) else SceneAudioMode(str(mode).upper())
        except ValueError as exc:
            raise ValueError("mode must be KEEP_ORIGINAL or REPLACE_VOICE") from exc
        if chosen is SceneAudioMode.KEEP_ORIGINAL:
            return SceneAudioDecision(
                mode=chosen,
                keep_original_audio=original_audio_present,
                replace_dialogue=False,
                preserve_ambience=original_audio_present,
                explanation="Retain the source clip's audio; do not overlay replacement dialogue.",
            )
        if not replacement_dialogue_present:
            raise ValueError("REPLACE_VOICE requires a synthesized or manually supplied dialogue track")
        return SceneAudioDecision(
            mode=chosen,
            keep_original_audio=False,
            replace_dialogue=True,
            preserve_ambience=bool(preserve_ambience and original_audio_present),
            explanation=(
                "Use the replacement dialogue. Preserve source ambience only when explicitly requested; "
                "the original dialogue track is not mixed back in."
            ),
        )

    def _validate_voice_reference(self, reference_path: str | Path | None = None) -> tuple[Path, AudioMetadata]:
        try:
            reference = (
                Path(reference_path).expanduser().resolve(strict=True)
                if reference_path is not None
                else config.resolve_drive_path("voice_vault", "my_voice.wav").resolve(strict=True)
            )
        except FileNotFoundError as exc:
            raise AudioAssetValidationError(
                "Cloned-voice sample is missing; place my_voice.wav in Google Drive voice_vault/"
            ) from exc
        voice_vault = config.resolve_drive_path("voice_vault")
        if not _is_within(reference, voice_vault):
            raise AudioAssetValidationError("Cloned-voice samples must be stored in Google Drive voice_vault/")
        if reference.suffix.casefold() != ".wav" or not reference.is_file():
            raise AudioAssetValidationError("Cloned-voice reference must be a readable WAV file in voice_vault/")
        try:
            with wave.open(str(reference), "rb") as wav_file:
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                sample_rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()
                duration = frame_count / sample_rate if sample_rate else 0.0
                raw = wav_file.readframes(frame_count)
        except (wave.Error, OSError) as exc:
            raise AudioAssetValidationError(f"Reference WAV could not be decoded: {exc}") from exc
        if not 1 <= channels <= 2:
            raise AudioAssetValidationError("Voice reference must be mono or stereo")
        if sample_width not in {1, 2, 3, 4}:
            raise AudioAssetValidationError("Voice reference must use 8-, 16-, 24-, or 32-bit PCM")
        if not 16_000 <= sample_rate <= 96_000:
            raise AudioAssetValidationError("Voice reference sample rate must be between 16 kHz and 96 kHz")
        if not 1.0 <= duration <= 30.0:
            raise AudioAssetValidationError("Voice reference duration must be between 1 and 30 seconds")
        rms = self._wav_rms(raw, sample_width)
        rms_db = _dbfs(rms)
        if rms_db < -55.0:
            raise AudioAssetValidationError(f"Voice reference is silent or too quiet ({rms_db:.1f} dBFS)")
        return reference, AudioMetadata(
            path=str(reference),
            duration_seconds=duration,
            sample_rate=sample_rate,
            channels=channels,
            codec=f"pcm_s{sample_width * 8}le",
            file_size_bytes=reference.stat().st_size,
            rms_dbfs=rms_db,
            silent=False,
        )

    @staticmethod
    def _wav_rms(raw: bytes, sample_width: int) -> float:
        if not raw:
            return 0.0
        if sample_width == 1:
            total_sq = sum((sample - 128) ** 2 for sample in raw)
            denominator = 128.0
            count = len(raw)
        elif sample_width == 2:
            samples = struct.iter_unpack("<h", raw[: len(raw) // 2 * 2])
            values = [sample[0] for sample in samples]
            total_sq = sum(value * value for value in values)
            denominator = 32768.0
            count = len(values)
        elif sample_width == 3:
            usable = len(raw) // 3 * 3
            values = []
            for index in range(0, usable, 3):
                sample = int.from_bytes(raw[index : index + 3], "little", signed=False)
                if sample & 0x800000:
                    sample -= 0x1000000
                values.append(sample)
            total_sq = sum(value * value for value in values)
            denominator = 8_388_608.0
            count = len(values)
        else:
            usable = len(raw) // 4 * 4
            values = [sample[0] for sample in struct.iter_unpack("<i", raw[:usable])]
            total_sq = sum(value * value for value in values)
            denominator = 2_147_483_648.0
            count = len(values)
        if count == 0:
            return 0.0
        return math.sqrt(total_sq / count) / denominator

    def _set_drive_model_cache(self) -> Path:
        cache = config.resolve_drive_path("models", "tts", "huggingface")
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(cache)
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(cache / "hub")
        os.environ["TRANSFORMERS_CACHE"] = str(cache / "transformers")
        return cache

    def _get_kokoro_pipeline(self, language_code: str) -> Any:
        if not config.KOKORO_ENABLED:
            raise AudioDependencyUnavailable("KOKORO_ENABLED is false")
        with self._kokoro_lock:
            if language_code in self._kokoro_pipelines:
                return self._kokoro_pipelines[language_code]
            self._set_drive_model_cache()
            try:
                from kokoro import KPipeline  # type: ignore[import-not-found]
            except ImportError as exc:
                raise AudioDependencyUnavailable("Kokoro is not installed; install the optional Kokoro CPU backend") from exc
            try:
                import inspect

                parameters = inspect.signature(KPipeline).parameters
                kwargs: dict[str, Any] = {"lang_code": language_code}
                if "device" in parameters:
                    kwargs["device"] = config.KOKORO_DEVICE
                pipeline = KPipeline(**kwargs)
            except Exception as exc:
                raise AudioDependencyUnavailable(f"Kokoro could not load its model from the Google Drive cache: {type(exc).__name__}") from exc
            self._kokoro_pipelines[language_code] = pipeline
            return pipeline

    def _synthesize_kokoro(
        self,
        text: str,
        destination: Path,
        profile: VoiceProfile,
        *,
        voice_id: str | None = None,
    ) -> tuple[str, str]:
        pipeline = self._get_kokoro_pipeline(profile.kokoro_language_code)
        chosen_voice = voice_id or profile.kokoro_voice
        speed = 1.0 + (int(profile.rate.rstrip("%")) / 100.0 if profile.rate.endswith("%") else 0.0)
        temp_wav: Path | None = None
        try:
            try:
                import torch  # type: ignore[import-not-found]
                torch.set_num_threads(config.KOKORO_THREADS)
            except ImportError:
                pass
            generated = pipeline(text, voice=chosen_voice, speed=max(0.7, min(1.4, speed)))
            chunks: list[float] = []
            sample_rate = 24_000
            for chunk in generated:
                if isinstance(chunk, (tuple, list)) and len(chunk) >= 3:
                    audio = chunk[2]
                else:
                    audio = chunk
                chunks.extend(_audio_values(audio))
            temp_wav = self._safe_temp_path(suffix=".wav")
            _float_samples_to_wav(temp_wav, chunks, sample_rate)
            self._transcode_audio(temp_wav, destination)
        except Exception as exc:
            raise AudioDependencyUnavailable(f"Kokoro synthesis failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if temp_wav is not None:
                temp_wav.unlink(missing_ok=True)
        return "kokoro", chosen_voice

    @staticmethod
    def _run_coroutine(coroutine: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        result: dict[str, Any] = {}
        error: list[BaseException] = []

        def runner() -> None:
            try:
                result["value"] = asyncio.run(coroutine)
            except BaseException as exc:  # re-raised in the calling thread
                error.append(exc)

        thread = threading.Thread(target=runner, name="daemoncut-edge-tts", daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result.get("value")

    def _synthesize_edge(
        self,
        text: str,
        destination: Path,
        profile: VoiceProfile,
        *,
        voice_id: str | None = None,
    ) -> tuple[str, str]:
        if not config.EDGE_TTS_ENABLED:
            raise AudioDependencyUnavailable("EDGE_TTS_ENABLED is false")
        try:
            import edge_tts  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AudioDependencyUnavailable("edge-tts is not installed") from exc
        temp_mp3 = self._safe_temp_path(suffix=".mp3")
        selected_voice = voice_id or profile.edge_voice
        try:
            communicate = edge_tts.Communicate(
                text,
                selected_voice,
                rate=profile.rate,
                pitch=profile.pitch,
                volume=profile.volume,
            )
            self._run_coroutine(communicate.save(str(temp_mp3)))
            self._transcode_audio(temp_mp3, destination)
        except Exception as exc:
            raise AudioDependencyUnavailable(f"Edge TTS request failed: {type(exc).__name__}: {exc}") from exc
        finally:
            temp_mp3.unlink(missing_ok=True)
        return "edge_tts", selected_voice

    def _transcode_audio(self, source: Path, destination: Path, *, sample_rate: int = config.AUDIO_SAMPLE_RATE) -> None:
        ffmpeg = self._ffmpeg()
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        command += config.ffmpeg_filter_args()
        command += ["-vn", "-ar", str(sample_rate)]
        if destination.suffix.casefold() == ".wav":
            command.extend(["-ac", "1", "-c:a", "pcm_s16le"])
        elif destination.suffix.casefold() == ".mp3":
            command.extend(["-ac", "1", "-c:a", "libmp3lame", "-b:a", "192k"])
        else:
            raise ValueError("Audio output path must end in .wav or .mp3")
        command.append(str(destination))
        self._run_process(command, label="audio conversion")

    @staticmethod
    def _run_process(command: Sequence[str], *, label: str, timeout: int = 900) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                list(command),
                check=True,
                capture_output=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise AudioDependencyUnavailable(f"Required executable was not found for {label}: {command[0]}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[-2_000:]
            raise AudioProcessError(f"{label} failed with exit code {exc.returncode}: {stderr}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioProcessError(f"{label} timed out after {timeout} seconds") from exc
        return result

    @staticmethod
    def _local_url(url: str, *, label: str) -> str:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}:
            raise AudioDependencyUnavailable(f"{label} must be a localhost HTTP(S) endpoint to protect voice samples")
        return url

    def _synthesize_local_api(
        self,
        url: str,
        provider_name: str,
        text: str,
        reference: Path,
        destination: Path,
    ) -> tuple[str, str]:
        checked_url = self._local_url(url, label=provider_name)
        model_dir = config.resolve_drive_path("models", "tts", provider_name.casefold().replace(" ", "-"))
        model_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "text": text,
                "audio_prompt_path": str(reference),
                "reference_audio_path": str(reference),
                "output_path": str(destination),
                "output_format": destination.suffix.lstrip("."),
                "sample_rate": config.AUDIO_SAMPLE_RATE,
                "device": "cpu",
                "model_dir": str(model_dir),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            checked_url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json, audio/wav, audio/mpeg"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                content_type = response.headers.get("Content-Type", "").casefold()
                body = response.read(100_000_000)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AudioDependencyUnavailable(f"{provider_name} local API is unavailable: {type(exc).__name__}") from exc
        if not body:
            raise AudioProcessError(f"{provider_name} local API returned no audio")
        if "json" in content_type:
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AudioProcessError(f"{provider_name} returned malformed JSON") from exc
            encoded = data.get("audio_base64") if isinstance(data, Mapping) else None
            if isinstance(encoded, str):
                try:
                    body = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise AudioProcessError(f"{provider_name} returned invalid base64 audio") from exc
                if not body:
                    raise AudioProcessError(f"{provider_name} returned empty base64 audio")
                destination.write_bytes(body)
            else:
                response_path = data.get("output_path") if isinstance(data, Mapping) else None
                if not response_path:
                    raise AudioProcessError(f"{provider_name} JSON response needs audio_base64 or output_path")
                candidate = self._require_drive_or_scratch(response_path, label=f"{provider_name} output")
                if not candidate.is_file():
                    raise AudioProcessError(f"{provider_name} output file was not created: {candidate}")
                if candidate.resolve() != destination.resolve():
                    shutil.copy2(candidate, destination)
        else:
            destination.write_bytes(body)
        return provider_name, provider_name

    def _synthesize_pocket_command(
        self,
        text: str,
        reference: Path,
        destination: Path,
    ) -> tuple[str, str]:
        command_text = config.POCKET_TTS_COMMAND or shutil.which("pocket-tts") or ""
        if not command_text:
            raise AudioDependencyUnavailable("Pocket TTS CLI is not configured or installed")
        try:
            tokens = shlex.split(command_text, posix=True)
        except ValueError as exc:
            raise AudioDependencyUnavailable(f"POCKET_TTS_COMMAND is invalid: {exc}") from exc
        if not tokens:
            raise AudioDependencyUnavailable("POCKET_TTS_COMMAND is empty")
        model_dir = config.resolve_drive_path("models", "tts", "pocket-tts")
        replacements = {
            "text": text,
            "reference": str(reference),
            "output": str(destination),
            "model_dir": str(model_dir),
            "device": config.POCKET_TTS_DEVICE,
        }
        has_template = any("{text}" in token or "{reference}" in token or "{output}" in token for token in tokens)
        if has_template:
            command = [token.format_map(replacements) for token in tokens]
        else:
            command = tokens + [
                "--text", text,
                "--reference", str(reference),
                "--output", str(destination),
                "--model-dir", str(model_dir),
                "--device", config.POCKET_TTS_DEVICE,
                "--threads", str(config.POCKET_TTS_THREADS),
            ]
        self._run_process(command, label="Pocket TTS voice clone", timeout=900)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise AudioProcessError("Pocket TTS CLI completed without producing the requested audio")
        return "pocket_tts", "cloned_voice"

    def _try_clone(
        self,
        text: str,
        reference: Path,
        destination: Path,
    ) -> tuple[str, str]:
        providers: list[tuple[str, Any]] = []
        if config.POCKET_TTS_COMMAND or shutil.which("pocket-tts"):
            providers.append(("pocket_tts", lambda: self._synthesize_pocket_command(text, reference, destination)))
        if config.POCKET_TTS_API_URL:
            providers.append(("pocket_tts_api", lambda: self._synthesize_local_api(config.POCKET_TTS_API_URL, "Pocket TTS", text, reference, destination)))
        if config.CHATTERBOX_API_URL:
            providers.append(("chatterbox", lambda: self._synthesize_local_api(config.CHATTERBOX_API_URL, "Chatterbox", text, reference, destination)))
        failures: list[str] = []
        for _, callback in providers:
            try:
                return callback()
            except (AudioEngineError, OSError, ValueError) as exc:
                failures.append(str(exc))
                destination.unlink(missing_ok=True)
        raise AudioDependencyUnavailable("No local voice-cloning backend is ready" + (": " + " | ".join(failures) if failures else ""))

    def _auto_cast(
        self,
        text: str,
        destination: Path,
        profile: VoiceProfile,
        *,
        voice_id: str | None = None,
        preferred_provider: str | None = None,
    ) -> tuple[str, str, list[str]]:
        warnings: list[str] = []
        requested_provider = preferred_provider
        requested_voice = voice_id
        if voice_id and ":" in voice_id:
            prefix, raw_voice = voice_id.split(":", 1)
            if prefix.casefold() in {"kokoro", "edge", "edge_tts"}:
                requested_provider = "kokoro" if prefix.casefold() == "kokoro" else "edge_tts"
                requested_voice = raw_voice.strip()
        if requested_provider == "kokoro":
            providers = ["kokoro"]
        elif requested_provider == "edge_tts":
            providers = ["edge_tts"]
        elif requested_provider in {"kokoro", "edge_tts"}:
            providers = [requested_provider]
        else:
            providers = ["kokoro", "edge_tts"]
        if requested_voice and requested_provider is None:
            if requested_voice.startswith(("af_", "am_", "bf_", "bm_")):
                providers = ["kokoro", "edge_tts"]
            else:
                providers = ["edge_tts", "kokoro"]
        for provider in providers:
            try:
                if provider == "kokoro":
                    used, chosen = self._synthesize_kokoro(text, destination, profile, voice_id=requested_voice)
                else:
                    used, chosen = self._synthesize_edge(text, destination, profile, voice_id=requested_voice)
                return used, chosen, warnings
            except (AudioEngineError, OSError, ValueError, ImportError) as exc:
                warnings.append(f"{provider} unavailable: {str(exc)[:240]}")
                destination.unlink(missing_ok=True)
        raise AudioDependencyUnavailable("No auto-cast voice provider is available: " + " | ".join(warnings))

    def synthesize(
        self,
        text: str,
        *,
        mode: str | VoiceMode = VoiceMode.AUTO_CAST,
        character: str | None = None,
        profile_id: str | None = None,
        voice_id: str | None = None,
        manual_audio_path: str | Path | None = None,
        reference_audio_path: str | Path | None = None,
        output_path: str | Path | None = None,
    ) -> AudioSynthesisResult:
        """Synthesize dialogue using auto cast, local cloning, or manual override."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text cannot be empty")
        if len(text) > 20_000:
            raise ValueError("text must be at most 20000 characters")
        try:
            selected_mode = mode if isinstance(mode, VoiceMode) else VoiceMode(str(mode).casefold())
        except ValueError as exc:
            raise ValueError("mode must be auto_cast, cloned_voice, or manual_override") from exc
        profile = self.resolve_voice_profile(character, profile_id)
        suffix = Path(output_path).suffix if output_path is not None else ".wav"
        destination = self._prepare_output_path(output_path, subdir="voice_vault", suffix=suffix or ".wav")
        warnings: list[str] = []
        cloned = False
        if selected_mode is VoiceMode.MANUAL_OVERRIDE and manual_audio_path is not None:
            source = self._require_drive_or_scratch(manual_audio_path, label="Manual audio override")
            if not source.is_file() or source.suffix.casefold() not in {".wav", ".mp3"}:
                raise AudioAssetValidationError("Manual override must be a readable WAV or MP3")
            if source.resolve() != destination.resolve():
                self._transcode_audio(source, destination)
            provider, chosen_voice = "manual_audio", None
        elif selected_mode is VoiceMode.CLONED_VOICE:
            reference, _ = self._validate_voice_reference(reference_audio_path)
            try:
                provider, chosen_voice = self._try_clone(text.strip(), reference, destination)
                cloned = True
            except AudioDependencyUnavailable as exc:
                warnings.append(f"Voice cloning fallback: {exc}")
                provider, chosen_voice, cast_warnings = self._auto_cast(text.strip(), destination, profile, voice_id=voice_id)
                warnings.extend(cast_warnings)
        else:
            if selected_mode is VoiceMode.MANUAL_OVERRIDE and not voice_id:
                raise ValueError("manual_override requires manual_audio_path or an explicit voice_id")
            provider, chosen_voice, cast_warnings = self._auto_cast(text.strip(), destination, profile, voice_id=voice_id)
            warnings.extend(cast_warnings)
        metadata = self.probe_audio(destination)
        return AudioSynthesisResult(
            path=destination,
            mode=selected_mode,
            provider=provider,
            voice_id=chosen_voice,
            profile_id=profile.profile_id,
            voice_cloned=cloned,
            duration_seconds=metadata.duration_seconds,
            sample_rate=metadata.sample_rate,
            warnings=tuple(warnings),
        )

    def _probe_with_ffprobe(self, path: Path) -> AudioMetadata | None:
        ffprobe = self._ffprobe()
        if ffprobe is None:
            return None
        command = [
            ffprobe, "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,sample_rate,channels",
            "-of", "json", str(path),
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
            payload = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return None
        streams = [item for item in payload.get("streams", []) if item.get("codec_type") == "audio"]
        stream = streams[0] if streams else {}
        try:
            duration = float(payload.get("format", {}).get("duration", 0.0))
        except (ValueError, TypeError):
            duration = 0.0
        if duration <= 0:
            return None
        try:
            rate = int(stream.get("sample_rate")) if stream.get("sample_rate") else None
        except (TypeError, ValueError):
            rate = None
        try:
            channels = int(stream.get("channels")) if stream.get("channels") else None
        except (TypeError, ValueError):
            channels = None
        try:
            size = int(payload.get("format", {}).get("size", path.stat().st_size))
        except (ValueError, TypeError, OSError):
            size = path.stat().st_size
        return AudioMetadata(
            path=str(path), duration_seconds=duration, sample_rate=rate, channels=channels,
            codec=stream.get("codec_name"), file_size_bytes=size,
        )

    def probe_audio(self, path: str | Path) -> AudioMetadata:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.casefold() not in _SUPPORTED_AUDIO:
            raise AudioAssetValidationError(f"Unsupported or missing audio file: {resolved}")
        probed = self._probe_with_ffprobe(resolved)
        if probed is not None:
            return probed
        if resolved.suffix.casefold() == ".wav":
            try:
                with wave.open(str(resolved), "rb") as wav_file:
                    rate = wav_file.getframerate()
                    frames = wav_file.getnframes()
                    duration = frames / rate if rate else 0.0
                    channels = wav_file.getnchannels()
                    width = wav_file.getsampwidth()
                    data = wav_file.readframes(frames)
                rms = self._wav_rms(data, width)
                return AudioMetadata(
                    path=str(resolved), duration_seconds=duration, sample_rate=rate, channels=channels,
                    codec=f"pcm_s{width * 8}le", file_size_bytes=resolved.stat().st_size,
                    rms_dbfs=_dbfs(rms), silent=rms < 0.001,
                )
            except (wave.Error, OSError) as exc:
                raise AudioAssetValidationError(f"Could not inspect WAV {resolved}: {exc}") from exc
        raise AudioDependencyUnavailable("ffprobe is needed to inspect non-WAV audio metadata")

    @staticmethod
    def _license_manifest_path(audio_path: Path) -> Path | None:
        candidates = (
            audio_path.with_suffix(audio_path.suffix + ".license.json"),
            audio_path.with_suffix(".license.json"),
        )
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def inspect_audio_asset(self, path: str | Path, *, kind: str) -> AudioRightsAudit:
        """Audit an asset in Drive and read its user-maintained license sidecar.

        A track is considered publish-eligible only when a manifest states a
        recognized license, source, commercial-use permission and user-confirmed
        rights. This is a conservative workflow guard, not legal advice.
        """
        asset_path = self._require_asset_path(path, kind)
        metadata = self.probe_audio(asset_path)
        manifest_path = self._license_manifest_path(asset_path)
        warnings: list[str] = []
        manifest: Mapping[str, Any] = {}
        if manifest_path is None:
            warnings.append("No .license.json sidecar was found; publication is blocked by the copyright guard.")
        else:
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    manifest = loaded
                else:
                    warnings.append("License sidecar must contain a JSON object.")
            except (OSError, json.JSONDecodeError):
                warnings.append("License sidecar could not be read as valid JSON.")
        raw_license = str(manifest.get("license", "")).strip().casefold()
        license_id = _LICENSE_ALIASES.get(raw_license)
        source = str(manifest.get("source", "")).strip() or None
        attribution = str(manifest.get("attribution", "")).strip() or None
        commercial = manifest.get("commercial_use") is True
        confirmed = manifest.get("rights_confirmed_by_user") is True
        if not license_id:
            warnings.append("License is missing or is not on the recognized license list.")
        if not source:
            warnings.append("License source/receipt is missing.")
        if not commercial:
            warnings.append("Commercial-use permission is not explicitly confirmed.")
        if not confirmed:
            warnings.append("rights_confirmed_by_user must be true in the sidecar.")
        if license_id in {"cc_by", "cc_by_sa"} and not attribution:
            warnings.append("This Creative Commons license requires an attribution string.")
        loop_declared = manifest.get("loop_safe")
        loop_safe: bool | None = bool(loop_declared) if isinstance(loop_declared, bool) else None
        seam = None
        if asset_path.suffix.casefold() == ".wav":
            seam = self._wav_loop_seam_dbfs(asset_path)
            if seam is not None:
                measured_safe = seam <= -30.0
                loop_safe = measured_safe if loop_safe is None else bool(loop_safe and measured_safe)
                if not measured_safe:
                    warnings.append(f"WAV loop seam measures {seam:.1f} dBFS; use a crossfade or a different loop.")
        if kind == "bgm" and loop_safe is None:
            warnings.append("Loop safety is unknown; a short BGM cannot be extended safely without a loop declaration.")
        publish_safe = bool(license_id and source and commercial and confirmed and not (license_id in {"cc_by", "cc_by_sa"} and not attribution))
        return AudioRightsAudit(
            path=str(asset_path), kind=kind, duration_seconds=metadata.duration_seconds,
            sample_rate=metadata.sample_rate, channels=metadata.channels, license=license_id,
            source=source, attribution=attribution, commercial_use=commercial,
            rights_confirmed_by_user=confirmed, publish_safe=publish_safe, loop_safe=loop_safe,
            warnings=tuple(warnings),
        )

    def _wav_loop_seam_dbfs(self, path: Path, *, window_seconds: float = 0.05) -> float | None:
        try:
            with wave.open(str(path), "rb") as wav_file:
                channels = wav_file.getnchannels()
                width = wav_file.getsampwidth()
                rate = wav_file.getframerate()
                frame_count = wav_file.getnframes()
                take = max(1, min(int(window_seconds * rate), frame_count // 2))
                head = wav_file.readframes(take)
                wav_file.setpos(max(0, frame_count - take))
                tail = wav_file.readframes(take)
        except (wave.Error, OSError):
            return None
        if width != 2 or channels < 1:
            return None
        per_frame_bytes = width * channels
        count = min(len(head), len(tail)) // per_frame_bytes
        if count <= 0:
            return None
        sample_bytes = count * channels * width
        head_samples = [item[0] for item in struct.iter_unpack("<h", head[:sample_bytes])]
        tail_samples = [item[0] for item in struct.iter_unpack("<h", tail[:sample_bytes])]
        delta = [last - first for first, last in zip(head_samples, tail_samples)]
        rms = math.sqrt(sum(value * value for value in delta) / max(1, len(delta))) / 32768.0
        return _dbfs(rms)

    @staticmethod
    def _license_publish_allowed(audit: AudioRightsAudit) -> None:
        if not audit.publish_safe:
            reason = " ".join(audit.warnings) or "license requirements were not satisfied"
            raise AudioRightsError(f"Refusing to publish {audit.kind} audio {Path(audit.path).name}: {reason}")

    def save_license_manifest(
        self,
        audio_path: str | Path,
        *,
        kind: str,
        license: str,
        source: str,
        commercial_use: bool,
        rights_confirmed_by_user: bool,
        attribution: str = "",
        loop_safe: bool | None = None,
        license_document: str = "",
    ) -> Path:
        asset_path = self._require_asset_path(audio_path, kind)
        license_key = _LICENSE_ALIASES.get(license.strip().casefold())
        if license_key is None:
            raise ValueError(f"Unsupported license value {license!r}")
        if not source.strip():
            raise ValueError("source must identify the creator, store, or license record")
        if not commercial_use or not rights_confirmed_by_user:
            raise ValueError("commercial_use and rights_confirmed_by_user must both be explicitly true")
        if license_key in {"cc_by", "cc_by_sa"} and not attribution.strip():
            raise ValueError("CC-BY licenses require an attribution string")
        document: dict[str, Any] = {
            "license": license_key,
            "source": source.strip(),
            "commercial_use": True,
            "rights_confirmed_by_user": True,
            "attribution": attribution.strip(),
            "loop_safe": loop_safe,
            "license_document": license_document.strip(),
        }
        manifest_path = asset_path.with_suffix(asset_path.suffix + ".license.json")
        manifest_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    @staticmethod
    def _atempo_filters(speed: float) -> str:
        """FFmpeg atempo factors are each limited to 0.5-2.0."""
        if not 0.5 <= speed <= 2.0:
            raise ValueError("Audio speed must be between 0.5 and 2.0")
        return f"atempo={speed:.6f}"

    def master_audio(
        self,
        dialogue_path: str | Path,
        *,
        output_path: str | Path | None = None,
        bgm_path: str | Path | None = None,
        sfx_events: Sequence[SfxEvent | Mapping[str, Any]] = (),
        duration_seconds: float | None = None,
        bgm_gain_db: float = -18.0,
        sidechain_threshold: float = 0.03,
        sidechain_ratio: float = 8.0,
        bgm_ducking: bool = True,
    ) -> AudioMasterResult:
        """Mix dialogue, licensed BGM and timed licensed SFX; master to -14 LUFS."""
        dialogue = self._require_drive_or_scratch(dialogue_path, label="Dialogue audio")
        if not dialogue.is_file():
            raise AudioAssetValidationError(f"Dialogue audio does not exist: {dialogue}")
        dialogue_metadata = self.probe_audio(dialogue)
        target_duration = float(dialogue_metadata.duration_seconds if duration_seconds is None else duration_seconds)
        if not math.isfinite(target_duration) or target_duration <= 0:
            raise ValueError("duration_seconds must be positive")
        if not -48.0 <= bgm_gain_db <= 0.0:
            raise ValueError("bgm_gain_db must be between -48 and 0 dB")
        if not 0.001 <= sidechain_threshold <= 1.0 or not 1.0 <= sidechain_ratio <= 20.0:
            raise ValueError("Invalid sidechain compressor threshold or ratio")
        destination = self._prepare_output_path(output_path, subdir="renders_archive", suffix=".wav")
        if destination.suffix.casefold() != ".wav":
            raise ValueError("Broadcast masters must be archived as 48 kHz stereo WAV files")
        if not _is_within(destination, config.resolve_drive_path("renders_archive")):
            raise AudioAssetValidationError("Final broadcast masters must be saved under Google Drive renders_archive")
        ffmpeg = self._ffmpeg()
        inputs: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(dialogue)]
        filter_parts: list[str] = [
            f"[0:a]aresample={config.AUDIO_SAMPLE_RATE},aformat=sample_fmts=fltp:sample_rates={config.AUDIO_SAMPLE_RATE}:channel_layouts=stereo,atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS[dlg]"
        ]
        rights: list[AudioRightsAudit] = []
        mix_labels: list[str] = ["dlg"]
        input_index = 1
        bgm_label: str | None = None
        if bgm_path is not None:
            bgm = self._require_asset_path(bgm_path, "bgm")
            audit = self.inspect_audio_asset(bgm, kind="bgm")
            self._license_publish_allowed(audit)
            rights.append(audit)
            if audit.duration_seconds < target_duration and audit.loop_safe is not True:
                raise AudioRightsError(
                    f"BGM {bgm.name} is shorter than the render and is not verified loop-safe; choose a longer track or document a clean loop."
                )
            if audit.duration_seconds < target_duration:
                inputs.extend(["-stream_loop", "-1"])
                inputs.extend(["-i", str(bgm)])
            else:
                inputs.extend(["-i", str(bgm)])
            gain = 10 ** (bgm_gain_db / 20.0)
            bgm_label = "bgm"
            filter_parts.append(
                f"[{input_index}:a]aresample={config.AUDIO_SAMPLE_RATE},aformat=sample_fmts=fltp:sample_rates={config.AUDIO_SAMPLE_RATE}:channel_layouts=stereo,volume={gain:.8f},atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS[bgm]"
            )
            if bgm_ducking:
                filter_parts.append(
                    f"[bgm][dlg]sidechaincompress=threshold={sidechain_threshold:.6f}:ratio={sidechain_ratio:.3f}:attack=20:release=250[ducked]"
                )
                mix_labels.append("ducked")
            else:
                mix_labels.append("bgm")
            input_index += 1

        sfx_labels: list[str] = []
        for index, raw_event in enumerate(sfx_events):
            if isinstance(raw_event, SfxEvent):
                event = raw_event
            elif isinstance(raw_event, Mapping):
                event = SfxEvent(
                    path=raw_event.get("path", ""),
                    start_seconds=float(raw_event.get("start_seconds", 0.0)),
                    gain_db=float(raw_event.get("gain_db", -8.0)),
                )
            else:
                raise ValueError(f"sfx_events[{index}] must be SfxEvent or a mapping")
            if not math.isfinite(event.start_seconds) or event.start_seconds < 0 or event.start_seconds >= target_duration:
                raise ValueError(f"sfx_events[{index}].start_seconds must be within the master duration")
            if not math.isfinite(event.gain_db) or not -36.0 <= event.gain_db <= 6.0:
                raise ValueError(f"sfx_events[{index}].gain_db must be between -36 and +6 dB")
            sfx_path = self._require_asset_path(event.path, "sfx")
            audit = self.inspect_audio_asset(sfx_path, kind="sfx")
            self._license_publish_allowed(audit)
            rights.append(audit)
            inputs.extend(["-i", str(sfx_path)])
            delay = int(round(event.start_seconds * 1000))
            gain = 10 ** (event.gain_db / 20.0)
            label = f"sfx{index}"
            filter_parts.append(
                f"[{input_index}:a]aresample={config.AUDIO_SAMPLE_RATE},aformat=sample_fmts=fltp:sample_rates={config.AUDIO_SAMPLE_RATE}:channel_layouts=stereo,volume={gain:.8f},adelay={delay}|{delay},atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS[{label}]"
            )
            sfx_labels.append(label)
            mix_labels.append(label)
            input_index += 1

        if bgm_label is None and not sfx_labels:
            filter_parts.append("[dlg]anull[mixed]")
        else:
            joined = "".join(f"[{label}]" for label in mix_labels)
            filter_parts.append(
                f"{joined}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0[mixed]"
            )
        filter_parts.append(
            f"[mixed]alimiter=limit=0.95,loudnorm=I={config.AUDIO_TARGET_LUFS:.1f}:TP={config.AUDIO_TRUE_PEAK_DBFS:.1f}:LRA={config.AUDIO_LOUDNESS_RANGE:.1f}:linear=true,aresample={config.AUDIO_SAMPLE_RATE},aformat=sample_fmts=s16:sample_rates={config.AUDIO_SAMPLE_RATE}:channel_layouts=stereo[outa]"
        )
        filter_graph = ";".join(filter_parts)
        command = inputs + config.ffmpeg_filter_args() + [
            "-filter_complex", filter_graph,
            "-map", "[outa]",
            "-t", f"{target_duration:.6f}",
            "-ar", str(config.AUDIO_SAMPLE_RATE),
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(destination),
        ]
        self._run_process(command, label="broadcast audio mastering", timeout=max(900, int(target_duration * 8)))
        return AudioMasterResult(
            path=destination, duration_seconds=target_duration, sample_rate=config.AUDIO_SAMPLE_RATE,
            channels=2, target_lufs=config.AUDIO_TARGET_LUFS,
            true_peak_dbfs=config.AUDIO_TRUE_PEAK_DBFS, encoder="pcm_s16le",
            rights_audits=tuple(rights), command=tuple(command),
        )


__all__ = [
    "AudioAssetValidationError",
    "AudioDependencyUnavailable",
    "AudioEngine",
    "AudioEngineError",
    "AudioMasterResult",
    "AudioMetadata",
    "AudioProcessError",
    "AudioRightsAudit",
    "AudioRightsError",
    "AudioSynthesisResult",
    "SceneAudioDecision",
    "SceneAudioMode",
    "SfxEvent",
    "VoiceMode",
    "VoiceProfile",
]
