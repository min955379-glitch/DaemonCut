"""Central configuration for DaemonCut Phase 1.

Heavy assets are resolved directly into a user-selected Google Drive for desktop
folder. There is deliberately no silent fallback to a Git checkout or local
scratch directory for model/media storage.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
SERIES_BIBLE_PATH = BACKEND_ROOT / "data" / "series_bible.json"


def _load_env_file(env_file: Path) -> None:
    """Load the project's .env without overriding values supplied by the OS."""
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(dotenv_path=env_file, override=False)
        return

    # Small dotenv-compatible fallback keeps local configuration usable even
    # when the optional convenience package is not installed.
    for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] and value[0] == "'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


_load_env_file(PROJECT_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of true/false, yes/no, 1/0, or on/off")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _expand_path(value: str) -> Path:
    expanded = os.path.expandvars(value.strip().strip('"').strip("'"))
    # os.path.expandvars only recognizes %NAME% on Windows. Accept that syntax
    # on every platform so .env.example remains portable and testable.
    expanded = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        expanded,
    )
    return Path(os.path.expanduser(expanded))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _configured_path(name: str, default: Path | None = None) -> Path | None:
    value = os.getenv(name, "").strip()
    if value:
        return _expand_path(value)
    return default


# Google Drive for desktop should be configured to mirror/stream this directory.
# Setup asks for its actual local path. None is intentional: it prevents a
# missing Drive configuration from silently placing large files in the repo.
GDRIVE_DAEMONCUT_PATH = _configured_path("GDRIVE_DAEMONCUT_PATH")

RUNTIME_DIR = PROJECT_ROOT / ".daemoncut"
DATABASE_PATH = _configured_path("DATABASE_PATH", RUNTIME_DIR / "daemoncut.sqlite3")
assert DATABASE_PATH is not None
CACHE_DIR = RUNTIME_DIR / "cache"
TEMP_DIR = RUNTIME_DIR / "tmp"
LOG_DIR = RUNTIME_DIR / "logs"

DRIVE_LAYOUT: tuple[str, ...] = (
    "models",
    "models/ollama",
    "models/whisper",
    "models/tts",
    "models/realesrgan",
    "assets/bgm",
    "assets/sfx",
    "assets/luts",
    "assets/fonts",
    "voice_vault",
    "source_media",
    "source_media/vods",
    "source_media/phone_uploads",
    "source_media/browser_cache",
    "renders_archive",
)

# Prompt length law: reference-image prompts are intentionally uncapped, while
# every YouTube Create video-scene prompt must be <= this exact limit.
MAX_YT_CREATE_CHARS = 880

# Hardware profile: AMD Ryzen 5 3600 (6C/12T), RX 6600 XT (8 GB), 16 GB RAM.
CPU_PHYSICAL_CORES = 6
CPU_LOGICAL_THREADS = 12
FFMPEG_THREADS = 6
FFMPEG_FILTER_THREADS = 6
GPU_NAME = "AMD Radeon RX 6600 XT"
GPU_VRAM_GB = 8
RAM_GB = 16
NVME_CAPACITY_GB = 512
FFMPEG_H264_ENCODER = "h264_amf"
FFMPEG_HEVC_ENCODER = "hevc_amf"
FFMPEG_AMF_QUALITY = "balanced"
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg").strip() or "ffmpeg"
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", "ffprobe").strip() or "ffprobe"
KOKORO_DEVICE = "cpu"
KOKORO_THREADS = 6
POCKET_TTS_DEVICE = "cpu"
POCKET_TTS_THREADS = 6
EDGE_TTS_ENABLED = _env_bool("EDGE_TTS_ENABLED", True)
KOKORO_ENABLED = _env_bool("KOKORO_ENABLED", True)
POCKET_TTS_COMMAND = os.getenv("POCKET_TTS_COMMAND", "").strip()
POCKET_TTS_API_URL = os.getenv("POCKET_TTS_API_URL", "").strip()
CHATTERBOX_API_URL = os.getenv("CHATTERBOX_API_URL", "").strip()
AUDIO_SAMPLE_RATE = _env_int("AUDIO_SAMPLE_RATE", 48_000, minimum=16_000, maximum=96_000)
AUDIO_TARGET_LUFS = _env_float("AUDIO_TARGET_LUFS", -14.0, minimum=-30.0, maximum=-5.0)
AUDIO_TRUE_PEAK_DBFS = _env_float("AUDIO_TRUE_PEAK_DBFS", -1.0, minimum=-6.0, maximum=0.0)
AUDIO_LOUDNESS_RANGE = _env_float("AUDIO_LOUDNESS_RANGE", 11.0, minimum=1.0, maximum=20.0)
WHISPER_CPP_BACKEND = "vulkan"
WHISPER_CPP_BINARY = os.getenv("WHISPER_CPP_BINARY", "whisper-cli").strip() or "whisper-cli"
WHISPER_CPP_THREADS = 6
WHISPER_CPP_GPU_LAYERS = 99
REALESRGAN_BACKEND = "vulkan"
REALESRGAN_NCNN_VULKAN_BINARY = os.getenv("REALESRGAN_NCNN_VULKAN_BINARY", "").strip()
OLLAMA_NUM_THREADS = 6
OLLAMA_CONTEXT_LENGTH = _env_int("OLLAMA_CONTEXT_LENGTH", 4096, minimum=1024, maximum=16384)
OLLAMA_NUM_PARALLEL = 1

# Phase 4's responsive console must preserve a usable safe zone at this width.
UI_SAFE_ZONE_MIN_WIDTH_PX = 380
UI_ZERO_GREEN_POLICY = True
UI_PALETTE = {
    "matte_obsidian": "#121316",
    "elevated_slate": "#1A1D24",
    "structural_border": "#2B303B",
    "warm_parchment": "#F4EFE6",
    "muted_brass": "#D6AD55",
    "deep_steel_blue": "#2B5B84",
    "deep_navy": "#102B4E",
    "crimson": "#C94A4A",
}
UI_FONT_SANS = "IBM Plex Sans"
UI_FONT_MONO = "IBM Plex Mono"
UI_STATUS_COLORS = {
    "active": UI_PALETTE["muted_brass"],
    "ready": UI_PALETTE["muted_brass"],
    "success": UI_PALETTE["muted_brass"],
    "info": UI_PALETTE["deep_steel_blue"],
    "alert": UI_PALETTE["crimson"],
    "recording": UI_PALETTE["crimson"],
}

# Only ephemeral, locally generated scratch data is eligible for TTL cleanup.
NVME_CACHE_TTL_HOURS = _env_int("NVME_CACHE_TTL_HOURS", 24, minimum=1, maximum=720)

# Phase 3 local service controls. One Uvicorn worker avoids duplicated
# schedulers; its bounded thread pool is sized to the Ryzen 5 3600 (6C/12T).
API_HOST = os.getenv("API_HOST", "0.0.0.0").strip() or "0.0.0.0"
API_PORT = _env_int("API_PORT", 8000, minimum=1, maximum=65_535)
API_WORKERS = 1
SOCIAL_HTTP_TIMEOUT_SECONDS = _env_int("SOCIAL_HTTP_TIMEOUT_SECONDS", 60, minimum=5, maximum=600)
SOCIAL_UPLOAD_CHUNK_BYTES = _env_int("SOCIAL_UPLOAD_CHUNK_BYTES", 8 * 1024 * 1024, minimum=1_048_576, maximum=64 * 1024 * 1024)
SOCIAL_STATUS_POLL_ATTEMPTS = _env_int("SOCIAL_STATUS_POLL_ATTEMPTS", 6, minimum=1, maximum=30)
SOCIAL_STATUS_POLL_SECONDS = _env_float("SOCIAL_STATUS_POLL_SECONDS", 2.0, minimum=0.1, maximum=30.0)
YOUTUBE_OAUTH_CLIENT_ID = os.getenv("YOUTUBE_OAUTH_CLIENT_ID", "").strip()
YOUTUBE_OAUTH_CLIENT_SECRET = os.getenv("YOUTUBE_OAUTH_CLIENT_SECRET", "").strip()
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
META_APP_ID = os.getenv("META_APP_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "").strip()
META_PAGE_ID = os.getenv("META_PAGE_ID", "").strip()
META_INSTAGRAM_USER_ID = os.getenv("META_INSTAGRAM_USER_ID", "").strip()
META_GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v22.0").strip() or "v22.0"
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/oauth/callback").strip()
YOUTUBE_PRIVACY_STATUS = os.getenv("YOUTUBE_PRIVACY_STATUS", "private").strip().casefold() or "private"
TIKTOK_PRIVACY_LEVEL = os.getenv("TIKTOK_PRIVACY_LEVEL", "PUBLIC_TO_EVERYONE").strip().upper() or "PUBLIC_TO_EVERYONE"
META_API_ENABLED = _env_bool("META_API_ENABLED", True)
API_THREADPOOL_WORKERS = _env_int("API_THREADPOOL_WORKERS", 6, minimum=1, maximum=6)
API_LOG_RING_SIZE = _env_int("API_LOG_RING_SIZE", 1_000, minimum=100, maximum=10_000)
BRIDGE_UPLOAD_MAX_BYTES = _env_int("BRIDGE_UPLOAD_MAX_BYTES", 536_870_912, minimum=1_048_576, maximum=8_589_934_592)
API_UPLOAD_CHUNK_BYTES = 1_048_576
YT_DLP_MAX_DURATION_SECONDS = _env_int("YT_DLP_MAX_DURATION_SECONDS", 21_600, minimum=60, maximum=86_400)
YT_DLP_MAX_FILESIZE_BYTES = _env_int("YT_DLP_MAX_FILESIZE_BYTES", 5_368_709_120, minimum=10_485_760, maximum=53_687_091_200)
YT_DLP_FRAGMENT_WORKERS = 1
BROWSER_SCRAPE_TIMEOUT_SECONDS = _env_int("BROWSER_SCRAPE_TIMEOUT_SECONDS", 25, minimum=5, maximum=120)
BOOT_NETWORK_RETRIES = _env_int("BOOT_NETWORK_RETRIES", 6, minimum=1, maximum=20)
BOOT_NETWORK_BASE_DELAY_SECONDS = _env_float("BOOT_NETWORK_BASE_DELAY_SECONDS", 2.0, minimum=0.1, maximum=60.0)
BOOT_NETWORK_MAX_DELAY_SECONDS = _env_float("BOOT_NETWORK_MAX_DELAY_SECONDS", 30.0, minimum=1.0, maximum=300.0)
PKT_PEAK_WINDOW_MINUTES = _env_int("PKT_PEAK_WINDOW_MINUTES", 15, minimum=1, maximum=120)
SCHEDULER_POLL_SECONDS = _env_int("SCHEDULER_POLL_SECONDS", 60, minimum=5, maximum=3_600)
SCHEDULER_AUTOSTART = _env_bool("SCHEDULER_AUTOSTART", True)
SCHEDULER_BOOT_AUTORUN = _env_bool("SCHEDULER_BOOT_AUTORUN", False)
if BOOT_NETWORK_BASE_DELAY_SECONDS > BOOT_NETWORK_MAX_DELAY_SECONDS:
    raise ValueError("BOOT_NETWORK_BASE_DELAY_SECONDS may not exceed BOOT_NETWORK_MAX_DELAY_SECONDS")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-4-hydrogen").strip() or "gemini-4-hydrogen"
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().casefold() or "auto"
if AI_PROVIDER not in {"auto", "gemini", "ollama"}:
    raise ValueError("AI_PROVIDER must be 'auto', 'gemini', or 'ollama'")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M").strip()
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "llava:7b").strip()
OLLAMA_ENABLED = _env_bool("OLLAMA_ENABLED", True)
OLLAMA_TIMEOUT_SECONDS = _env_int("OLLAMA_TIMEOUT_SECONDS", 90, minimum=1, maximum=900)
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "10m").strip() or "10m"
GEMINI_TIMEOUT_SECONDS = _env_int("GEMINI_TIMEOUT_SECONDS", 90, minimum=1, maximum=900)

TREND_COUNTRIES: tuple[str, ...] = ("US", "GB", "CA", "AU")
TREND_RSS_URL = os.getenv(
    "TREND_RSS_URL", "https://trends.google.com/trending/rss?geo={country}"
).strip()
TREND_FETCH_TIMEOUT_SECONDS = _env_int(
    "TREND_FETCH_TIMEOUT_SECONDS", 12, minimum=1, maximum=120
)


def resolve_drive_path(*parts: str | os.PathLike[str]) -> Path:
    """Return a safely-contained path inside the configured Drive folder.

    The root folder must be named ``DaemonCut`` or ``DaemonCut Development``.
    Path traversal and symlink/junction escapes are rejected.
    """
    if GDRIVE_DAEMONCUT_PATH is None:
        raise RuntimeError(
            "Google Drive storage is not configured. Set GDRIVE_DAEMONCUT_PATH "
            "in .env to the local Google Drive for desktop folder named "
            "'DaemonCut Development' (or 'DaemonCut')."
        )
    root = GDRIVE_DAEMONCUT_PATH.expanduser().resolve(strict=False)
    if root.name.casefold() not in {"daemoncut", "daemoncut development"}:
        raise ValueError(
            "GDRIVE_DAEMONCUT_PATH must point to a folder named 'DaemonCut' "
            "or 'DaemonCut Development', not the Drive root itself."
        )
    if not root.exists() and not root.parent.is_dir():
        raise FileNotFoundError(
            f"The parent of GDRIVE_DAEMONCUT_PATH does not exist: {root.parent}. "
            "Select a locally mounted Google Drive for desktop folder; DaemonCut will not create a fallback on the NVMe."
        )
    repo = PROJECT_ROOT.resolve(strict=False)
    try:
        root.relative_to(repo)
    except ValueError:
        pass
    else:
        raise ValueError("Google Drive storage must not be inside the Git repository.")

    candidate = root.joinpath(*(os.fspath(part) for part in parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Storage path escapes the configured Google Drive folder.") from exc
    return candidate


def ensure_drive_layout() -> dict[str, Path]:
    """Create and return the canonical heavy-file folders in Google Drive."""
    root = resolve_drive_path()
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {"root": root}
    for relative in DRIVE_LAYOUT:
        folder = resolve_drive_path(relative)
        folder.mkdir(parents=True, exist_ok=True)
        result[relative] = folder
    return result


def ensure_runtime_dirs() -> None:
    """Create only small local runtime directories (never model/media folders)."""
    for folder in (RUNTIME_DIR, CACHE_DIR, TEMP_DIR, LOG_DIR, DATABASE_PATH.parent):
        folder.mkdir(parents=True, exist_ok=True)


def asset_directory(kind: str) -> Path:
    """Resolve one of the supported Drive asset areas."""
    normalized = kind.strip().casefold()
    allowed = {"bgm", "sfx", "luts", "fonts"}
    if normalized not in allowed:
        raise ValueError(f"Unknown asset kind {kind!r}; expected one of {sorted(allowed)}")
    return resolve_drive_path("assets", normalized)


def ffmpeg_encoder_args(codec: str = "h264") -> list[str]:
    """Return conservative FFmpeg AMD AMF settings for the target RX 6600 XT."""
    normalized = codec.strip().casefold()
    if normalized in {"h264", "avc", "mp4"}:
        encoder = FFMPEG_H264_ENCODER
    elif normalized in {"hevc", "h265"}:
        encoder = FFMPEG_HEVC_ENCODER
    else:
        raise ValueError("codec must be h264/avc/mp4 or hevc/h265")
    return [
        "-c:v",
        encoder,
        "-quality",
        FFMPEG_AMF_QUALITY,
        "-threads",
        str(FFMPEG_THREADS),
    ]


def ffmpeg_filter_args() -> list[str]:
    """Bound FFmpeg CPU-side filters to the six physical Ryzen cores."""
    return [
        "-filter_threads",
        str(FFMPEG_FILTER_THREADS),
        "-filter_complex_threads",
        str(FFMPEG_FILTER_THREADS),
    ]


def whisper_cpp_runtime_args() -> list[str]:
    """Recommended whisper.cpp thread/GPU-layer flags for a Vulkan build."""
    return ["-t", str(WHISPER_CPP_THREADS), "-ngl", str(WHISPER_CPP_GPU_LAYERS)]


def ollama_options(*, temperature: float = 0.4) -> dict[str, int | float]:
    """Bound local inference for the 16 GB RAM / 8 GB VRAM target machine."""
    return {
        "num_ctx": OLLAMA_CONTEXT_LENGTH,
        "num_thread": OLLAMA_NUM_THREADS,
        "num_batch": 128,
        "temperature": max(0.0, min(float(temperature), 2.0)),
    }


def transient_directories() -> Iterable[Path]:
    """Directories eligible for 24-hour cleanup; never include Drive outputs."""
    return (CACHE_DIR, TEMP_DIR)


def _main() -> int:
    """Small config/storage diagnostic used by the Windows launcher."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate DaemonCut Phase 1 configuration")
    parser.add_argument("--check-storage", action="store_true", help="create/check Drive folders")
    parser.add_argument("--check-runtime", action="store_true", help="create local runtime folders")
    args = parser.parse_args()
    if args.check_storage:
        for name, folder in ensure_drive_layout().items():
            print(f"{name}: {folder}")
    if args.check_runtime:
        ensure_runtime_dirs()
        print(f"runtime: {RUNTIME_DIR}")
    if not args.check_storage and not args.check_runtime:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
