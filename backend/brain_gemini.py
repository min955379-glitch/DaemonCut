"""Gemini-first/local-Ollama AI engine for DaemonCut.

Gemini is optional. With no API key (or when Gemini fails), the engine uses an
Ollama model on the local machine when available. Reference-image prompts are
not subject to the YouTube Create scene limit; every returned scene prompt is
normalized and validated against ``MAX_YT_CREATE_CHARS`` (880).
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend import config


class BrainError(RuntimeError):
    """Base class for generation and validation failures."""


class AIProviderUnavailable(BrainError):
    """No configured provider could complete a request."""


class AIResponseError(BrainError):
    """A provider returned an empty or structurally invalid response."""


class Brain:
    """Content and multimodal QA engine with automatic local fallback."""

    def __init__(self, *, provider: str | None = None) -> None:
        self.provider = (provider or config.AI_PROVIDER).strip().casefold()
        if self.provider not in {"auto", "gemini", "ollama"}:
            raise ValueError("provider must be 'auto', 'gemini', or 'ollama'")
        self._gemini_client: Any = None
        self._gemini_types: Any = None

    @staticmethod
    def load_series_bible(path: str | Path = config.SERIES_BIBLE_PATH) -> dict[str, Any]:
        bible_path = Path(path)
        try:
            data = json.loads(bible_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BrainError(f"Series bible not found: {bible_path}") from exc
        except json.JSONDecodeError as exc:
            raise BrainError(f"Series bible is not valid JSON at line {exc.lineno}: {exc.msg}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("series"), dict):
            raise BrainError("Series bible must contain a top-level 'series' object")
        return data

    def _provider_plan(self, requested: str | None = None) -> list[str]:
        choice = (requested or self.provider).strip().casefold()
        if choice not in {"auto", "gemini", "ollama"}:
            raise ValueError("provider must be 'auto', 'gemini', or 'ollama'")
        if choice == "ollama":
            return ["ollama"] if config.OLLAMA_ENABLED else []
        if choice == "gemini":
            return ["gemini"] + (["ollama"] if config.OLLAMA_ENABLED else [])
        if config.GEMINI_API_KEY:
            return ["gemini"] + (["ollama"] if config.OLLAMA_ENABLED else [])
        return ["ollama"] if config.OLLAMA_ENABLED else []

    def _get_gemini_client(self) -> tuple[Any, Any]:
        if self._gemini_client is not None:
            return self._gemini_client, self._gemini_types
        if not config.GEMINI_API_KEY:
            raise AIProviderUnavailable("GEMINI_API_KEY is not set")
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai import types  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AIProviderUnavailable(
                "The optional google-genai package is not installed; run setup.bat or pip install -r requirements.txt"
            ) from exc
        try:
            http_options = types.HttpOptions(timeout=config.GEMINI_TIMEOUT_SECONDS * 1000)
            self._gemini_client = genai.Client(
                api_key=config.GEMINI_API_KEY,
                http_options=http_options,
            )
        except Exception as exc:  # SDK can raise different auth/config errors by version.
            raise AIProviderUnavailable(f"Gemini client initialization failed: {type(exc).__name__}") from exc
        self._gemini_types = types
        return self._gemini_client, types

    @staticmethod
    def _response_text(response: Any) -> str:
        try:
            text = response.text
        except Exception:
            text = None
        if isinstance(text, str) and text.strip():
            return text.strip()
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                value = getattr(part, "text", None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise AIResponseError("Gemini returned no text content")

    def _gemini_text(
        self,
        system_instruction: str,
        user_prompt: str,
        *,
        json_output: bool,
        temperature: float,
    ) -> str:
        client, types = self._get_gemini_client()
        options: dict[str, Any] = {
            "system_instruction": system_instruction,
            "temperature": max(0.0, min(float(temperature), 2.0)),
        }
        if json_output:
            options["response_mime_type"] = "application/json"
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL_ID,
                contents=user_prompt,
                config=types.GenerateContentConfig(**options),
            )
        except Exception as exc:
            raise AIProviderUnavailable(f"Gemini request failed: {type(exc).__name__}") from exc
        return self._response_text(response)

    @staticmethod
    def _ollama_http(payload: dict[str, Any], *, json_output: bool) -> dict[str, Any]:
        url = f"{config.OLLAMA_BASE_URL}/api/chat"
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=config.OLLAMA_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(2_000).decode("utf-8", errors="replace")
            if json_output and exc.code in {400, 404, 422} and "format" in payload:
                # Older Ollama versions do not understand format=json. The
                # prompt still explicitly asks for JSON, so retry compatibly.
                retry_payload = dict(payload)
                retry_payload.pop("format", None)
                return Brain._ollama_http(retry_payload, json_output=False)
            raise AIProviderUnavailable(f"Ollama HTTP {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIProviderUnavailable(f"Ollama is unreachable at {config.OLLAMA_BASE_URL}") from exc
        except json.JSONDecodeError as exc:
            raise AIResponseError("Ollama returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise AIResponseError("Ollama returned a non-object response")
        if data.get("error"):
            raise AIProviderUnavailable(f"Ollama error: {str(data['error'])[:300]}")
        return data

    def _ollama_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_output: bool,
        temperature: float,
        model: str | None = None,
    ) -> str:
        if not config.OLLAMA_ENABLED:
            raise AIProviderUnavailable("OLLAMA_ENABLED is false")
        payload: dict[str, Any] = {
            "model": model or config.OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": config.ollama_options(temperature=temperature),
        }
        if json_output:
            payload["format"] = "json"
        data = self._ollama_http(payload, json_output=json_output)
        message = data.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise AIResponseError("Ollama returned an empty message")
        return content.strip()

    def _ollama_text(
        self,
        system_instruction: str,
        user_prompt: str,
        *,
        json_output: bool,
        temperature: float,
    ) -> str:
        return self._ollama_chat(
            [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt},
            ],
            json_output=json_output,
            temperature=temperature,
        )

    def generate_text(
        self,
        system_instruction: str,
        user_prompt: str,
        *,
        json_output: bool = False,
        temperature: float = 0.4,
        provider: str | None = None,
    ) -> str:
        """Generate text; Gemini errors fall back to local Ollama when enabled."""
        if not isinstance(system_instruction, str) or not system_instruction.strip():
            raise ValueError("system_instruction cannot be empty")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("user_prompt cannot be empty")
        failures: list[str] = []
        plan = self._provider_plan(provider)
        if not plan:
            raise AIProviderUnavailable(
                "No AI provider is enabled. Configure GEMINI_API_KEY or enable a local Ollama service."
            )
        for backend in plan:
            try:
                if backend == "gemini":
                    return self._gemini_text(
                        system_instruction,
                        user_prompt,
                        json_output=json_output,
                        temperature=temperature,
                    )
                return self._ollama_text(
                    system_instruction,
                    user_prompt,
                    json_output=json_output,
                    temperature=temperature,
                )
            except (AIProviderUnavailable, AIResponseError) as exc:
                failures.append(f"{backend}: {exc}")
        raise AIProviderUnavailable("All AI providers failed. " + " | ".join(failures))

    @staticmethod
    def _strip_json_wrappers(text: str) -> str:
        cleaned = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        return cleaned

    @staticmethod
    def _extract_balanced_json(text: str) -> str | None:
        """Find the first complete object/array, ignoring delimiters in strings."""
        start = -1
        opening = ""
        stack: list[str] = []
        in_string = False
        escaped = False
        for index, char in enumerate(text):
            if start < 0:
                if char in "{[":
                    start = index
                    opening = char
                    stack.append("}" if char == "{" else "]")
                continue
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if not stack or char != stack[-1]:
                    return None
                stack.pop()
                if not stack:
                    candidate = text[start : index + 1]
                    try:
                        json.loads(candidate)
                    except json.JSONDecodeError:
                        return None
                    return candidate
        return None

    def generate_json(
        self,
        system_instruction: str,
        user_prompt: str,
        *,
        expected_type: type | tuple[type, ...] | None = dict,
        temperature: float = 0.3,
        provider: str | None = None,
    ) -> Any:
        """Generate and parse JSON, tolerating common Markdown wrappers."""
        prompt = user_prompt.rstrip() + "\n\nReturn only valid JSON. Do not wrap it in Markdown fences."
        text = self.generate_text(
            system_instruction,
            prompt,
            json_output=True,
            temperature=temperature,
            provider=provider,
        )
        cleaned = self._strip_json_wrappers(text)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            balanced = self._extract_balanced_json(cleaned)
            if balanced is None:
                raise AIResponseError("AI response did not contain valid JSON")
            parsed = json.loads(balanced)
        if expected_type is not None and not isinstance(parsed, expected_type):
            raise AIResponseError(
                f"AI returned {type(parsed).__name__}; expected {expected_type}"
            )
        return parsed

    @staticmethod
    def _string(value: Any, *, field: str, required: bool = True) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise AIResponseError(f"{field} must be text")
        result = value.strip()
        if required and not result:
            raise AIResponseError(f"{field} cannot be empty")
        return result

    @staticmethod
    def _scene_brief(scene: str | Mapping[str, Any]) -> dict[str, str]:
        if isinstance(scene, str):
            text = scene.strip()
            if not text:
                raise ValueError("scene description cannot be empty")
            return {"visual_description": text}
        if not isinstance(scene, Mapping):
            raise ValueError("scene must be text or a mapping")
        keys = (
            "scene_number",
            "visual_description",
            "description",
            "action",
            "setting",
            "characters",
            "camera",
            "lighting",
            "style",
            "narration",
            "duration_seconds",
        )
        brief: dict[str, str] = {}
        for key in keys:
            value = scene.get(key)
            if isinstance(value, (str, int, float)):
                text = str(value).strip()
                if text:
                    brief[key] = text
            elif isinstance(value, (list, tuple)):
                text = ", ".join(str(part).strip() for part in value if str(part).strip())
                if text:
                    brief[key] = text
        if not any(brief.get(key) for key in ("visual_description", "description", "action", "setting")):
            raise ValueError("scene requires a visual_description, description, action, or setting")
        return brief

    @staticmethod
    def _compact_prompt(prompt: str, limit: int) -> str:
        """Deterministic last-resort cap used only after model condensing retries."""
        text = re.sub(r"\s+", " ", prompt).strip()
        if len(text) <= limit:
            return text
        if limit <= 1:
            return "…"[:limit]
        target = limit - 1  # reserve one character for an ellipsis
        minimum_boundary = int(target * 0.72)
        sentence_ends = [text.rfind(mark, 0, target) for mark in (". ", "! ", "? ", "; ")]
        sentence_end = max(sentence_ends)
        if sentence_end >= minimum_boundary:
            cut = sentence_end + 1
        else:
            space = text.rfind(" ", 0, target)
            cut = space if space >= minimum_boundary else target
        compacted = text[:cut].rstrip(" ,;:-") + "…"
        return compacted[:limit]

    def enforce_video_scene_prompt_limit(
        self,
        prompt: str,
        *,
        scene_context: str = "",
        max_retries: int = 3,
    ) -> str:
        """Condense oversized prompts and always return at most 880 characters.

        It first asks the configured provider(s) to preserve essential action,
        character continuity, setting, camera and lighting. If no provider is
        reachable or model output remains too long, a deterministic final cap
        guarantees the hard limit before this function returns.
        """
        if not isinstance(prompt, str):
            raise ValueError("video scene prompt must be text")
        if not 0 <= max_retries <= 8:
            raise ValueError("max_retries must be between 0 and 8")
        current = re.sub(r"\s+", " ", prompt).strip()
        if not current:
            raise ValueError("video scene prompt cannot be empty")
        if len(current) <= config.MAX_YT_CREATE_CHARS:
            return current
        system = (
            "You are a precise video-scene prompt editor. Condense the supplied prompt while preserving its "
            "main visible action, named characters and continuity details, setting, camera, and essential lighting. "
            f"The output must be plain text and no more than {config.MAX_YT_CREATE_CHARS} characters including spaces. "
            "Remove repetition and ornamental wording before removing essential scene facts. Return only the prompt."
        )
        for _ in range(max_retries):
            if len(current) <= config.MAX_YT_CREATE_CHARS:
                break
            user = (
                f"Scene context (use only if helpful):\n{scene_context[:4_000]}\n\n"
                f"Current prompt ({len(current)} characters):\n{current}\n\n"
                f"Rewrite this scene prompt in at most {config.MAX_YT_CREATE_CHARS} characters."
            )
            try:
                candidate = self.generate_text(system, user, temperature=0.1).strip().strip('"')
            except (AIProviderUnavailable, AIResponseError):
                break
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if not candidate:
                break
            if len(candidate) < len(current):
                current = candidate
            else:
                # The model failed to make progress; still attempt a bounded
                # deterministic condensation rather than looping indefinitely.
                current = self._compact_prompt(current, config.MAX_YT_CREATE_CHARS)
                break
        if len(current) > config.MAX_YT_CREATE_CHARS:
            current = self._compact_prompt(current, config.MAX_YT_CREATE_CHARS)
        if len(current) > config.MAX_YT_CREATE_CHARS:
            raise AssertionError("scene prompt validator failed to enforce its configured maximum")
        return current

    def generate_video_scene_prompt(
        self,
        scene: str | Mapping[str, Any],
        *,
        reference_context: str = "",
        style: str = "",
    ) -> str:
        """Generate one YouTube Create scene prompt and enforce the 880-char cap."""
        brief = self._scene_brief(scene)
        scene_context = json.dumps(brief, ensure_ascii=False)
        system = (
            "Write one production-ready prompt for a single short video scene. Be concrete about visible action, "
            "composition, camera, lighting, and relevant character continuity. Do not add dialogue unless requested. "
            f"The final prompt must be no more than {config.MAX_YT_CREATE_CHARS} characters. Return only plain text."
        )
        user = (
            f"Scene brief:\n{scene_context}\n\n"
            f"Visual reference context:\n{reference_context.strip() or 'None supplied'}\n\n"
            f"Art direction:\n{style.strip() or brief.get('style', 'Consistent polished 3D animation')}\n\n"
            f"Write a concise prompt with a hard maximum of {config.MAX_YT_CREATE_CHARS} characters."
        )
        try:
            generated = self.generate_text(system, user, temperature=0.35).strip().strip('"')
        except AIProviderUnavailable:
            # Usable offline even when Ollama is not installed: preserve the
            # explicit visual brief rather than returning a fake AI result.
            pieces = [
                brief.get("visual_description") or brief.get("description") or brief.get("action"),
                brief.get("setting"),
                brief.get("characters"),
                brief.get("camera"),
                brief.get("lighting"),
                style.strip() or brief.get("style"),
            ]
            generated = "; ".join(piece for piece in pieces if piece)
        if not generated:
            raise AIResponseError("No usable video scene prompt was generated")
        return self.enforce_video_scene_prompt_limit(generated, scene_context=scene_context)

    def generate_script(
        self,
        *,
        niche: str,
        topic: str,
        duration_seconds: int = 60,
        style: str = "",
        audience: str = "general audience",
        language: str = "English",
        series_name: str | None = None,
        series_bible: Mapping[str, Any] | None = None,
        workspace_context: Mapping[str, Any] | None = None,
        scene_count: int = 5,
    ) -> dict[str, Any]:
        """Create a script and storyboard with a validated <=880-char prompt per scene."""
        niche = self._string(niche, field="niche")
        topic = self._string(topic, field="topic")
        if not 10 <= int(duration_seconds) <= 3_600:
            raise ValueError("duration_seconds must be between 10 and 3600")
        if not 1 <= int(scene_count) <= 40:
            raise ValueError("scene_count must be between 1 and 40")
        if workspace_context is not None and not isinstance(workspace_context, Mapping):
            raise ValueError("workspace_context must be a mapping")
        bible = dict(series_bible) if series_bible is not None else self.load_series_bible()
        series = bible.get("series", {}) if isinstance(bible, Mapping) else {}
        templates = bible.get("templates", {}) if isinstance(bible, Mapping) else {}
        chosen_series = series.get(series_name) if series_name and isinstance(series, Mapping) else None
        if series_name and chosen_series is None and isinstance(templates, Mapping):
            chosen_series = templates.get(series_name)
        if series_name and chosen_series is None:
            raise ValueError(f"Series or template {series_name!r} is not present in the series bible")
        if chosen_series is None and not series_name and isinstance(series, Mapping) and len(series) == 1:
            chosen_series = next(iter(series.values()))
        bible_context = chosen_series if chosen_series is not None else (templates.get("custom_universal_niche", {}) if isinstance(templates, Mapping) else {})
        prompt_payload = {
            "niche": niche,
            "topic": topic,
            "duration_seconds": int(duration_seconds),
            "style": style.strip(),
            "audience": audience.strip(),
            "language": language.strip(),
            "scene_count": int(scene_count),
            "series_context": bible_context,
            "workspace_context": dict(workspace_context or {}),
        }
        system = (
            "You are DaemonCut's short-form story editor. Create coherent, original, audience-appropriate scripts. "
            "Honor supplied series canon and workspace boundaries. Do not promise virality or invent current facts. "
            "Return a JSON object with: title (string), hook (string), script (string), description (string), "
            "tags (array of strings), and scenes (array). Each scene must contain scene_number, duration_seconds, "
            "narration, visual_description, and video_prompt. Video prompts should be concise, visual, and independent; "
            f"each must be at most {config.MAX_YT_CREATE_CHARS} characters. The backend will validate and condense them."
        )
        output = self.generate_json(
            system,
            "Create the script and storyboard for this brief:\n" + json.dumps(prompt_payload, ensure_ascii=False),
            expected_type=dict,
            temperature=0.65,
        )
        title = self._string(output.get("title"), field="title")
        hook = self._string(output.get("hook"), field="hook")
        script = self._string(output.get("script"), field="script")
        description = self._string(output.get("description", ""), field="description", required=False)
        tags = output.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise AIResponseError("tags must be an array of strings")
        raw_scenes = output.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise AIResponseError("scenes must be a non-empty array")
        if len(raw_scenes) > 40:
            raise AIResponseError("scenes may not exceed 40 entries")
        scenes: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_scenes, start=1):
            if not isinstance(raw, Mapping):
                raise AIResponseError(f"scenes[{index}] must be an object")
            visual = self._string(
                raw.get("visual_description", raw.get("description", raw.get("video_prompt", ""))),
                field=f"scenes[{index}].visual_description",
            )
            prompt = raw.get("video_prompt", raw.get("create_prompt", ""))
            if not isinstance(prompt, str) or not prompt.strip():
                prompt = visual
            prompt = self.enforce_video_scene_prompt_limit(
                prompt,
                scene_context=json.dumps(dict(raw), ensure_ascii=False),
            )
            narration = self._string(raw.get("narration", ""), field=f"scenes[{index}].narration", required=False)
            try:
                scene_duration = max(1, min(600, int(raw.get("duration_seconds", duration_seconds / len(raw_scenes)))))
            except (TypeError, ValueError):
                scene_duration = max(1, int(duration_seconds / len(raw_scenes)))
            scenes.append(
                {
                    "scene_number": index,
                    "duration_seconds": scene_duration,
                    "narration": narration,
                    "visual_description": visual,
                    "video_prompt": prompt,
                    "prompt_char_count": len(prompt),
                    "on_screen_text": self._string(
                        raw.get("on_screen_text", ""),
                        field=f"scenes[{index}].on_screen_text",
                        required=False,
                    ),
                }
            )
        return {
            "title": title,
            "hook": hook,
            "script": script,
            "description": description,
            "tags": [tag.strip() for tag in tags if tag.strip()],
            "scenes": scenes,
            "prompt_limits": {
                "video_scene_max_chars": config.MAX_YT_CREATE_CHARS,
                "reference_image_prompts_capped": False,
            },
        }

    def generate_reference_prompts(
        self,
        *,
        character_description: str = "",
        environment_description: str = "",
        art_style: str = "",
        series_name: str | None = None,
        series_bible: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """Generate unlimited-length E1/E2/E3/E3B visual continuity prompts."""
        bible = dict(series_bible) if series_bible is not None else self.load_series_bible()
        series = bible.get("series", {}) if isinstance(bible, Mapping) else {}
        templates = bible.get("templates", {}) if isinstance(bible, Mapping) else {}
        if series_name:
            context = series.get(series_name, {}) if isinstance(series, Mapping) else {}
            if not context and isinstance(templates, Mapping):
                context = templates.get(series_name, {})
            if not context:
                raise ValueError(f"Series or template {series_name!r} is not present in the series bible")
        elif isinstance(series, Mapping) and len(series) == 1:
            context = next(iter(series.values()))
        else:
            context = dict(templates.get("custom_universal_niche", {})) if isinstance(templates, Mapping) else {}
        user_context = {
            "series_canon": context,
            "character_description": character_description.strip(),
            "environment_description": environment_description.strip(),
            "art_style": art_style.strip(),
            "reference_slots": ["E1", "E2", "E3", "E3B"],
        }
        system = (
            "You are a visual-continuity prompt engineer. Produce four exceptionally detailed, independent reference-image "
            "prompts labelled E1, E2, E3, and E3B. E1/E2 establish individual character identity, anatomy, clothing and "
            "signature details; E3 establishes the environment; E3B shows the characters together while preserving their "
            "canonical differences. Include composition, camera/lens, lighting, materials, palette and atmosphere where "
            "useful. Be exhaustive rather than terse. There is no 880-character limit for these reference prompts. "
            "Do not add facts that contradict supplied canon. Return a JSON object whose four keys map to full prompt strings."
        )
        output = self.generate_json(
            system,
            "Create the reference prompts from this canon:\n" + json.dumps(user_context, ensure_ascii=False),
            expected_type=dict,
            temperature=0.55,
        )
        normalized = {str(key).upper(): value for key, value in output.items()}
        prompts: dict[str, str] = {}
        for key in ("E1", "E2", "E3", "E3B"):
            value = normalized.get(key)
            if not isinstance(value, str) or not value.strip():
                raise AIResponseError(f"Reference prompt {key} is missing or empty")
            # Intentionally no MAX_YT_CREATE_CHARS check or truncation here.
            prompts[key] = value.strip()
        return prompts

    def generate_strategy_blueprint(
        self,
        *,
        niche: str,
        target_audience: str,
        channel_promise: str,
        platforms: Sequence[str] = ("YouTube Shorts",),
        tone: str = "",
        content_boundaries: Sequence[str] = (),
        preferred_languages: Sequence[str] = ("English",),
    ) -> dict[str, Any]:
        """Generate a reusable strategy blueprint for a custom niche."""
        niche = self._string(niche, field="niche")
        target_audience = self._string(target_audience, field="target_audience")
        channel_promise = self._string(channel_promise, field="channel_promise")
        if not platforms or any(not isinstance(item, str) or not item.strip() for item in platforms):
            raise ValueError("platforms must contain at least one non-empty string")
        if not preferred_languages or any(not isinstance(item, str) or not item.strip() for item in preferred_languages):
            raise ValueError("preferred_languages must contain at least one non-empty string")
        payload = {
            "niche": niche,
            "target_audience": target_audience,
            "channel_promise": channel_promise,
            "platforms": list(platforms),
            "tone": tone.strip(),
            "content_boundaries": [str(item).strip() for item in content_boundaries if str(item).strip()],
            "preferred_languages": list(preferred_languages),
        }
        system = (
            "Create an actionable, ethical content strategy blueprint, not a promise of reach. Return valid JSON with "
            "positioning, audience_insights, content_pillars (3-5), repeatable_formats (at least 4), hook_principles, "
            "cadence, testing_plan, production_workflow, safety_and_accuracy_rules, and measurement_plan. "
            "Respect the supplied niche boundaries and distinguish hypotheses from facts."
        )
        result = self.generate_json(
            system,
            "Build a strategy blueprint for:\n" + json.dumps(payload, ensure_ascii=False),
            expected_type=dict,
            temperature=0.55,
        )
        required = (
            "positioning",
            "content_pillars",
            "repeatable_formats",
            "hook_principles",
            "cadence",
            "testing_plan",
            "safety_and_accuracy_rules",
            "measurement_plan",
        )
        missing = [key for key in required if key not in result]
        if missing:
            raise AIResponseError(f"Strategy blueprint is missing fields: {', '.join(missing)}")
        return result

    def generate_captions(
        self,
        text: str,
        *,
        mode: str = "english",
        max_chars_per_line: int = 42,
    ) -> dict[str, Any]:
        """Translate/transliterate caption copy into English, Roman Urdu, or Roman Hindi."""
        source = self._string(text, field="text")
        if len(source) > 30_000:
            raise ValueError("text must be at most 30000 characters")
        mode = mode.strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {"romanurdu": "roman_urdu", "roman hindi": "roman_hindi", "roman_hindi": "roman_hindi"}
        mode = aliases.get(mode, mode)
        if mode not in {"english", "roman_urdu", "roman_hindi"}:
            raise ValueError("mode must be 'english', 'roman_urdu', or 'roman_hindi'")
        if not 12 <= int(max_chars_per_line) <= 80:
            raise ValueError("max_chars_per_line must be between 12 and 80")
        instructions = {
            "english": "Write natural, concise English captions. Preserve meaning, names, and emotional tone.",
            "roman_urdu": "Translate into natural conversational Roman Urdu written only in Latin characters. Do not use Urdu/Arabic or Devanagari script.",
            "roman_hindi": "Translate into natural conversational Roman Hindi written only in Latin characters. Do not use Devanagari or Urdu/Arabic script.",
        }
        system = (
            "You are a careful caption editor. Preserve factual meaning, speaker intent, names, and sequence. "
            "Break captions into readable lines and do not add explanatory commentary. "
            "Return only the finished caption text. " + instructions[mode]
        )
        caption = self.generate_text(
            system,
            f"Target mode: {mode}\nMaximum preferred line length: {int(max_chars_per_line)} characters.\n\nSource text:\n{source}",
            temperature=0.25,
        ).strip()
        if not caption:
            raise AIResponseError("Caption generation returned empty text")
        return {
            "mode": mode,
            "script": "Latin",
            "caption": caption,
            "max_chars_per_line": int(max_chars_per_line),
        }

    def generate_hook_title_variants(
        self,
        *,
        topic: str,
        context: str = "",
        audience: str = "general audience",
        platform: str = "YouTube Shorts",
        language: str = "English",
    ) -> dict[str, list[dict[str, str]]]:
        """Create exactly three labelled hook variants and three title variants."""
        topic = self._string(topic, field="topic")
        system = (
            "Generate a restrained A/B/C creative test for a short-form story. Provide exactly three distinct hooks and "
            "exactly three matching title options. Avoid deceptive claims, fabricated facts, spammy all-caps, and false "
            "urgency. Hooks should earn attention while accurately representing the content. Return JSON with arrays "
            "'hooks' and 'titles'; each item has label A, B, or C and a text field."
        )
        payload = {"topic": topic, "context": context.strip(), "audience": audience, "platform": platform, "language": language}
        output = self.generate_json(
            system,
            "Generate variants for:\n" + json.dumps(payload, ensure_ascii=False),
            expected_type=dict,
            temperature=0.7,
        )
        result: dict[str, list[dict[str, str]]] = {}
        for kind in ("hooks", "titles"):
            items = output.get(kind)
            if not isinstance(items, list) or len(items) != 3:
                raise AIResponseError(f"{kind} must contain exactly three variants")
            normalized: list[dict[str, str]] = []
            seen: set[str] = set()
            for index, item in enumerate(items):
                if isinstance(item, str):
                    label, text_value = chr(ord("A") + index), item
                elif isinstance(item, Mapping):
                    label = str(item.get("label", chr(ord("A") + index))).strip().upper()
                    text_value = item.get("text", "")
                else:
                    raise AIResponseError(f"{kind}[{index}] must be text or an object")
                if label not in {"A", "B", "C"} or label in seen:
                    raise AIResponseError(f"{kind} labels must be unique A/B/C values")
                text_value = self._string(text_value, field=f"{kind}[{index}].text")
                if len(text_value) > (220 if kind == "hooks" else 120):
                    text_value = text_value[: (217 if kind == "hooks" else 117)].rstrip() + "…"
                seen.add(label)
                normalized.append({"label": label, "text": text_value})
            if seen != {"A", "B", "C"}:
                raise AIResponseError(f"{kind} must include labels A, B, and C")
            result[kind] = sorted(normalized, key=lambda item: item["label"])
        return result

    @staticmethod
    def _probe_duration_seconds(path: Path) -> float | None:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            return None
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            duration = float(result.stdout.strip())
            return duration if duration > 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    @classmethod
    def _extract_qa_frames(cls, clip_path: str | Path, *, max_frames: int = 3) -> tuple[list[tuple[bytes, str]], str]:
        path = Path(clip_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"clip_path is not a file: {path}")
        image_mimes = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        if path.suffix.casefold() in image_mimes:
            return [(path.read_bytes(), image_mimes[path.suffix.casefold()])], "image"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise BrainError("ffmpeg is required to sample frames from a video for scene QA")
        duration = cls._probe_duration_seconds(path)
        if duration:
            times = [max(0.0, duration * ratio) for ratio in (0.12, 0.5, 0.88)][:max_frames]
        else:
            times = [0.0, 0.5, 1.0][:max_frames]
        frames: list[tuple[bytes, str]] = []
        for second in times:
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{second:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=768:768:force_original_aspect_ratio=decrease",
                "-q:v",
                "4",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
            try:
                result = subprocess.run(command, check=True, capture_output=True, timeout=45)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.stdout:
                # Frames remain in memory and are sent directly to the selected
                # vision model; QA does not create extra media files on the NVMe.
                frames.append((result.stdout, "image/jpeg"))
        if not frames:
            raise BrainError(f"ffmpeg could not extract a usable frame from {path.name}")
        return frames, "video"

    def _gemini_multimodal(
        self,
        system_instruction: str,
        user_prompt: str,
        frames: Sequence[tuple[bytes, str]],
        *,
        temperature: float,
    ) -> str:
        client, types = self._get_gemini_client()
        parts: list[Any] = [types.Part.from_text(text=user_prompt)]
        parts.extend(types.Part.from_bytes(data=data, mime_type=mime) for data, mime in frames)
        contents = [types.Content(role="user", parts=parts)]
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL_ID,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=max(0.0, min(float(temperature), 2.0)),
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:
            raise AIProviderUnavailable(f"Gemini multimodal request failed: {type(exc).__name__}") from exc
        return self._response_text(response)

    def _ollama_multimodal(
        self,
        system_instruction: str,
        user_prompt: str,
        frames: Sequence[tuple[bytes, str]],
        *,
        temperature: float,
    ) -> str:
        encoded_images = [base64.b64encode(data).decode("ascii") for data, _ in frames]
        return self._ollama_chat(
            [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt, "images": encoded_images},
            ],
            json_output=True,
            temperature=temperature,
            model=config.OLLAMA_VISION_MODEL,
        )

    def multimodal_scene_qa(
        self,
        scene: Mapping[str, Any],
        *,
        clip_path: str | Path | None = None,
        clip_metadata: Mapping[str, Any] | None = None,
        frame_descriptions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Compare a scene brief with sampled clip frames or supplied frame notes."""
        if not isinstance(scene, Mapping):
            raise ValueError("scene must be a mapping")
        if clip_metadata is not None and not isinstance(clip_metadata, Mapping):
            raise ValueError("clip_metadata must be a mapping")
        if clip_path is None and not frame_descriptions:
            raise ValueError("Provide clip_path or non-empty frame_descriptions for scene QA")
        frames: list[tuple[bytes, str]] = []
        media_kind = "described_frames"
        if clip_path is not None:
            frames, media_kind = self._extract_qa_frames(clip_path)
        scene_context = {
            "expected_scene": dict(scene),
            "clip_metadata": dict(clip_metadata or {}),
            "frame_descriptions": list(frame_descriptions or []),
            "media_kind": media_kind,
            "sampled_frame_count": len(frames),
        }
        system = (
            "You are a conservative per-scene quality reviewer for video production. Compare only the supplied scene brief "
            "and sampled visual evidence. Do not infer facts that are not visible, and distinguish certain observations "
            "from uncertainty. Return valid JSON with score (integer 0-100), scene_match (boolean), continuity_issues "
            "(array of strings), visual_issues (array of strings), technical_issues (array of strings), summary (string), "
            "and recommendation ('accept', 'review', or 'reject'). Use 'review' when evidence is insufficient."
        )
        user_prompt = (
            "Review this scene against the sampled frames. Note that a few still frames do not prove motion or audio quality.\n"
            + json.dumps(scene_context, ensure_ascii=False)
        )
        failures: list[str] = []
        if frames:
            for backend in self._provider_plan():
                try:
                    if backend == "gemini":
                        raw = self._gemini_multimodal(system, user_prompt, frames, temperature=0.2)
                    else:
                        raw = self._ollama_multimodal(system, user_prompt, frames, temperature=0.2)
                    output = self._parse_json_text(raw)
                    break
                except (AIProviderUnavailable, AIResponseError) as exc:
                    failures.append(f"{backend}: {exc}")
            else:
                raise AIProviderUnavailable("Multimodal scene QA failed. " + " | ".join(failures))
        else:
            output = self.generate_json(system, user_prompt, expected_type=dict, temperature=0.2)
        if not isinstance(output, dict):
            raise AIResponseError("Scene QA response must be a JSON object")
        try:
            score = int(float(output.get("score", 0)))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))

        def string_list(key: str) -> list[str]:
            value = output.get(key, [])
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return []
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]

        match = output.get("scene_match", False)
        if not isinstance(match, bool):
            match = str(match).casefold() in {"true", "yes", "1"}
        recommendation = str(output.get("recommendation", "review")).casefold()
        if recommendation not in {"accept", "review", "reject"}:
            recommendation = "review"
        return {
            "score": score,
            "scene_match": match,
            "continuity_issues": string_list("continuity_issues"),
            "visual_issues": string_list("visual_issues"),
            "technical_issues": string_list("technical_issues"),
            "summary": self._string(output.get("summary", "Evidence reviewed."), field="summary", required=False),
            "recommendation": recommendation,
            "media_kind": media_kind,
            "sampled_frame_count": len(frames),
        }

    def _parse_json_text(self, text: str) -> Any:
        cleaned = self._strip_json_wrappers(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            balanced = self._extract_balanced_json(cleaned)
            if balanced is None:
                raise AIResponseError("AI response did not contain valid JSON")
            return json.loads(balanced)


def generate_reference_prompts(**kwargs: Any) -> dict[str, str]:
    """Convenience function for callers that do not need to retain a Brain."""
    return Brain().generate_reference_prompts(**kwargs)


def generate_video_scene_prompt(scene: str | Mapping[str, Any], **kwargs: Any) -> str:
    """Convenience function that returns one validated <=880-character prompt."""
    return Brain().generate_video_scene_prompt(scene, **kwargs)


def _main() -> int:
    parser = argparse.ArgumentParser(description="DaemonCut AI brain diagnostics")
    parser.add_argument("--check", action="store_true", help="print configured provider availability without making a request")
    parser.add_argument("--limit-check", help="validate a video scene prompt from a string")
    args = parser.parse_args()
    if args.limit_check is not None:
        prompt = Brain().enforce_video_scene_prompt_limit(args.limit_check, max_retries=0)
        print(json.dumps({"prompt": prompt, "characters": len(prompt), "maximum": config.MAX_YT_CREATE_CHARS}, ensure_ascii=False))
        return 0
    if args.check:
        print(
            json.dumps(
                {
                    "provider_mode": config.AI_PROVIDER,
                    "gemini_key_configured": bool(config.GEMINI_API_KEY),
                    "ollama_enabled": config.OLLAMA_ENABLED,
                    "gemini_model": config.GEMINI_MODEL_ID,
                    "ollama_model": config.OLLAMA_MODEL,
                    "max_video_scene_chars": config.MAX_YT_CREATE_CHARS,
                    "reference_prompts_capped": False,
                },
                indent=2,
            )
        )
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
