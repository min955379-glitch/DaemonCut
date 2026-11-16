"""Director copilot, allowlisted studio tool routing, and pause-safe dictation."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from backend import config
from backend.brain_gemini import AIProviderUnavailable, AIResponseError, Brain
from backend.browser_cloner import BrowserCloner
from backend.clipper_engine import ClipperEngine
from backend.audio_engine import AudioEngine, VoiceMode
from backend.subtitle_engine import SubtitleEngine
from backend.thumbnail_maker import ThumbnailMaker
from backend.video_editor import ClipSpec, VideoEditor


class DirectorChatError(RuntimeError):
    """Base error for dictation and director tool dispatch."""


class ToolRoutingError(DirectorChatError, ValueError):
    """An unknown tool or invalid tool argument was rejected."""


@dataclass(frozen=True)
class DictationDraft:
    session_id: str
    text: str
    paused: bool
    updated_at: float
    chunk_count: int


@dataclass(frozen=True)
class ToolPlan:
    tool: str | None
    arguments: Mapping[str, Any]
    reply: str
    source: str


@dataclass(frozen=True)
class DirectorResponse:
    reply: str
    tool: str | None
    result: Any = None
    success: bool = True
    plan_source: str = "deterministic"


class DictationBuffer:
    """Thread-safe, disk-persistent drafts; pauses never discard buffered text.

    Small transcript text is stored atomically beneath ``.daemoncut`` so a
    local API restart or Windows sleep does not erase an active dictation.
    Drafts expire after 24 hours and audio chunks are never persisted here.
    """

    def __init__(
        self,
        *,
        max_sessions: int = 512,
        ttl_seconds: int = 86_400,
        max_chars: int = 100_000,
        state_path: str | Path | None = config.RUNTIME_DIR / "dictation_sessions.json",
    ) -> None:
        if not 1 <= max_sessions <= 10_000 or not 60 <= ttl_seconds <= 604_800 or not 1 <= max_chars <= 1_000_000:
            raise ValueError("Dictation buffer limits are outside the supported range")
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.max_chars = max_chars
        self.state_path = Path(state_path).expanduser().resolve(strict=False) if state_path is not None else None
        self._lock = threading.RLock()
        self._drafts: dict[str, DictationDraft] = {}
        self._load()

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", session_id):
            raise ValueError("session_id may contain only letters, numbers, underscores, and hyphens")
        return session_id

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw_drafts = payload.get("drafts", []) if isinstance(payload, dict) else []
            if not isinstance(raw_drafts, list):
                return
            for item in raw_drafts:
                if not isinstance(item, dict):
                    continue
                session_id = item.get("session_id")
                text = item.get("text")
                updated_at = item.get("updated_at")
                chunk_count = item.get("chunk_count", 0)
                if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", session_id):
                    continue
                if not isinstance(text, str) or len(text) > self.max_chars:
                    continue
                try:
                    updated = float(updated_at)
                    chunks = int(chunk_count)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(updated) or chunks < 0:
                    continue
                self._drafts[session_id] = DictationDraft(session_id, text, bool(item.get("paused", False)), updated, chunks)
            with self._lock:
                pruned = self._prune_locked(time.time())
                if pruned:
                    self._persist_locked()
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            logging.getLogger("daemoncut.director").warning("Could not load persisted dictation drafts (%s).", type(exc).__name__)

    def _persist_locked(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "drafts": [
                {
                    "session_id": draft.session_id,
                    "text": draft.text,
                    "paused": draft.paused,
                    "updated_at": draft.updated_at,
                    "chunk_count": draft.chunk_count,
                }
                for draft in sorted(self._drafts.values(), key=lambda value: value.updated_at)
            ],
        }
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _prune_locked(self, now: float) -> bool:
        changed = False
        expired = [key for key, draft in self._drafts.items() if now - draft.updated_at > self.ttl_seconds]
        for key in expired:
            self._drafts.pop(key, None)
            changed = True
        if len(self._drafts) > self.max_sessions:
            oldest = sorted(self._drafts.values(), key=lambda item: item.updated_at)
            for draft in oldest[: len(self._drafts) - self.max_sessions]:
                self._drafts.pop(draft.session_id, None)
                changed = True
        return changed

    def get(self, session_id: str) -> DictationDraft:
        key = self._validate_session_id(session_id)
        with self._lock:
            now = time.time()
            if self._prune_locked(now):
                self._persist_locked()
            return self._drafts.get(key, DictationDraft(key, "", False, now, 0))

    def append_speech_chunk(self, session_id: str, text: str) -> DictationDraft:
        key = self._validate_session_id(session_id)
        if not isinstance(text, str):
            raise ValueError("speech chunk must be text")
        chunk = re.sub(r"\s+", " ", text).strip()
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            draft = self._drafts.get(key, DictationDraft(key, "", False, now, 0))
            if draft.paused:
                raise DirectorChatError("Dictation is paused; resume the session before appending speech")
            separator = " " if draft.text and chunk else ""
            combined = draft.text + separator + chunk
            if len(combined) > self.max_chars:
                raise ValueError(f"Dictation draft exceeds the {self.max_chars}-character limit")
            updated = DictationDraft(key, combined, False, now, draft.chunk_count + (1 if chunk else 0))
            self._drafts[key] = updated
            self._persist_locked()
            return updated

    def pause_dictation(self, session_id: str) -> DictationDraft:
        key = self._validate_session_id(session_id)
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            draft = self._drafts.get(key, DictationDraft(key, "", False, now, 0))
            updated = DictationDraft(key, draft.text, True, now, draft.chunk_count)
            self._drafts[key] = updated
            self._persist_locked()
            return updated

    def resume_dictation(self, session_id: str) -> DictationDraft:
        key = self._validate_session_id(session_id)
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            draft = self._drafts.get(key, DictationDraft(key, "", False, now, 0))
            updated = DictationDraft(key, draft.text, False, now, draft.chunk_count)
            self._drafts[key] = updated
            self._persist_locked()
            return updated

    def clear_draft(self, session_id: str) -> DictationDraft:
        key = self._validate_session_id(session_id)
        with self._lock:
            now = time.time()
            updated = DictationDraft(key, "", False, now, 0)
            self._drafts[key] = updated
            self._persist_locked()
            return updated

    def finalize_draft(self, session_id: str) -> str:
        key = self._validate_session_id(session_id)
        with self._lock:
            draft = self._drafts.pop(key, None)
            self._persist_locked()
            return draft.text if draft else ""


class DirectorChat:
    """Natural-language copilot with safe, fixed-name studio tool dispatch."""

    TOOL_DESCRIPTIONS = {
        "create_script_and_prompts": "Generate an original script/storyboard and optional E1/E2/E3/E3B references.",
        "clone_url_story": "Inspect a public short-video link and adapt only abstract narrative structure.",
        "clip_long_video": "Score and render highlight clips from a local video or supported public VOD URL.",
        "edit_video_scene": "Trim, speed-adjust, frame, LUT-grade, and render a video scene.",
        "generate_voice_and_master": "Synthesize or override dialogue and optionally master it with BGM/SFX.",
        "generate_subtitles": "Generate ASS karaoke and SRT sidecars from narration/audio.",
        "generate_thumbnails": "Create the cover and 16:9 thumbnail from an image or rendered video.",
    }

    def __init__(
        self,
        *,
        brain: Brain | None = None,
        dictation: DictationBuffer | None = None,
        clipper: ClipperEngine | None = None,
        browser_cloner: BrowserCloner | None = None,
        audio_engine: AudioEngine | None = None,
        subtitle_engine: SubtitleEngine | None = None,
        video_editor: VideoEditor | None = None,
        thumbnail_maker: ThumbnailMaker | None = None,
    ) -> None:
        self.brain = brain or Brain()
        self.dictation = dictation or DictationBuffer()
        self.clipper = clipper or ClipperEngine(brain=self.brain)
        self.browser_cloner = browser_cloner or BrowserCloner(brain=self.brain)
        self.audio_engine = audio_engine or AudioEngine()
        self.subtitle_engine = subtitle_engine or SubtitleEngine(brain=self.brain)
        self.video_editor = video_editor or VideoEditor()
        self.thumbnail_maker = thumbnail_maker or ThumbnailMaker(brain=self.brain, video_editor=self.video_editor)

    def transcribe_audio_chunk(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        whisper_model: str = "base.en",
    ) -> dict[str, Any]:
        """Local Whisper helper used when a browser speech API is unavailable."""
        return self.subtitle_engine.transcribe_audio(audio_path, language=language, whisper_model=whisper_model)

    def _plan_with_brain(self, message: str, context: Mapping[str, Any] | None = None) -> ToolPlan:
        try:
            response = self.brain.generate_json(
                "You are a local video-studio assistant. Select at most one tool from the supplied allowlist; "
                "never invent tool names or execute shell commands. If a tool needs information not supplied, "
                "ask a concise follow-up in reply and return tool=null. Return JSON with tool, arguments object, reply.",
                json.dumps({
                    "message": message[:4_000],
                    "context": dict(context or {}),
                    "tools": self.TOOL_DESCRIPTIONS,
                }, ensure_ascii=False),
                expected_type=dict,
                temperature=0.1,
            )
            tool = response.get("tool")
            if tool in {None, "", "none"}:
                return ToolPlan(None, {}, str(response.get("reply") or "How would you like to work on this video?"), "brain")
            if not isinstance(tool, str) or tool not in self.TOOL_DESCRIPTIONS:
                raise ToolRoutingError("Brain returned a tool outside the allowlist")
            arguments = response.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise ToolRoutingError("Tool arguments must be a JSON object")
            return ToolPlan(tool, dict(arguments), str(response.get("reply") or "Working on that now."), "brain")
        except (AIProviderUnavailable, AIResponseError, ValueError, TypeError, AttributeError):
            return self._deterministic_plan(message, context or {})

    def _deterministic_plan(self, message: str, context: Mapping[str, Any]) -> ToolPlan:
        text = message.strip()
        lowered = text.casefold()
        url_match = re.search(r"https?://\S+", text)
        url = url_match.group(0).rstrip(".,!?)]\"") if url_match else context.get("url")
        common = dict(context)
        if url:
            common.setdefault("url", url)
        if url and any(word in lowered for word in ("clone", "structure", "remake", "inspire")):
            return ToolPlan("clone_url_story", common, "I’ll use only the source’s broad pacing and beat structure, then create original content.", "deterministic")
        if url and any(word in lowered for word in ("clip", "highlight", "short")):
            return ToolPlan("clip_long_video", common, "I’ll score candidate moments and render a vertical short.", "deterministic")
        if any(word in lowered for word in ("thumbnail", "cover image", "cover art")):
            return ToolPlan("generate_thumbnails", common, "I’ll create the cover and 16:9 thumbnail.", "deterministic")
        if any(word in lowered for word in ("subtitle", "caption", "karaoke")):
            return ToolPlan("generate_subtitles", common, "I’ll prepare the subtitle sidecars.", "deterministic")
        if any(word in lowered for word in ("voice", "narration", "master audio")):
            return ToolPlan("generate_voice_and_master", common, "I’ll prepare dialogue audio and its master.", "deterministic")
        if any(word in lowered for word in ("edit", "trim", "speed", "frame")):
            return ToolPlan("edit_video_scene", common, "I’ll edit the requested scene using the supplied parameters.", "deterministic")
        if any(word in lowered for word in ("script", "storyboard", "episode", "write a story")):
            return ToolPlan("create_script_and_prompts", common, "I’ll create an original script and validated scene prompts.", "deterministic")
        return ToolPlan(None, {}, "I can help write a script, adapt a public URL’s structure, clip a VOD, edit a scene, create audio/subtitles, or make thumbnails.", "deterministic")

    @staticmethod
    def _require(arguments: Mapping[str, Any], key: str) -> Any:
        value = arguments.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ToolRoutingError(f"Tool requires {key!r}")
        return value

    def _create_script_and_prompts(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        niche = str(self._require(arguments, "niche"))
        topic = str(self._require(arguments, "topic"))
        result = self.brain.generate_script(
            niche=niche,
            topic=topic,
            duration_seconds=int(arguments.get("duration_seconds", 45)),
            style=str(arguments.get("style", "")),
            audience=str(arguments.get("audience", "general audience")),
            language=str(arguments.get("language", "English")),
            series_name=arguments.get("series_name"),
            scene_count=int(arguments.get("scene_count", 5)),
        )
        for scene in result.get("scenes", []):
            if len(scene.get("video_prompt", "")) > config.MAX_YT_CREATE_CHARS:
                raise DirectorChatError("Brain returned a scene prompt above the configured character limit")
        if arguments.get("include_reference_prompts"):
            result["reference_prompts"] = self.brain.generate_reference_prompts(
                character_description=str(arguments.get("character_description", "")),
                environment_description=str(arguments.get("environment_description", "")),
                art_style=str(arguments.get("style", "")),
                series_name=arguments.get("series_name"),
            )
        return result

    def _clip_long_video(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("video_path"):
            media = self.clipper.ingest_local_file(arguments["video_path"])
        else:
            media = self.clipper.ingest_url(str(self._require(arguments, "url")), duration_seconds=arguments.get("capture_duration_seconds"))
        highlights = self.clipper.find_highlights(
            media.path,
            transcript_segments=arguments.get("transcript_segments", ()),
            window_seconds=float(arguments.get("window_seconds", 30)),
            max_highlights=int(arguments.get("max_highlights", 1)),
            use_brain=bool(arguments.get("use_brain", True)),
        )
        rendered = [
            self.clipper.render_vertical_short(
                media.path,
                highlight,
                hook=arguments.get("hook"),
                framing_mode=arguments.get("framing_mode", "whole_blur"),
                center_x=float(arguments.get("center_x", 0.5)),
                language_mode=str(arguments.get("language_mode", "english")),
                make_thumbnail=bool(arguments.get("make_thumbnail", True)),
            )
            for highlight in highlights[: int(arguments.get("max_highlights", 1))]
        ]
        return {"media": media, "highlights": highlights, "renders": rendered}

    def _edit_video_scene(self, arguments: Mapping[str, Any]) -> Any:
        path = self._require(arguments, "video_path")
        start = float(arguments.get("start_seconds", 0.0))
        end = float(self._require(arguments, "end_seconds"))
        speed = float(arguments.get("speed", 1.0))
        framing = arguments.get("framing_mode", "whole_blur")
        clip = ClipSpec(
            path=path,
            start_seconds=start,
            end_seconds=end,
            speed=speed,
            framing_mode=framing,
            center_x=float(arguments.get("center_x", 0.5)),
            motion_effect=str(arguments.get("motion_effect", "none")),
            include_audio=bool(arguments.get("include_audio", False)),
        )
        return self.video_editor.render(
            [clip],
            resolution=(1440, 2560) if arguments.get("resolution") == "1440p" else (1080, 1920),
            lut_name=arguments.get("lut_name"),
        )

    def _generate_voice_and_master(self, arguments: Mapping[str, Any]) -> Any:
        synthesis = self.audio_engine.synthesize(
            str(self._require(arguments, "text")),
            mode=arguments.get("mode", VoiceMode.AUTO_CAST),
            character=arguments.get("character"),
            profile_id=arguments.get("profile_id"),
            voice_id=arguments.get("voice_id"),
            manual_audio_path=arguments.get("manual_audio_path"),
            reference_audio_path=arguments.get("reference_audio_path"),
            output_path=arguments.get("output_path"),
        )
        if not arguments.get("master", False):
            return synthesis
        return self.audio_engine.master_audio(
            synthesis.path,
            output_path=arguments.get("master_output_path"),
            bgm_path=arguments.get("bgm_path"),
            duration_seconds=arguments.get("duration_seconds"),
        )

    def _generate_subtitles(self, arguments: Mapping[str, Any]) -> Any:
        return self.subtitle_engine.export_sidecars(
            str(self._require(arguments, "text")),
            duration_seconds=float(self._require(arguments, "duration_seconds")),
            audio_path=arguments.get("audio_path"),
            language_mode=str(arguments.get("language_mode", "english")),
            resolution=(1440, 2560) if arguments.get("resolution") == "1440p" else (1080, 1920),
            use_brain=bool(arguments.get("use_brain", True)),
        )

    def _generate_thumbnails(self, arguments: Mapping[str, Any]) -> Any:
        if arguments.get("video_path"):
            return self.thumbnail_maker.make_from_video(
                arguments["video_path"],
                str(self._require(arguments, "hook")),
                candidate_count=int(arguments.get("candidate_count", 7)),
                use_brain=bool(arguments.get("use_brain", False)),
                selection_context=str(arguments.get("context", "")),
            )
        frame = self._require(arguments, "image_path")
        return self.thumbnail_maker.make_thumbnails(frame, str(self._require(arguments, "hook")))

    def invoke_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if name not in self.TOOL_DESCRIPTIONS:
            raise ToolRoutingError(f"Unknown tool {name!r}; available tools are {sorted(self.TOOL_DESCRIPTIONS)}")
        if not isinstance(arguments, Mapping):
            raise ToolRoutingError("Tool arguments must be a mapping")
        dispatch: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "create_script_and_prompts": self._create_script_and_prompts,
            "clone_url_story": lambda args: self.browser_cloner.clone_story(
                str(self._require(args, "url")),
                niche=str(args.get("niche", "original short-form storytelling")),
                topic=args.get("topic"),
                duration_seconds=int(args.get("duration_seconds", 45)),
                style=str(args.get("style", "original 3D animation")),
                audience=str(args.get("audience", "general audience")),
                language=str(args.get("language", "English")),
                scene_count=int(args.get("scene_count", 5)),
            ),
            "clip_long_video": self._clip_long_video,
            "edit_video_scene": self._edit_video_scene,
            "generate_voice_and_master": self._generate_voice_and_master,
            "generate_subtitles": self._generate_subtitles,
            "generate_thumbnails": self._generate_thumbnails,
        }
        return dispatch[name](arguments)

    def chat(self, message: str, *, context: Mapping[str, Any] | None = None) -> DirectorResponse:
        if not isinstance(message, str) or not message.strip() or len(message) > 20_000:
            raise ValueError("message must be non-empty text at most 20000 characters")
        plan = self._plan_with_brain(message, context)
        if plan.tool is None:
            return DirectorResponse(plan.reply, None, None, True, plan.source)
        try:
            result = self.invoke_tool(plan.tool, plan.arguments)
        except Exception as exc:
            return DirectorResponse(
                reply=f"{plan.tool} could not complete: {type(exc).__name__}: {str(exc)[:500]}",
                tool=plan.tool,
                result=None,
                success=False,
                plan_source=plan.source,
            )
        return DirectorResponse(plan.reply, plan.tool, result, True, plan.source)

    def append_speech_chunk(self, session_id: str, text: str) -> DictationDraft:
        return self.dictation.append_speech_chunk(session_id, text)

    def pause_dictation(self, session_id: str) -> DictationDraft:
        return self.dictation.pause_dictation(session_id)

    def resume_dictation(self, session_id: str) -> DictationDraft:
        return self.dictation.resume_dictation(session_id)

    def clear_draft(self, session_id: str) -> DictationDraft:
        return self.dictation.clear_draft(session_id)

    def finalize_draft(self, session_id: str) -> str:
        return self.dictation.finalize_draft(session_id)


__all__ = [
    "DictationBuffer",
    "DictationDraft",
    "DirectorChat",
    "DirectorChatError",
    "DirectorResponse",
    "ToolPlan",
    "ToolRoutingError",
]
