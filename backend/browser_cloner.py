"""Public-page inspection and ethical narrative-structure adaptation.

The module never requires cookies or authenticated browser profiles. It keeps
subtitle scratch files in ``config.TEMP_DIR`` and sends only abstract pacing
structure—not copied dialogue—to the script generator.
"""
from __future__ import annotations

import html
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
import threading
from typing import Any, Mapping
from urllib.parse import urlsplit

from backend import config
from backend.brain_gemini import AIProviderUnavailable, AIResponseError, Brain


class BrowserClonerError(RuntimeError):
    """Base error for public metadata inspection and structure analysis."""


class BrowserClonerDependencyUnavailable(BrowserClonerError):
    """An optional scraper, downloader, or browser backend is unavailable."""


class BrowserClonerValidationError(BrowserClonerError, ValueError):
    """An unsafe or unsupported URL/request was rejected."""


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class SourceSummary:
    url: str
    source_type: str
    title: str
    creator: str
    duration_seconds: float | None
    description: str
    transcript_segments: tuple[TranscriptSegment, ...]
    page_text: str
    metadata: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    def to_dict(self, *, include_transcript: bool = True) -> dict[str, Any]:
        result = asdict(self)
        if not include_transcript:
            result.pop("transcript_segments", None)
        return result


@dataclass(frozen=True)
class NarrativeStructure:
    duration_seconds: float | None
    hook_pattern: str
    pacing: str
    emotional_arc: tuple[str, ...]
    beats: tuple[Mapping[str, Any], ...]
    originality_note: str = "Adapt narrative pacing only; do not reuse source dialogue, characters, names, or distinctive scenes."


@dataclass(frozen=True)
class CloneStoryResult:
    source: SourceSummary
    structure: NarrativeStructure
    script: Mapping[str, Any]
    original_content_only: bool = True


_PLATFORM_SUFFIXES = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
)
_VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts")
_TIME_RE = re.compile(r"(?:(\d{2,}):)?(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(?:(\d{2,}):)?(\d{2}):(\d{2})[,.](\d{3})")
_TAG_RE = re.compile(r"<[^>]*>")


def validate_public_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip() or len(url) > 2_048:
        raise BrowserClonerValidationError("url must be a public platform URL under 2048 characters")
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise BrowserClonerValidationError("Only credential-free http(s) URLs are accepted")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserClonerValidationError("URL port is malformed") from exc
    if port not in {None, 80, 443}:
        raise BrowserClonerValidationError("Non-standard URL ports are not allowed")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _PLATFORM_SUFFIXES):
        raise BrowserClonerValidationError("Supported public hosts are YouTube, TikTok, Instagram, Facebook, and fb.watch")
    return url.strip()


def _timestamp(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.description = ""
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = dict(attrs)
        if tag.casefold() == "title":
            self._in_title = True
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag.casefold() == "meta":
            name = (normalized.get("name") or normalized.get("property") or "").casefold()
            content = normalized.get("content") or ""
            if name in {"description", "og:description", "twitter:description"} and content and not self.description:
                self.description = content.strip()[:2_000]
            if name in {"og:title", "twitter:title"} and content and not self.title:
                self.title = content.strip()[:300]

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text[:300]
        if not self._skip_depth:
            self.parts.append(text)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BrowserCloner:
    """Inspect platform metadata/captions and derive original story scaffolds."""

    def __init__(self, *, brain: Brain | None = None, ydl_factory: Any = None) -> None:
        self.brain = brain or Brain()
        self._ydl_factory = ydl_factory
        # Limit concurrent Chromium launches even when several API requests
        # arrive together on the six-thread workstation.
        self._playwright_lock = threading.BoundedSemaphore(1)

    def _ydl(self, options: Mapping[str, Any]) -> Any:
        factory = self._ydl_factory
        if factory is None:
            try:
                import yt_dlp  # type: ignore[import-not-found]
            except ImportError as exc:
                raise BrowserClonerDependencyUnavailable("yt-dlp is not installed") from exc
            factory = yt_dlp.YoutubeDL
        return factory(dict(options))

    @staticmethod
    def parse_vtt(text: str, *, max_segments: int = 2_000) -> tuple[TranscriptSegment, ...]:
        segments: list[TranscriptSegment] = []
        lines = text.replace("\r", "").split("\n")
        index = 0
        while index < len(lines) and len(segments) < max_segments:
            match = _TIME_RE.search(lines[index])
            if not match:
                index += 1
                continue
            start = _timestamp(match.group(1), match.group(2), match.group(3), match.group(4))
            end = _timestamp(match.group(5), match.group(6), match.group(7), match.group(8))
            index += 1
            caption_lines: list[str] = []
            while index < len(lines) and lines[index].strip():
                if "-->" in lines[index]:
                    break
                caption_lines.append(lines[index].strip())
                index += 1
            cleaned = html.unescape(_TAG_RE.sub("", " ".join(caption_lines)))
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned and end > start:
                if segments and cleaned == segments[-1].text and start - segments[-1].end_seconds < 0.15:
                    previous = segments[-1]
                    segments[-1] = TranscriptSegment(previous.start_seconds, end, previous.text)
                else:
                    segments.append(TranscriptSegment(start, end, cleaned[:1_000]))
        return tuple(segments)

    def _extract_with_ytdlp(self, url: str) -> tuple[Mapping[str, Any] | None, tuple[TranscriptSegment, ...], list[str]]:
        warnings: list[str] = []
        config.ensure_runtime_dirs()
        with tempfile.TemporaryDirectory(prefix="browser-clone-", dir=config.TEMP_DIR) as scratch:
            root = Path(scratch)
            options = {
                "quiet": True,
                "no_warnings": True,
                "ignoreconfig": True,
                "noplaylist": True,
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en", "en-US", "en-GB", "ur", "hi"],
                "subtitlesformat": "vtt/best",
                "outtmpl": str(root / "%(id)s.%(ext)s"),
                "socket_timeout": config.BROWSER_SCRAPE_TIMEOUT_SECONDS,
                "retries": 1,
                "fragment_retries": 1,
                "max_filesize": 20 * 1024 * 1024,
            }
            try:
                with self._ydl(options) as downloader:
                    info = downloader.extract_info(url, download=True)
            except BrowserClonerDependencyUnavailable:
                raise
            except Exception as exc:
                warnings.append(f"yt-dlp metadata/caption extraction failed: {type(exc).__name__}")
                return None, (), warnings
            transcript: list[TranscriptSegment] = []
            for caption_file in root.glob("*.vtt"):
                try:
                    transcript.extend(self.parse_vtt(caption_file.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
            transcript.sort(key=lambda item: item.start_seconds)
            return info if isinstance(info, Mapping) else None, tuple(transcript[:2_000]), warnings

    def _inspect_playwright(self, url: str) -> tuple[str, str, str] | None:
        if not self._playwright_lock.acquire(timeout=min(2.0, config.BROWSER_SCRAPE_TIMEOUT_SECONDS)):
            return None
        try:
            return self._inspect_playwright_locked(url)
        finally:
            self._playwright_lock.release()

    def _inspect_playwright_locked(self, url: str) -> tuple[str, str, str] | None:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-dev-shm-usage",
                        "--disable-background-networking",
                        "--renderer-process-limit=6",
                        "--num-raster-threads=6",
                    ],
                    timeout=config.BROWSER_SCRAPE_TIMEOUT_SECONDS * 1_000,
                )
                try:
                    context = browser.new_context(
                        java_script_enabled=True,
                        accept_downloads=False,
                        service_workers="block",
                        user_agent="DaemonCut-PublicMetadata/1.0",
                    )
                    def route_guard(route: Any) -> None:
                        try:
                            validate_public_url(route.request.url)
                            route.continue_()
                        except BrowserClonerValidationError:
                            route.abort()
                    context.route("**/*", route_guard)
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=config.BROWSER_SCRAPE_TIMEOUT_SECONDS * 1_000)
                    validate_public_url(page.url)
                    page_title = page.title()[:300]
                    description = page.locator('meta[name="description"],meta[property="og:description"]').first.get_attribute("content", timeout=2_000) or ""
                    text = page.locator("body").inner_text(timeout=3_000)[:20_000]
                    return page_title, description[:2_000], re.sub(r"\s+", " ", text).strip()
                finally:
                    browser.close()
        except Exception:
            return None

    def _inspect_urllib(self, url: str) -> tuple[str, str, str] | None:
        validate_public_url(url)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 DaemonCut public metadata inspector", "Accept": "text/html"},
        )
        opener = urllib.request.build_opener(_PublicRedirectHandler())
        try:
            with opener.open(request, timeout=config.BROWSER_SCRAPE_TIMEOUT_SECONDS) as response:
                if response.status >= 400:
                    return None
                raw = response.read(2_000_000)
                content_type = response.headers.get("Content-Type", "").casefold()
                if "html" not in content_type:
                    return None
        except (urllib.error.URLError, TimeoutError, OSError, BrowserClonerValidationError):
            return None
        parser = _PageTextParser()
        try:
            parser.feed(raw.decode("utf-8", errors="replace"))
        except Exception:
            return None
        return parser.title[:300], parser.description[:2_000], " ".join(parser.parts)[:20_000]

    @staticmethod
    def _source_type(url: str, metadata: Mapping[str, Any] | None) -> str:
        if metadata and (metadata.get("duration") or metadata.get("is_live")):
            return "video"
        path = urlsplit(url).path.casefold()
        if any(token in path for token in ("/shorts/", "/reel/", "/watch", "/video/", "/videos/")):
            return "video"
        return "creator_page"

    def inspect_url(self, url: str, *, inspect_dom: bool = True) -> SourceSummary:
        safe_url = validate_public_url(url)
        metadata: Mapping[str, Any] | None = None
        transcript: tuple[TranscriptSegment, ...] = ()
        warnings: list[str] = []
        try:
            metadata, transcript, ytdlp_warnings = self._extract_with_ytdlp(safe_url)
            warnings.extend(ytdlp_warnings)
        except BrowserClonerDependencyUnavailable as exc:
            warnings.append(str(exc))
        dom = self._inspect_playwright(safe_url) if inspect_dom else None
        if dom is None:
            dom = self._inspect_urllib(safe_url)
        dom_title, dom_description, page_text = dom or ("", "", "")
        info = metadata or {}
        duration_raw = info.get("duration")
        try:
            duration = float(duration_raw) if duration_raw is not None else None
            if duration is not None and (duration <= 0 or duration > 86_400):
                duration = None
        except (TypeError, ValueError):
            duration = None
        description = str(info.get("description") or dom_description or "")[:5_000]
        title = str(info.get("title") or dom_title or "Public video")[:300]
        creator = str(info.get("uploader") or info.get("channel") or "Unknown creator")[:200]
        allowed_metadata = {
            key: info[key]
            for key in ("id", "extractor_key", "upload_date", "view_count", "like_count", "categories")
            if key in info and isinstance(info[key], (str, int, float, list, dict))
        }
        if not metadata and not page_text:
            warnings.append("No public metadata was available; source title and transcript may be incomplete.")
        return SourceSummary(
            url=safe_url,
            source_type=self._source_type(safe_url, metadata),
            title=title,
            creator=creator,
            duration_seconds=duration,
            description=description,
            transcript_segments=transcript,
            page_text=page_text,
            metadata=allowed_metadata,
            warnings=tuple(warnings),
        )

    @staticmethod
    def analyze_structure(source: SourceSummary) -> NarrativeStructure:
        duration = source.duration_seconds or max((segment.end_seconds for segment in source.transcript_segments), default=0.0) or None
        segments = source.transcript_segments
        combined = " ".join(segment.text for segment in segments)
        lower = combined.casefold()
        if "?" in combined[:200] or any(cue in lower[:200] for cue in ("why", "how", "what if")):
            hook = "question"
        elif any(cue in lower[:250] for cue in ("wait", "unexpected", "suddenly", "turns out")):
            hook = "surprise"
        elif "you" in lower[:250] or "your" in lower[:250]:
            hook = "direct_address"
        else:
            hook = "curiosity_or_visual_claim"
        if len(segments) >= 8:
            gaps = [max(0.0, segments[index + 1].start_seconds - segments[index].end_seconds) for index in range(len(segments) - 1)]
            density = sum(1 for gap in gaps if gap < 0.35) / max(1, len(gaps))
            pacing = "fast" if density >= 0.65 else "measured" if density < 0.3 else "varied"
        else:
            pacing = "varied"
        duration_value = duration or 30.0
        beat_specs = (
            ("hook", 0.00, 0.10),
            ("setup", 0.10, 0.32),
            ("escalation", 0.32, 0.70),
            ("payoff", 0.70, 0.90),
            ("closing_beat", 0.90, 1.00),
        )
        beats: list[dict[str, Any]] = []
        for name, start_ratio, end_ratio in beat_specs:
            start = duration_value * start_ratio
            end = duration_value * end_ratio
            beats.append({"beat": name, "start_seconds": round(start, 2), "end_seconds": round(end, 2)})
        return NarrativeStructure(
            duration_seconds=duration,
            hook_pattern=hook,
            pacing=pacing,
            emotional_arc=("curiosity", "tension_or_question", "payoff", "warm_close"),
            beats=tuple(beats),
        )

    @staticmethod
    def _fallback_script(
        brain: Brain,
        *,
        niche: str,
        topic: str,
        duration_seconds: int,
        style: str,
        audience: str,
        language: str,
        structure: NarrativeStructure,
    ) -> dict[str, Any]:
        names = ["Ari", "Sam"]
        stages = [
            ("hook", "A small surprise appears before the characters know what caused it."),
            ("setup", "The friends describe the problem in their own words and choose a simple test."),
            ("attempt", "Their first idea creates a harmless complication, so they compare what changed."),
            ("payoff", "They combine their observations to make an original, practical solution."),
            ("close", "The result earns a kind joke and a satisfying final image."),
        ]
        scene_count = 5
        scenes = []
        for index, (beat, visual) in enumerate(stages, start=1):
            prompt = (
                f"Original {style or niche} scene, beat {beat}: {visual} Use newly invented characters {names[0]} and {names[1]}, "
                "distinct outfits and props, family-friendly tone, clear vertical composition, no logos or copied source dialogue."
            )
            prompt = brain.enforce_video_scene_prompt_limit(prompt, max_retries=0)
            scenes.append({
                "scene_number": index,
                "duration_seconds": max(2, duration_seconds // scene_count),
                "narration": "",
                "visual_description": visual,
                "video_prompt": prompt,
                "prompt_char_count": len(prompt),
                "on_screen_text": "",
            })
        return {
            "title": f"An Original {niche.title()} Story",
            "hook": "What if a small mistake revealed a clever solution?",
            "script": f"{topic}. Two original friends investigate, test ideas, learn from a harmless setback, and solve the problem together.",
            "description": f"An original, family-friendly {niche} story for {audience}, written in {language}.",
            "tags": [niche, "original story"],
            "scenes": scenes,
            "prompt_limits": {"video_scene_max_chars": config.MAX_YT_CREATE_CHARS, "reference_image_prompts_capped": False},
            "structure_reference": {"hook_pattern": structure.hook_pattern, "pacing": structure.pacing},
        }

    def clone_story(
        self,
        url: str,
        *,
        niche: str,
        topic: str | None = None,
        duration_seconds: int = 45,
        style: str = "original short-form animation",
        audience: str = "general audience",
        language: str = "English",
        scene_count: int = 5,
        inspect_dom: bool = True,
    ) -> CloneStoryResult:
        source = self.inspect_url(url, inspect_dom=inspect_dom)
        structure = self.analyze_structure(source)
        requested_topic = (topic or f"An original {niche} story with a fresh situation inspired by a broad {structure.hook_pattern} hook and {structure.pacing} pacing").strip()
        context = {
            "reference_structure_only": {
                "hook_pattern": structure.hook_pattern,
                "pacing": structure.pacing,
                "emotional_arc": list(structure.emotional_arc),
                "beat_timing": [dict(beat) for beat in structure.beats],
            },
            "originality_guard": (
                "Do not copy source dialogue, wording, names, likenesses, characters, props, or distinctive scenes. "
                "Use only abstract beat ordering, pacing, and emotional progression; invent a fresh premise and cast."
            ),
        }
        try:
            script = self.brain.generate_script(
                niche=niche,
                topic=requested_topic,
                duration_seconds=duration_seconds,
                style=style,
                audience=audience,
                language=language,
                workspace_context=context,
                scene_count=scene_count,
            )
        except (AIProviderUnavailable, AIResponseError):
            script = self._fallback_script(
                self.brain,
                niche=niche,
                topic=requested_topic,
                duration_seconds=duration_seconds,
                style=style,
                audience=audience,
                language=language,
                structure=structure,
            )
        # Revalidate even custom Brain adapters: the scene limit law is absolute.
        for scene in script.get("scenes", []):
            if isinstance(scene, dict) and isinstance(scene.get("video_prompt"), str):
                scene["video_prompt"] = self.brain.enforce_video_scene_prompt_limit(scene["video_prompt"], max_retries=0)
                scene["prompt_char_count"] = len(scene["video_prompt"])
        script["originality_guard"] = context["originality_guard"]
        script["reference_structure"] = context["reference_structure_only"]
        return CloneStoryResult(source=source, structure=structure, script=script)


__all__ = [
    "BrowserCloner",
    "BrowserClonerDependencyUnavailable",
    "BrowserClonerError",
    "BrowserClonerValidationError",
    "CloneStoryResult",
    "NarrativeStructure",
    "SourceSummary",
    "TranscriptSegment",
    "validate_public_url",
]
