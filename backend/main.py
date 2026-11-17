"""FastAPI application and local hardware-console API for DaemonCut."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend import config
from backend.audio_engine import AudioEngine
from backend.brain_gemini import Brain
from backend.browser_cloner import BrowserCloner
from backend.clipper_engine import ClipperEngine, HighlightSegment, ClipperError, ClipperDependencyUnavailable, ClipperValidationError
from backend.database import ConflictError, DatabaseManager, NotFoundError
from backend.director_chat import DirectorChat, DirectorChatError
from backend.scheduler_daemon import SchedulerDaemon
from backend.social_publisher import (
    PLATFORMS,
    OAuthFlowError,
    PlatformNotConfigured,
    SocialPublisher,
    SocialPublisherError,
    TokenVaultError,
)
from backend.subtitle_engine import SubtitleEngine
from backend.thumbnail_maker import ThumbnailMaker
from backend.trend_scout import TrendScout
from backend.video_editor import ClipSpec, VideoEditor

LOGGER = logging.getLogger("daemoncut.api")
BRIDGE_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".3gp", ".jpg", ".jpeg", ".png", ".webp", ".heic"})
TRANSCRIPTION_MAX_BYTES = 64 * 1024 * 1024
_ALLOWED_COLORS = frozenset(config.UI_PALETTE.values())
ENV_EDITABLE_KEYS = frozenset({
    "GEMINI_API_KEY", "GEMINI_MODEL_ID", "YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET",
    "YOUTUBE_PRIVACY_STATUS", "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_PRIVACY_LEVEL",
    "META_APP_ID", "META_APP_SECRET", "META_API_ENABLED", "META_PAGE_ACCESS_TOKEN", "META_PAGE_ID",
    "META_INSTAGRAM_USER_ID", "META_GRAPH_API_VERSION", "OAUTH_REDIRECT_URI",
})
ENV_SECRET_KEYS = frozenset({"GEMINI_API_KEY", "YOUTUBE_OAUTH_CLIENT_SECRET", "TIKTOK_CLIENT_SECRET", "META_APP_SECRET", "META_PAGE_ACCESS_TOKEN"})


class APIRequestError(RuntimeError):
    """Expected service-side error returned as an API status."""


class WorkspaceCreateRequest(BaseModel):
    name: str
    timezone_name: str = "Asia/Karachi"
    default_platform: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    slug: str | None = None


class BatchCreateRequest(BaseModel):
    episodes: list[dict[str, Any]]
    batch_date: str | None = None


class EpisodeCreateRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=200)
    hook: str = Field(default="", max_length=2_000)
    script: str = Field(default="", max_length=100_000)
    scheduled_for: str | None = None
    status: str = "queued"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChannelCreateRequest(BaseModel):
    platform: str = Field(min_length=1, max_length=40)
    external_channel_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="", max_length=160)
    autopublish: bool = True


class BatchScanRequest(BaseModel):
    workspace_id: str
    episodes: list[dict[str, Any]] = Field(min_length=7, max_length=7)
    threshold: float = Field(default=0.72, ge=0.0, le=1.0)


class PublishEpisodeRequest(BaseModel):
    platforms: list[str] | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)


class SettingsUpdateRequest(BaseModel):
    updates: dict[str, str]


class StrategyRequest(BaseModel):
    niche: str = Field(min_length=2, max_length=160)
    target_audience: str = Field(min_length=2, max_length=200)
    channel_promise: str = Field(min_length=5, max_length=500)
    platforms: list[str] = Field(default_factory=lambda: ["YouTube Shorts"])
    tone: str = Field(default="", max_length=300)
    content_boundaries: list[str] = Field(default_factory=list, max_length=20)
    preferred_languages: list[str] = Field(default_factory=lambda: ["English"])


class NicheCreateRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=2, max_length=160)
    target_audience: str = Field(default="", max_length=200)
    channel_promise: str = Field(default="", max_length=500)
    tone: str = Field(default="", max_length=300)
    boundaries: list[str] = Field(default_factory=list, max_length=20)
    blueprint: dict[str, Any] = Field(default_factory=dict)
    series_bible: dict[str, Any] = Field(default_factory=dict)


class NicheSubscriptionsRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=100)
    niche_ids: list[str] = Field(default_factory=list, max_length=100)


class ScriptRequest(BaseModel):
    niche: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1, max_length=1_000)
    duration_seconds: int = Field(default=45, ge=10, le=3_600)
    style: str = Field(default="", max_length=500)
    audience: str = Field(default="general audience", max_length=200)
    language: str = Field(default="English", max_length=80)
    series_name: str | None = None
    scene_count: int = Field(default=5, ge=1, le=40)
    include_reference_prompts: bool = False
    character_description: str = ""
    environment_description: str = ""


class AudioSynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    mode: str = "auto_cast"
    character: str | None = None
    profile_id: str | None = None
    voice_id: str | None = None
    manual_audio_path: str | None = None
    reference_audio_path: str | None = None
    output_path: str | None = None


class AudioMasterRequest(BaseModel):
    dialogue_path: str = Field(min_length=1)
    output_path: str | None = None
    bgm_path: str | None = None
    sfx_events: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float | None = Field(default=None, gt=0)
    bgm_gain_db: float = Field(default=-18.0, ge=-48.0, le=0.0)
    bgm_ducking: bool = True


class SubtitleRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    duration_seconds: float = Field(gt=0)
    audio_path: str | None = None
    language_mode: str = "english"
    resolution: str = "1080p"
    use_brain: bool = True
    output_ass: str | None = None
    output_srt: str | None = None


class VideoRenderRequest(BaseModel):
    clips: list[dict[str, Any]] = Field(min_length=1, max_length=40)
    resolution: str = "1080p"
    fps: int = Field(default=60, ge=1, le=120)
    transition: str = "fade"
    transition_duration: float = Field(default=0.0, ge=0.0, le=5.0)
    lut_name: str | None = None
    output_path: str | None = None
    prefer_amf: bool = True


class ThumbnailRequest(BaseModel):
    hook: str = Field(min_length=1, max_length=200)
    video_path: str | None = None
    image_path: str | None = None
    candidate_count: int = Field(default=7, ge=2, le=20)
    use_brain: bool = False
    context: str = Field(default="", max_length=2_000)


class BrowserInspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2_048)
    inspect_dom: bool = True


class BrowserCloneRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2_048)
    niche: str = Field(min_length=1, max_length=160)
    topic: str | None = Field(default=None, max_length=1_000)
    duration_seconds: int = Field(default=45, ge=10, le=3_600)
    style: str = Field(default="original short-form animation", max_length=500)
    audience: str = Field(default="general audience", max_length=200)
    language: str = Field(default="English", max_length=80)
    scene_count: int = Field(default=5, ge=1, le=40)
    inspect_dom: bool = True


class DirectorChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict)


class DictationChunkRequest(BaseModel):
    text: str = Field(max_length=10_000)


class HighlightSelectionRequest(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    transcript: str = Field(default="", max_length=20_000)
    audio_score: float = Field(default=0.0, ge=0.0, le=100.0)
    hook_score: float = Field(default=0.0, ge=0.0, le=100.0)
    viral_score: float = Field(default=0.0, ge=0.0, le=100.0)
    explanation: str = Field(default="", max_length=1_000)


class ClipperRequest(BaseModel):
    source_url: str | None = None
    video_path: str | None = None
    title: str | None = None
    start_seconds: float = Field(default=0.0, ge=0)
    capture_duration_seconds: float | None = Field(default=None, ge=15, le=3_600)
    workspace_id: str | None = None
    transcript_segments: list[dict[str, Any]] = Field(default_factory=list)
    window_seconds: float = Field(default=30.0, ge=15.0, le=60.0)
    step_seconds: float = Field(default=8.0, ge=1.0, le=60.0)
    max_highlights: int = Field(default=5, ge=1, le=20)
    use_brain: bool = True
    render: bool = False
    framing_mode: str = "whole_blur"
    center_x: float = Field(default=0.5, ge=0.0, le=1.0)
    language_mode: str = "english"
    make_thumbnail: bool = True
    hook: str | None = None
    lut_name: str | None = None
    selected_highlight_indices: list[int] = Field(default_factory=list, max_length=20)
    selected_highlights: list[HighlightSelectionRequest] = Field(default_factory=list, max_length=20)


class TrendVariantsRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    context: str = ""
    audience: str = "general audience"
    platform: str = "YouTube Shorts"
    language: str = "English"
    use_ai: bool = True


class SchedulerRunRequest(BaseModel):
    wait_for_connectivity: bool = False
    cleanup_scratch: bool = False


class SchedulerStartRequest(BaseModel):
    boot_run: bool = False


@dataclass
class ServiceContainer:
    database: DatabaseManager
    brain: Brain
    trend_scout: TrendScout
    audio_engine: AudioEngine
    subtitle_engine: SubtitleEngine
    video_editor: VideoEditor
    thumbnail_maker: ThumbnailMaker
    browser_cloner: BrowserCloner
    clipper: ClipperEngine
    director_chat: DirectorChat
    social_publisher: SocialPublisher
    scheduler: SchedulerDaemon


def create_services(*, database: DatabaseManager | None = None) -> ServiceContainer:
    db = database or DatabaseManager()
    brain = Brain()
    video_editor = VideoEditor()
    audio_engine = AudioEngine()
    subtitle_engine = SubtitleEngine(brain=brain)
    thumbnail_maker = ThumbnailMaker(brain=brain, video_editor=video_editor)
    browser_cloner = BrowserCloner(brain=brain)
    clipper = ClipperEngine(brain=brain, video_editor=video_editor, subtitle_engine=subtitle_engine, thumbnail_maker=thumbnail_maker)
    director_chat = DirectorChat(
        brain=brain,
        clipper=clipper,
        browser_cloner=browser_cloner,
        audio_engine=audio_engine,
        subtitle_engine=subtitle_engine,
        video_editor=video_editor,
        thumbnail_maker=thumbnail_maker,
    )
    publisher = SocialPublisher()
    scheduler = SchedulerDaemon(db, upload_handler=publisher.scheduler_upload_handler)
    return ServiceContainer(
        database=db,
        brain=brain,
        trend_scout=TrendScout(brain=brain),
        audio_engine=audio_engine,
        subtitle_engine=subtitle_engine,
        video_editor=video_editor,
        thumbnail_maker=thumbnail_maker,
        browser_cloner=browser_cloner,
        clipper=clipper,
        director_chat=director_chat,
        social_publisher=publisher,
        scheduler=scheduler,
    )


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _serialize(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_serialize(item) for item in value]
    if hasattr(value, "model_dump"):
        return _serialize(value.model_dump())
    if hasattr(value, "dict") and callable(value.dict):
        return _serialize(value.dict())
    return str(value)


class LogHub:
    """Bounded activity ring-buffer and live WebSocket fan-out."""

    def __init__(self, capacity: int = config.API_LOG_RING_SIZE, *, queue_capacity: int = 256) -> None:
        if not 1 <= capacity <= 10_000 or not 1 <= queue_capacity <= 2_000:
            raise ValueError("log hub capacities are outside the supported range")
        self._events: deque[dict[str, str]] = deque(maxlen=capacity)
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, str]]]] = set()
        self._lock = threading.RLock()
        self.queue_capacity = queue_capacity

    @staticmethod
    def _event_color(level: int, success: bool = False) -> str:
        if success:
            return config.UI_PALETTE["muted_brass"]
        if level >= logging.WARNING:
            return config.UI_PALETTE["crimson"]
        if level <= logging.DEBUG:
            return config.UI_PALETTE["deep_navy"]
        return config.UI_PALETTE["deep_steel_blue"]

    def emit(self, message: str, *, level: int = logging.INFO, success: bool = False) -> dict[str, str]:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": logging.getLevelName(level).casefold(),
            "message": str(message).replace("\x00", "")[:4_000],
            "color": self._event_color(level, success=success),
        }
        with self._lock:
            self._events.append(event)
            subscribers = tuple(self._subscribers)
        for loop, queue in subscribers:
            if not loop.is_closed():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._offer, queue, event)
        return event

    def snapshot(self, *, limit: int = 200) -> list[dict[str, str]]:
        if not 1 <= limit <= 2_000:
            raise ValueError("limit must be between 1 and 2000")
        with self._lock:
            return list(self._events)[-limit:]

    async def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, str]]]:
        entry = (asyncio.get_running_loop(), asyncio.Queue(maxsize=self.queue_capacity))
        with self._lock:
            self._subscribers.add(entry)
        return entry

    def unsubscribe(self, entry: tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, str]]]) -> None:
        with self._lock:
            self._subscribers.discard(entry)

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, str]], event: dict[str, str]) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)


class LogHubHandler(logging.Handler):
    def __init__(self, hub: LogHub) -> None:
        super().__init__(level=logging.INFO)
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.emit(self.format(record), level=record.levelno, success=bool(getattr(record, "daemoncut_success", False)))
        except Exception:
            self.handleError(record)


class RequestSizeLimitMiddleware:
    """Limit multipart upload bodies before Starlette spools request data."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    async def _send_error(send: Any, status: int, message: str) -> None:
        body = json.dumps({"detail": {"status": "error", "message": message, "color": config.UI_PALETTE["crimson"]}}).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        if path == "/api/bridge/upload":
            limit = config.BRIDGE_UPLOAD_MAX_BYTES + 1_048_576
        elif path == "/api/director/transcribe":
            limit = TRANSCRIPTION_MAX_BYTES + 1_048_576
        else:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                declared = int(raw_length)
            except ValueError:
                await self._send_error(send, 400, "Invalid upload content length.")
                return
            if declared < 0:
                await self._send_error(send, 400, "Invalid upload content length.")
                return
            if declared > limit:
                await self._send_error(send, 413, f"Request exceeds the {limit}-byte transport limit.")
                return
        received = 0
        exceeded = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            if not exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await self._send_error(send, 413, f"Request exceeds the {limit}-byte transport limit.")


def detect_lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        address = sock.getsockname()[0]
        return address if address and not address.startswith("127.") else None
    except OSError:
        return None
    finally:
        sock.close()


def _qr_svg(data: str) -> str:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise APIRequestError("QR bridge support requires the qrcode package.") from exc
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=1, border=4)
    qr.add_data(data, optimize=0)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    dark = " ".join(f"M{x},{y}h1v1h-1z" for y, row in enumerate(matrix) for x, active in enumerate(row) if active)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" role="img" aria-label="Scan DaemonCut bridge" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="{config.UI_PALETTE["warm_parchment"]}"/>'
        f'<path d="{dark}" fill="{config.UI_PALETTE["matte_obsidian"]}"/></svg>'
    )


def _safe_filename(filename: str) -> tuple[str, str]:
    original = Path(filename or "upload.bin").name
    suffix = Path(original).suffix.casefold()
    if suffix not in BRIDGE_SUFFIXES:
        raise HTTPException(status_code=415, detail={"status": "error", "message": "Upload must be a supported video or reference image.", "color": config.UI_PALETTE["crimson"]})
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original).stem).strip("-_")[:80] or "upload"
    return original[:255], f"{stem}{suffix}"


async def _stream_upload_to_drive(file: UploadFile) -> tuple[Path, int, str]:
    maximum = config.BRIDGE_UPLOAD_MAX_BYTES
    original, safe_name = _safe_filename(file.filename or "upload")
    del original
    part_headers = file.headers
    raw_length = part_headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > maximum:
                raise HTTPException(status_code=413, detail={"status": "error", "message": f"Upload exceeds the {maximum}-byte limit.", "color": config.UI_PALETTE["crimson"]})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "Invalid upload content length.", "color": config.UI_PALETTE["crimson"]}) from exc
    try:
        root = config.resolve_drive_path("source_media", "phone_uploads")
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail={"status": "error", "message": f"Google Drive storage is unavailable: {str(exc)[:200]}", "color": config.UI_PALETTE["crimson"]}) from exc
    root.mkdir(parents=True, exist_ok=True)
    name = f"phone_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}_{safe_name}"
    destination = root / name
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with partial.open("xb") as handle:
            while True:
                chunk = await file.read(config.API_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise HTTPException(status_code=413, detail={"status": "error", "message": f"Upload exceeds the {maximum}-byte limit.", "color": config.UI_PALETTE["crimson"]})
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total == 0:
            raise HTTPException(status_code=400, detail={"status": "error", "message": "The uploaded file is empty.", "color": config.UI_PALETTE["crimson"]})
        partial.replace(destination)
        LOGGER.info("Mobile bridge saved %d bytes to Drive (%s).", total, destination.name)
        return destination, total, digest.hexdigest()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def _status_color(status: str) -> str:
    status = status.casefold()
    if status in {"ready", "active", "success", "uploaded", "completed", "connected", "published"}:
        return config.UI_PALETTE["muted_brass"]
    if status in {"error", "failed", "missing", "needs_configuration", "unavailable", "attention", "recording"}:
        return config.UI_PALETTE["crimson"]
    return config.UI_PALETTE["deep_steel_blue"]


def _bridge_url(request: Request, token: str) -> str:
    host = detect_lan_ip() or request.url.hostname or "127.0.0.1"
    if host.casefold() in {"localhost", "testserver", "0.0.0.0"}:
        host = "127.0.0.1"
    port = request.url.port or config.API_PORT
    return f"http://{host}:{port}/bridge?{urlencode({'token': token})}"


def _bridge_html(*, bridge_url: str, token: str, qr_svg: str) -> str:
    escaped_url = html.escape(bridge_url, quote=True)
    token_json = json.dumps(token)
    palette = config.UI_PALETTE
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DaemonCut Phone Bridge</title><style>
:root{{--ink:{palette['matte_obsidian']};--slate:{palette['elevated_slate']};--border:{palette['structural_border']};--paper:{palette['warm_parchment']};--brass:{palette['muted_brass']};--blue:{palette['deep_steel_blue']};--alert:{palette['crimson']}}}*{{box-sizing:border-box}}body{{margin:0;background:var(--ink);color:var(--paper);font:16px/1.5 system-ui,sans-serif}}main{{max-width:760px;margin:auto;padding:28px 18px 48px}}.card{{background:var(--slate);border:1px solid var(--border);border-radius:16px;padding:20px;margin:18px 0}}.qr{{width:min(100%,320px);padding:14px;margin:12px auto;background:var(--paper);border-radius:12px}}.qr svg{{display:block;width:100%;height:auto}}input{{display:block;width:100%;margin:16px 0;padding:14px;color:var(--paper);background:var(--ink);border:1px solid var(--border);border-radius:10px}}button{{min-height:48px;padding:12px 18px;color:var(--ink);background:var(--brass);border:0;border-radius:10px;font-weight:700}}a{{color:var(--brass)}}.status{{min-height:28px;color:var(--brass)}}.error{{color:var(--alert)}}.muted{{opacity:.78}}
</style></head><body><main><p class="muted">DAEMONCUT / LOCAL WI-FI BRIDGE</p><h1>Send clips to your Drive workspace</h1><p class="muted">Connect your phone to the same Wi-Fi network, scan the code, then select video clips or reference images.</p><section class="card"><div class="qr">{qr_svg}</div><p>Bridge link: <a href="{escaped_url}">{escaped_url}</a></p></section><section class="card"><form id="upload"><label for="files">Choose video or image files</label><input id="files" type="file" multiple accept="video/*,image/jpeg,image/png,image/webp,.mp4,.mov,.mkv,.webm,.jpg,.jpeg,.png,.webp"><button>Upload to Drive</button></form><p id="status" class="status" aria-live="polite"></p><ul id="files-list"></ul><p class="muted">Maximum per file: {config.BRIDGE_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB.</p></section></main><script>
const token={token_json};document.getElementById('upload').addEventListener('submit',async event=>{{event.preventDefault();const status=document.getElementById('status');const list=document.getElementById('files-list');list.replaceChildren();const files=[...document.getElementById('files').files];if(!files.length){{status.textContent='Choose at least one file.';status.className='status error';return;}}status.textContent='Uploading…';for(const file of files){{const data=new FormData();data.append('file',file,file.name);try{{const response=await fetch('/api/bridge/upload',{{method:'POST',headers:{{'X-Bridge-Token':token}},body:data}});const payload=await response.json();if(!response.ok)throw new Error(payload.detail?.message||'Upload failed.');const item=document.createElement('li');item.textContent=file.name+' — saved to Drive';list.append(item);}}catch(error){{const item=document.createElement('li');item.textContent=file.name+' — '+(error.message||'upload failed');list.append(item);}}}}status.textContent='Upload batch finished.';}});
</script></body></html>"""


def _settings_client_is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host not in {"testclient", "localhost"}:
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    origin = request.headers.get("origin")
    if origin:
        try:
            origin_host = (urlsplit(origin).hostname or "").casefold()
        except ValueError:
            return False
        if origin_host not in {"localhost", "127.0.0.1", "::1", "testserver"} and not origin_host.endswith(".e2b.app"):
            return False
    return True


def _write_local_env_updates(updates: Mapping[str, str]) -> None:
    path = config.PROJECT_ROOT / ".env"
    try:
        original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    except OSError as exc:
        raise APIRequestError("The local .env settings file could not be read.") from exc
    lines = original.splitlines()
    for key, value in updates.items():
        encoded = json.dumps(value, ensure_ascii=False)
        matcher = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
        matches = [index for index, line in enumerate(lines) if matcher.match(line)]
        if matches:
            lines[matches[0]] = f"{key}={encoded}"
            for index in reversed(matches[1:]):
                lines.pop(index)
        else:
            lines.append(f"{key}={encoded}")
    payload = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError as exc:
        raise APIRequestError("The local .env settings file could not be updated.") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _settings_summary() -> dict[str, Any]:
    values = {}
    for key in sorted(ENV_EDITABLE_KEYS):
        raw = os.getenv(key, str(getattr(config, key, "")))
        values[key] = {"configured": bool(raw), "secret": key in ENV_SECRET_KEYS}
    return {"values": values, "editable_keys": sorted(ENV_EDITABLE_KEYS)}


def create_app(services: ServiceContainer | None = None, *, start_scheduler: bool | None = None) -> FastAPI:
    """Create the local service with injectable dependencies for testing."""
    config.ensure_runtime_dirs()
    shared = services or create_services()
    should_start = config.SCHEDULER_AUTOSTART if start_scheduler is None else bool(start_scheduler)
    hub = LogHub()
    handler = LogHubHandler(hub)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    root_logger = logging.getLogger()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        root_logger.addHandler(handler)
        LOGGER.info("DaemonCut API startup complete; workers=%d, threadpool=%d.", config.API_WORKERS, config.API_THREADPOOL_WORKERS)
        try:
            import anyio.to_thread
            anyio.to_thread.current_default_thread_limiter().total_tokens = config.API_THREADPOOL_WORKERS
        except Exception:
            LOGGER.warning("Could not configure the bounded API thread pool.")
        if should_start:
            try:
                shared.scheduler.start_background(boot_run=config.SCHEDULER_BOOT_AUTORUN)
            except Exception:
                LOGGER.exception("Scheduler background startup failed.")

        async def watch_shutdown() -> None:
            while not shared.scheduler.shutdown_event.is_set():
                await asyncio.sleep(0.2)
            server = getattr(app.state, "uvicorn_server", None)
            if server is not None:
                server.should_exit = True
            LOGGER.info("Scheduler shutdown signal received by the API process.")

        watcher = asyncio.create_task(watch_shutdown(), name="daemoncut-shutdown-watcher")
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            shared.scheduler.stop_background()
            root_logger.removeHandler(handler)
            handler.close()
            LOGGER.info("DaemonCut API shutdown complete.")

    app = FastAPI(
        title="DaemonCut Local Studio API",
        version="0.4.0",
        description="Local-first creator studio and official multi-platform publisher.",
        lifespan=lifespan,
    )
    app.state.services = shared
    app.state.log_hub = hub
    app.state.bridge_token = secrets.token_urlsafe(32)
    app.state.uvicorn_server = None
    app.state.start_scheduler = should_start
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["*"])
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc), "color": config.UI_PALETTE["crimson"]}}), status_code=404, media_type="application/json")

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, exc: ConflictError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc), "color": config.UI_PALETTE["crimson"]}}), status_code=409, media_type="application/json")

    @app.exception_handler(DirectorChatError)
    async def director_error_handler(_request: Request, exc: DirectorChatError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=409, media_type="application/json")

    @app.exception_handler(APIRequestError)
    async def api_error_handler(_request: Request, exc: APIRequestError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=503, media_type="application/json")

    @app.exception_handler(SocialPublisherError)
    async def publisher_error_handler(_request: Request, exc: SocialPublisherError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=503, media_type="application/json")

    @app.exception_handler(ClipperValidationError)
    async def clipper_validation_handler(_request: Request, exc: ClipperValidationError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=422, media_type="application/json")

    @app.exception_handler(ClipperDependencyUnavailable)
    async def clipper_dependency_handler(_request: Request, exc: ClipperDependencyUnavailable) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=503, media_type="application/json")

    @app.exception_handler(ClipperError)
    async def clipper_error_handler(_request: Request, exc: ClipperError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=500, media_type="application/json")

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> Response:
        return Response(json.dumps({"detail": {"status": "error", "message": str(exc)[:500], "color": config.UI_PALETTE["crimson"]}}), status_code=422, media_type="application/json")

    @app.exception_handler(Exception)
    async def unhandled_handler(_request: Request, exc: Exception) -> Response:
        incident = uuid.uuid4().hex[:12]
        LOGGER.exception("Unhandled API error incident=%s.", incident, exc_info=exc)
        return Response(json.dumps({"detail": {"status": "error", "message": "The request could not be completed.", "incident_id": incident, "color": config.UI_PALETTE["crimson"]}}), status_code=500, media_type="application/json")

    @app.get("/", include_in_schema=False)
    async def index() -> dict[str, Any]:
        return {"name": "DaemonCut Local Studio API", "version": "0.4.0", "docs": "/docs", "bridge": "/bridge", "status": "active", "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.get("/health")
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "active", "service": "daemoncut-api", "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "database": shared.database.stats(), "scheduler": {"running": shared.scheduler.running, "shutdown_requested": shared.scheduler.shutdown_event.is_set()}, "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.get("/api/system")
    @app.get("/api/hardware")
    async def system_status() -> dict[str, Any]:
        root = config.GDRIVE_DAEMONCUT_PATH
        mounted = bool(root and root.is_dir())
        state = "ready" if mounted else "needs_configuration" if root is None else "unavailable"
        return {
            "status": state,
            "color": _status_color(state),
            "hardware": {"cpu_physical_cores": config.CPU_PHYSICAL_CORES, "cpu_logical_threads": config.CPU_LOGICAL_THREADS, "gpu": config.GPU_NAME, "gpu_vram_gb": config.GPU_VRAM_GB, "ram_gb": config.RAM_GB, "video_encoder": config.FFMPEG_H264_ENCODER, "upscaler_backend": config.REALESRGAN_BACKEND, "whisper_backend": config.WHISPER_CPP_BACKEND, "ollama_base_url": config.OLLAMA_BASE_URL},
            "drive": {"configured": root is not None, "mounted": mounted, "folder_name": root.name if root else None, "root_exposed": False},
            "api": {"workers": config.API_WORKERS, "threadpool_workers": config.API_THREADPOOL_WORKERS},
            "palette": dict(config.UI_PALETTE),
        }

    @app.get("/api/drive")
    async def drive_status() -> dict[str, Any]:
        result = await system_status()
        return result["drive"] | {"status": result["status"], "color": result["color"]}

    @app.get("/api/workspaces")
    async def list_workspaces() -> dict[str, Any]:
        return {"items": shared.database.list_workspaces()}

    @app.post("/api/workspaces")
    async def create_workspace(payload: WorkspaceCreateRequest) -> dict[str, Any]:
        workspace = await run_in_threadpool(shared.database.create_workspace, payload.name, timezone_name=payload.timezone_name, default_platform=payload.default_platform, settings=payload.settings, slug=payload.slug)
        return {"workspace": workspace, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.get("/api/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str) -> dict[str, Any]:
        return {"workspace": shared.database.get_workspace(workspace_id)}

    @app.get("/api/niches")
    async def list_niches(workspace_id: str | None = None) -> dict[str, Any]:
        workspaces = [shared.database.get_workspace(workspace_id)] if workspace_id else shared.database.list_workspaces()
        niches: list[dict[str, Any]] = []
        subscribed_ids: list[str] = []
        for workspace in workspaces:
            settings = workspace.get("settings", {})
            if not isinstance(settings, Mapping):
                continue
            workspace_niches = settings.get("niches", [])
            if isinstance(workspace_niches, list):
                niches.extend({**item, "workspace_id": workspace["workspace_id"]} for item in workspace_niches if isinstance(item, Mapping))
            if workspace_id:
                subscriptions = settings.get("subscribed_niche_ids", [])
                subscribed_ids = [str(item) for item in subscriptions] if isinstance(subscriptions, list) else []
        return {"niches": niches, "subscribed_ids": subscribed_ids}

    @app.post("/api/niches")
    async def save_niche(payload: NicheCreateRequest) -> dict[str, Any]:
        workspace = shared.database.get_workspace(payload.workspace_id)
        settings = dict(workspace.get("settings", {}))
        existing = settings.get("niches", [])
        niches = [dict(item) for item in existing if isinstance(item, Mapping)] if isinstance(existing, list) else []
        niche = {
            "id": uuid.uuid4().hex,
            "name": payload.name.strip(),
            "target_audience": payload.target_audience.strip(),
            "channel_promise": payload.channel_promise.strip(),
            "tone": payload.tone.strip(),
            "boundaries": [item.strip() for item in payload.boundaries if item.strip()],
            "blueprint": payload.blueprint,
            "series_bible": payload.series_bible,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        match = next((item for item in niches if str(item.get("name", "")).casefold() == niche["name"].casefold()), None)
        if match is not None:
            niche["id"] = str(match.get("id") or niche["id"])
            niches[niches.index(match)] = niche
        else:
            niches.append(niche)
        settings["niches"] = niches[-100:]
        updated = await run_in_threadpool(shared.database.update_workspace, workspace["workspace_id"], settings=settings)
        return {"niche": {**niche, "workspace_id": updated["workspace_id"]}, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/niches/subscriptions")
    async def update_niche_subscriptions(payload: NicheSubscriptionsRequest) -> dict[str, Any]:
        workspace = shared.database.get_workspace(payload.workspace_id)
        settings = dict(workspace.get("settings", {}))
        existing = settings.get("niches", [])
        allowed = {str(item.get("id")) for item in existing if isinstance(item, Mapping)} if isinstance(existing, list) else set()
        subscribed = sorted({str(item) for item in payload.niche_ids if str(item) in allowed})
        settings["subscribed_niche_ids"] = subscribed
        await run_in_threadpool(shared.database.update_workspace, workspace["workspace_id"], settings=settings)
        return {"subscribed_ids": subscribed, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/workspaces/{workspace_id}/channels")
    async def create_channel(workspace_id: str, payload: ChannelCreateRequest) -> dict[str, Any]:
        channel = await run_in_threadpool(shared.database.add_channel, workspace_id, payload.platform, payload.external_channel_id, display_name=payload.display_name, settings={"autopublish": payload.autopublish})
        return {"channel": channel, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.get("/api/workspaces/{workspace_id}/queue")
    async def workspace_queue(workspace_id: str, status: list[str] | None = Query(default=None), limit: int = Query(default=200, ge=1, le=2_000)) -> dict[str, Any]:
        return {"items": shared.database.list_queue(workspace_id, statuses=status, limit=limit)}

    @app.get("/api/workspaces/{workspace_id}/batches")
    async def list_batches(workspace_id: str, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        return {"items": shared.database.list_batches(workspace_id, limit=limit)}

    @app.post("/api/workspaces/{workspace_id}/episodes")
    async def create_episode(workspace_id: str, payload: EpisodeCreateRequest) -> dict[str, Any]:
        episode = await run_in_threadpool(shared.database.create_episode, workspace_id, topic=payload.topic, title=payload.title, hook=payload.hook, script=payload.script, scheduled_for=payload.scheduled_for, status=payload.status, metadata=payload.metadata)
        return {"episode": episode, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/workspaces/{workspace_id}/batches/saturday")
    async def create_saturday_batch(workspace_id: str, payload: BatchCreateRequest) -> dict[str, Any]:
        batch = await run_in_threadpool(shared.database.create_saturday_batch, workspace_id, payload.episodes, batch_date=payload.batch_date)
        LOGGER.info("Saturday batch queued: workspace=%s episode_count=%d.", workspace_id, len(batch.get("episodes", [])))
        return {"batch": batch, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.get("/api/batches/{batch_id}")
    async def get_batch(batch_id: str) -> dict[str, Any]:
        return {"batch": shared.database.get_batch(batch_id)}

    @app.get("/api/trends")
    async def get_trends(limit: int = Query(default=20, ge=1, le=100), force_refresh: bool = False, workspace_id: str | None = None) -> dict[str, Any]:
        recent_topics = shared.database.list_queue(workspace_id, limit=500) if workspace_id else []
        try:
            items = await run_in_threadpool(shared.trend_scout.scout, limit=limit, force_refresh=force_refresh, recent_topics=recent_topics)
        except Exception as exc:
            LOGGER.exception("Trend Scout request failed.")
            raise APIRequestError(f"Trend sources are unavailable: {type(exc).__name__}") from exc
        return {"items": [_serialize(item) for item in items], "source_errors": shared.trend_scout.last_errors}

    @app.post("/api/trends/variants")
    async def generate_trend_variants(payload: TrendVariantsRequest) -> dict[str, Any]:
        variants = await run_in_threadpool(shared.trend_scout.generate_variants, payload.topic, context=payload.context, audience=payload.audience, platform=payload.platform, language=payload.language, use_ai=payload.use_ai)
        return {"variants": variants}

    @app.post("/api/trends/scan-batch")
    async def scan_batch(payload: BatchScanRequest) -> dict[str, Any]:
        recent = shared.database.list_queue(payload.workspace_id, limit=1_000)
        return {"scan": shared.trend_scout.scan_seven_episode_batch(payload.episodes, recent_episodes=recent, threshold=payload.threshold)}

    @app.post("/api/studio/strategy")
    async def generate_strategy(payload: StrategyRequest) -> dict[str, Any]:
        blueprint = await run_in_threadpool(
            shared.brain.generate_strategy_blueprint,
            niche=payload.niche,
            target_audience=payload.target_audience,
            channel_promise=payload.channel_promise,
            platforms=payload.platforms,
            tone=payload.tone,
            content_boundaries=payload.content_boundaries,
            preferred_languages=payload.preferred_languages,
        )
        return {"blueprint": blueprint, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/studio/scripts")
    async def generate_script(payload: ScriptRequest) -> dict[str, Any]:
        script = await run_in_threadpool(shared.brain.generate_script, niche=payload.niche, topic=payload.topic, duration_seconds=payload.duration_seconds, style=payload.style, audience=payload.audience, language=payload.language, series_name=payload.series_name, scene_count=payload.scene_count)
        for scene in script.get("scenes", []):
            prompt = scene.get("video_prompt") if isinstance(scene, Mapping) else None
            if not isinstance(prompt, str) or len(prompt) > config.MAX_YT_CREATE_CHARS:
                raise ValueError("Generated scene prompt violates the 880-character video prompt law")
        if payload.include_reference_prompts:
            script["reference_prompts"] = await run_in_threadpool(shared.brain.generate_reference_prompts, character_description=payload.character_description, environment_description=payload.environment_description, art_style=payload.style, series_name=payload.series_name)
        script["prompt_limits"] = {"video_scene_max_chars": config.MAX_YT_CREATE_CHARS, "reference_image_prompts_capped": False}
        return {"script": script, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/studio/audio/synthesize")
    async def synthesize_audio(payload: AudioSynthesisRequest) -> dict[str, Any]:
        result = await run_in_threadpool(shared.audio_engine.synthesize, payload.text, mode=payload.mode, character=payload.character, profile_id=payload.profile_id, voice_id=payload.voice_id, manual_audio_path=payload.manual_audio_path, reference_audio_path=payload.reference_audio_path, output_path=payload.output_path)
        return {"audio": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/studio/audio/master")
    async def master_audio(payload: AudioMasterRequest) -> dict[str, Any]:
        result = await run_in_threadpool(shared.audio_engine.master_audio, payload.dialogue_path, output_path=payload.output_path, bgm_path=payload.bgm_path, sfx_events=payload.sfx_events, duration_seconds=payload.duration_seconds, bgm_gain_db=payload.bgm_gain_db, bgm_ducking=payload.bgm_ducking)
        return {"master": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/studio/subtitles")
    async def generate_subtitles(payload: SubtitleRequest) -> dict[str, Any]:
        if payload.resolution not in {"1080p", "1440p"}:
            raise ValueError("resolution must be 1080p or 1440p")
        resolution = (1440, 2560) if payload.resolution == "1440p" else (1080, 1920)
        result = await run_in_threadpool(shared.subtitle_engine.export_sidecars, payload.text, duration_seconds=payload.duration_seconds, audio_path=payload.audio_path, language_mode=payload.language_mode, resolution=resolution, output_ass=payload.output_ass, output_srt=payload.output_srt, use_brain=payload.use_brain)
        return {"subtitles": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/studio/video/render")
    async def render_video(payload: VideoRenderRequest) -> dict[str, Any]:
        if payload.resolution not in {"1080p", "1440p"}:
            raise ValueError("resolution must be 1080p or 1440p")
        resolution = (1440, 2560) if payload.resolution == "1440p" else (1080, 1920)
        clips = [ClipSpec(**clip) for clip in payload.clips]
        result = await run_in_threadpool(shared.video_editor.render, clips, output_path=payload.output_path, resolution=resolution, fps=payload.fps, transition=payload.transition, transition_duration=payload.transition_duration, lut_name=payload.lut_name, prefer_amf=payload.prefer_amf)
        return {"render": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/studio/thumbnails")
    async def create_thumbnails(payload: ThumbnailRequest) -> dict[str, Any]:
        if payload.video_path:
            result = await run_in_threadpool(shared.thumbnail_maker.make_from_video, payload.video_path, payload.hook, candidate_count=payload.candidate_count, use_brain=payload.use_brain, selection_context=payload.context)
        elif payload.image_path:
            result = await run_in_threadpool(shared.thumbnail_maker.make_thumbnails, payload.image_path, payload.hook)
        else:
            raise ValueError("video_path or image_path is required")
        return {"thumbnails": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/browser/inspect")
    async def inspect_browser_url(payload: BrowserInspectRequest) -> dict[str, Any]:
        result = await run_in_threadpool(shared.browser_cloner.inspect_url, payload.url, inspect_dom=payload.inspect_dom)
        return {"source": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/browser/clone")
    async def clone_browser_story(payload: BrowserCloneRequest) -> dict[str, Any]:
        result = await run_in_threadpool(shared.browser_cloner.clone_story, payload.url, niche=payload.niche, topic=payload.topic, duration_seconds=payload.duration_seconds, style=payload.style, audience=payload.audience, language=payload.language, scene_count=payload.scene_count, inspect_dom=payload.inspect_dom)
        return {"clone": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/director/chat")
    async def director_chat(payload: DirectorChatRequest) -> dict[str, Any]:
        result = await run_in_threadpool(shared.director_chat.chat, payload.message, context=payload.context)
        if not result.success:
            LOGGER.warning("Director tool %s did not complete.", result.tool or "unknown")
        status = "ready" if result.success else "attention"
        return {"response": _serialize(result), "status": status, "color": _status_color(status)}

    @app.post("/api/director/dictation/{session_id}/chunk")
    async def append_dictation_chunk(session_id: str, payload: DictationChunkRequest) -> dict[str, Any]:
        draft = shared.director_chat.append_speech_chunk(session_id, payload.text)
        return {"draft": _serialize(draft), "status": "active", "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.post("/api/director/dictation/{session_id}/pause")
    async def pause_dictation(session_id: str) -> dict[str, Any]:
        return {"draft": _serialize(shared.director_chat.pause_dictation(session_id)), "status": "attention", "color": config.UI_PALETTE["crimson"]}

    @app.post("/api/director/dictation/{session_id}/resume")
    async def resume_dictation(session_id: str) -> dict[str, Any]:
        return {"draft": _serialize(shared.director_chat.resume_dictation(session_id)), "status": "active", "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.delete("/api/director/dictation/{session_id}")
    async def clear_dictation(session_id: str) -> dict[str, Any]:
        return {"draft": _serialize(shared.director_chat.clear_draft(session_id)), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/director/dictation/{session_id}/finalize")
    async def finalize_dictation(session_id: str) -> dict[str, Any]:
        return {"text": shared.director_chat.finalize_draft(session_id), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/director/transcribe")
    async def transcribe_local_audio(file: UploadFile = File(...), language: str | None = Form(default=None)) -> dict[str, Any]:
        suffix = Path(file.filename or "chunk.wav").suffix.casefold()
        if suffix not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}:
            raise HTTPException(status_code=415, detail={"status": "error", "message": "Unsupported audio chunk format.", "color": config.UI_PALETTE["crimson"]})
        scratch = config.TEMP_DIR / f"dictation_{uuid.uuid4().hex}{suffix}"
        total = 0
        try:
            with scratch.open("xb") as handle:
                while True:
                    chunk = await file.read(config.API_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > TRANSCRIPTION_MAX_BYTES:
                        raise HTTPException(status_code=413, detail={"status": "error", "message": "Audio chunk exceeds the 64 MiB transcription limit.", "color": config.UI_PALETTE["crimson"]})
                    handle.write(chunk)
            if not total:
                raise HTTPException(status_code=400, detail={"status": "error", "message": "Audio chunk is empty.", "color": config.UI_PALETTE["crimson"]})
            result = await run_in_threadpool(shared.director_chat.transcribe_audio_chunk, scratch, language=language)
            return {"transcription": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}
        finally:
            scratch.unlink(missing_ok=True)
            await file.close()

    @app.post("/api/clipper/highlights")
    async def clipper_highlights(payload: ClipperRequest) -> dict[str, Any]:
        if payload.hook and len(payload.hook.split()) > 3:
            raise ValueError("PeakCut thumbnail overlay hooks are limited to three words")
        if bool(payload.source_url) == bool(payload.video_path):
            raise ValueError("Supply exactly one of source_url or video_path")
        if payload.source_url:
            media = await run_in_threadpool(shared.clipper.ingest_url, payload.source_url, start_seconds=payload.start_seconds, duration_seconds=payload.capture_duration_seconds, workspace_id=payload.workspace_id)
        else:
            media = await run_in_threadpool(shared.clipper.ingest_local_file, payload.video_path or "", title=payload.title)
        highlights = await run_in_threadpool(shared.clipper.find_highlights, media.path, transcript_segments=payload.transcript_segments, window_seconds=payload.window_seconds, step_seconds=payload.step_seconds, max_highlights=payload.max_highlights, use_brain=payload.use_brain)
        renders = []
        if payload.render:
            if payload.selected_highlights:
                chosen_highlights = [
                    HighlightSegment(
                        start_seconds=item.start_seconds,
                        end_seconds=item.end_seconds,
                        transcript=item.transcript,
                        audio_score=item.audio_score,
                        hook_score=item.hook_score,
                        viral_score=item.viral_score,
                        explanation=item.explanation,
                    )
                    for item in payload.selected_highlights
                ]
                if any(item.end_seconds > media.duration_seconds or not 15 <= item.duration_seconds <= 60 for item in chosen_highlights):
                    raise ValueError("selected highlights must be 15–60 seconds and within the ingested media duration")
            else:
                indices = sorted(set(payload.selected_highlight_indices))
                if any(index < 0 or index >= len(highlights) for index in indices):
                    raise ValueError("selected_highlight_indices must refer to ranked candidates from this request")
                chosen_highlights = [highlights[index] for index in indices] if indices else highlights
            for highlight in chosen_highlights:
                renders.append(await run_in_threadpool(shared.clipper.render_vertical_short, media.path, highlight, hook=payload.hook, framing_mode=payload.framing_mode, center_x=payload.center_x, language_mode=payload.language_mode, make_thumbnail=payload.make_thumbnail, lut_name=payload.lut_name))
        LOGGER.info("PeakCut found %d highlight candidates (render=%s).", len(highlights), payload.render)
        return {"media": _serialize(media), "highlights": _serialize(highlights), "renders": _serialize(renders), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.get("/api/scheduler/status")
    async def scheduler_status() -> dict[str, Any]:
        status = shared.scheduler.status()
        label = "active" if status["running"] else "ready"
        return {"scheduler": status, "status": label, "color": _status_color(label)}

    @app.post("/api/scheduler/run")
    async def scheduler_run(payload: SchedulerRunRequest) -> dict[str, Any]:
        result = await run_in_threadpool(shared.scheduler.run_once, wait_for_connectivity=payload.wait_for_connectivity, cleanup_scratch=payload.cleanup_scratch)
        return {"result": _serialize(result), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/scheduler/start")
    async def scheduler_start(payload: SchedulerStartRequest) -> dict[str, Any]:
        shared.scheduler.start_background(boot_run=payload.boot_run)
        return {"scheduler": shared.scheduler.status(), "status": "active", "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.post("/api/scheduler/stop")
    async def scheduler_stop() -> dict[str, Any]:
        shared.scheduler.stop_background()
        return {"scheduler": shared.scheduler.status(), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.post("/api/scheduler/shutdown")
    async def scheduler_shutdown() -> dict[str, Any]:
        shared.scheduler.request_clean_shutdown()
        return {"status": "attention", "shutdown_requested": True, "color": config.UI_PALETTE["crimson"]}

    @app.get("/api/oauth/{platform}/authorize")
    async def authorize_platform(platform: str) -> dict[str, Any]:
        result = await run_in_threadpool(shared.social_publisher.authorization_url, platform)
        return {**result, "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.get("/oauth/callback", response_class=HTMLResponse)
    async def oauth_callback(state: str = Query(default=""), code: str | None = None, error: str | None = None, error_description: str | None = None) -> HTMLResponse:
        del error_description
        try:
            result = await run_in_threadpool(shared.social_publisher.complete_oauth, state=state, code=code, error=error)
            platform = html.escape(str(result.get("platform", "platform")))
            message = f"{platform.title()} is connected. You may close this window."
            status = 200
        except (OAuthFlowError, SocialPublisherError, ValueError) as exc:
            message = f"Authorization was not completed: {str(exc)[:300]}"
            status = 400
        return HTMLResponse(f"<!doctype html><html><meta charset='utf-8'><title>DaemonCut OAuth</title><body style='font:16px system-ui;background:{config.UI_PALETTE['matte_obsidian']};color:{config.UI_PALETTE['warm_parchment']};padding:3rem'><h1>DaemonCut account connection</h1><p>{html.escape(message)}</p><p>You can close this tab and return to the console.</p></body></html>", status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/api/publish/status")
    async def publish_status() -> dict[str, Any]:
        return {"platforms": shared.social_publisher.platform_status(), "status": "ready", "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.post("/api/publish/episode/{episode_id}")
    async def publish_episode(episode_id: str, payload: PublishEpisodeRequest) -> dict[str, Any]:
        episode = shared.database.get_episode(episode_id)
        results = await run_in_threadpool(shared.social_publisher.publish_episode, episode, platforms=payload.platforms, idempotency_key=payload.idempotency_key)
        failed = any(item.get("status") == "failed" for item in results)
        return {"episode_id": episode_id, "results": results, "status": "attention" if failed else "ready", "color": config.UI_PALETTE["crimson"] if failed else config.UI_PALETTE["muted_brass"]}

    @app.get("/api/settings/env")
    async def settings_env() -> dict[str, Any]:
        return {**_settings_summary(), "status": "ready", "color": config.UI_PALETTE["deep_steel_blue"]}

    @app.put("/api/settings/env")
    async def update_settings_env(request: Request, payload: SettingsUpdateRequest) -> dict[str, Any]:
        if not _settings_client_is_local(request):
            raise HTTPException(status_code=403, detail={"status": "error", "message": "Settings may only be changed from the local DaemonCut console.", "color": config.UI_PALETTE["crimson"]})
        if not payload.updates:
            raise ValueError("updates must contain at least one allowlisted setting")
        unknown = set(payload.updates) - ENV_EDITABLE_KEYS
        if unknown:
            raise ValueError(f"Unsupported environment setting(s): {', '.join(sorted(unknown))}")
        cleaned: dict[str, str] = {}
        for key, raw in payload.updates.items():
            if not isinstance(raw, str) or len(raw) > 4_096 or any(char in raw for char in "\r\n\x00"):
                raise ValueError(f"{key} must be a single line of at most 4096 characters")
            value = raw.strip()
            if key == "META_API_ENABLED" and value.casefold() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("META_API_ENABLED must be true or false")
            cleaned[key] = value
        await run_in_threadpool(_write_local_env_updates, cleaned)
        for key, value in cleaned.items():
            os.environ[key] = value
            if key == "META_API_ENABLED":
                setattr(config, key, value.casefold() in {"true", "1", "yes", "on"})
            else:
                setattr(config, key, value)
        LOGGER.info("Local settings updated for keys: %s.", ", ".join(sorted(cleaned)))
        return {"updated_keys": sorted(cleaned), **_settings_summary(), "status": "ready", "color": config.UI_PALETTE["muted_brass"]}

    @app.get("/bridge", response_class=HTMLResponse)
    async def bridge_page(request: Request, token: str | None = Query(default=None)) -> HTMLResponse:
        expected = app.state.bridge_token
        peer = request.client.host if request.client else ""
        if token is None and peer not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail={"status": "error", "message": "Open the bridge on the studio PC or scan its current QR link.", "color": config.UI_PALETTE["crimson"]})
        if token is not None and not secrets.compare_digest(token, expected):
            raise HTTPException(status_code=403, detail={"status": "error", "message": "This bridge link is no longer valid.", "color": config.UI_PALETTE["crimson"]})
        bridge_url = _bridge_url(request, expected)
        page = _bridge_html(bridge_url=bridge_url, token=expected, qr_svg=_qr_svg(bridge_url))
        return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    @app.get("/bridge/qr.svg")
    async def bridge_qr(token: str = Query(...)) -> Response:
        if not secrets.compare_digest(token, app.state.bridge_token):
            raise HTTPException(status_code=403, detail={"status": "error", "message": "Invalid bridge token.", "color": config.UI_PALETTE["crimson"]})
        url = f"http://{detect_lan_ip() or '127.0.0.1'}:{config.API_PORT}/bridge?{urlencode({'token': token})}"
        return Response(_qr_svg(url), media_type="image/svg+xml", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/bridge/upload")
    async def bridge_upload(file: UploadFile = File(...), x_bridge_token: str | None = Header(default=None, alias="X-Bridge-Token")) -> dict[str, Any]:
        if not x_bridge_token or not secrets.compare_digest(x_bridge_token, app.state.bridge_token):
            raise HTTPException(status_code=403, detail={"status": "error", "message": "A valid local bridge token is required.", "color": config.UI_PALETTE["crimson"]})
        original_name = (file.filename or "upload")[:255]
        path, size, digest = await _stream_upload_to_drive(file)
        return {"status": "uploaded", "color": config.UI_PALETTE["muted_brass"], "filename": original_name, "stored_name": path.name, "size_bytes": size, "sha256": digest, "storage": "source_media/phone_uploads"}

    @app.websocket("/ws/logs")
    async def log_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        subscriber = await hub.subscribe()
        try:
            for event in hub.snapshot(limit=200):
                await websocket.send_json(event)

            async def sender() -> None:
                while True:
                    await websocket.send_json(await subscriber[1].get())

            async def receiver() -> None:
                while True:
                    await websocket.receive_text()

            send_task = asyncio.create_task(sender())
            receive_task = asyncio.create_task(receiver())
            done, pending = await asyncio.wait({send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done | pending:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await task
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(subscriber)

    return app


app = create_app()


def run() -> None:
    """Start a single Uvicorn worker; multiple workers duplicate scheduler state."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("Uvicorn is required; install requirements.txt") from exc
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = uvicorn.Server(uvicorn.Config(app, host=config.API_HOST, port=config.API_PORT, workers=config.API_WORKERS, access_log=False, log_config=None, timeout_keep_alive=15))
    app.state.uvicorn_server = server
    LOGGER.info("Starting DaemonCut on %s:%d (workers=%d).", config.API_HOST, config.API_PORT, config.API_WORKERS)
    server.run()


if __name__ == "__main__":
    run()


__all__ = ["LogHub", "RequestSizeLimitMiddleware", "ServiceContainer", "app", "create_app", "create_services", "detect_lan_ip", "run"]
