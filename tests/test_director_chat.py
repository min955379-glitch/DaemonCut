from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend import config
from backend.brain_gemini import AIProviderUnavailable
from backend.director_chat import (
    DictationBuffer,
    DirectorChat,
    DirectorChatError,
    ToolRoutingError,
)


class _Brain:
    def __init__(self) -> None:
        self.script_kwargs: dict[str, object] | None = None
        self.plan: dict[str, object] | None = None

    def generate_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        if self.plan is not None:
            return self.plan
        raise AIProviderUnavailable("offline")

    def generate_script(self, **kwargs: object) -> dict[str, object]:
        self.script_kwargs = kwargs
        return {
            "title": "Fresh Story",
            "hook": "A new idea",
            "script": "Two friends solve a new problem.",
            "scenes": [{"video_prompt": "An entirely original scene.", "narration": ""}],
        }

    @staticmethod
    def enforce_video_scene_prompt_limit(prompt: str, *, max_retries: int = 0) -> str:
        del max_retries
        return prompt[: config.MAX_YT_CREATE_CHARS]

    @staticmethod
    def generate_reference_prompts(**_kwargs: object) -> dict[str, str]:
        return {key: "reference " + ("detail " * 200) for key in ("E1", "E2", "E3", "E3B")}


class _Subtitle:
    def __init__(self) -> None:
        self.transcribe_calls: list[tuple[Path, dict[str, object]]] = []

    def transcribe_audio(self, path: Path, **kwargs: object) -> dict[str, object]:
        self.transcribe_calls.append((path, kwargs))
        return {"text": "local words", "backend": "whisper.cpp-vulkan", "word_timings": []}


class _VideoEditor:
    def __init__(self) -> None:
        self.clips: object | None = None
        self.kwargs: dict[str, object] = {}

    def render(self, clips: object, **kwargs: object) -> str:
        self.clips, self.kwargs = clips, kwargs
        return "rendered"


class DictationBufferTests(unittest.TestCase):
    def test_pause_resume_preserves_text_and_rejects_chunks_while_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "dictation.json"
            buffer = DictationBuffer(state_path=state)
            first = buffer.append_speech_chunk("session-1", "The first half")
            self.assertEqual(first.text, "The first half")
            paused = buffer.pause_dictation("session-1")
            self.assertTrue(paused.paused)
            self.assertEqual(paused.text, "The first half")
            with self.assertRaises(DirectorChatError):
                buffer.append_speech_chunk("session-1", "should not replace it")
            resumed = buffer.resume_dictation("session-1")
            self.assertFalse(resumed.paused)
            second = buffer.append_speech_chunk("session-1", "and the second half")
            self.assertEqual(second.text, "The first half and the second half")

    def test_drafts_persist_across_buffer_instances_and_finalize_clears_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "drafts.json"
            buffer = DictationBuffer(state_path=state)
            buffer.append_speech_chunk("session-A", "Keep this draft")
            buffer.pause_dictation("session-A")
            serialized = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(serialized["drafts"][0]["text"], "Keep this draft")
            reopened = DictationBuffer(state_path=state)
            draft = reopened.get("session-A")
            self.assertEqual(draft.text, "Keep this draft")
            self.assertTrue(draft.paused)
            self.assertEqual(reopened.finalize_draft("session-A"), "Keep this draft")
            self.assertEqual(DictationBuffer(state_path=state).get("session-A").text, "")

    def test_sessions_are_isolated_clear_and_finalize_are_safe(self) -> None:
        buffer = DictationBuffer(state_path=None)
        buffer.append_speech_chunk("one", "first")
        buffer.append_speech_chunk("two", "second")
        buffer.clear_draft("one")
        self.assertEqual(buffer.get("one").text, "")
        self.assertEqual(buffer.get("two").text, "second")
        self.assertEqual(buffer.finalize_draft("two"), "second")
        with self.assertRaises(ValueError):
            buffer.append_speech_chunk("../unsafe", "text")

    def test_maximum_draft_size_is_enforced(self) -> None:
        buffer = DictationBuffer(max_chars=10, state_path=None)
        buffer.append_speech_chunk("id", "12345")
        with self.assertRaisesRegex(ValueError, "10-character"):
            buffer.append_speech_chunk("id", "678901")


class DirectorChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brain = _Brain()
        self.subtitle = _Subtitle()
        self.video = _VideoEditor()
        self.chat = DirectorChat(
            brain=self.brain,
            dictation=DictationBuffer(state_path=None),
            subtitle_engine=self.subtitle,  # type: ignore[arg-type]
            video_editor=self.video,  # type: ignore[arg-type]
        )

    def test_local_transcription_helper_routes_to_subtitle_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "chunk.wav"
            audio.write_bytes(b"audio")
            result = self.chat.transcribe_audio_chunk(audio, language="en")
        self.assertEqual(result["text"], "local words")
        self.assertEqual(self.subtitle.transcribe_calls[0][1]["language"], "en")

    def test_deterministic_chat_routes_script_creation_with_context(self) -> None:
        response = self.chat.chat(
            "Please create a script",
            context={"niche": "cartoon comedy", "topic": "a clock that runs backward"},
        )
        self.assertTrue(response.success)
        self.assertEqual(response.tool, "create_script_and_prompts")
        self.assertEqual(self.brain.script_kwargs["topic"], "a clock that runs backward")

    def test_brain_plan_is_restricted_to_allowlisted_tools(self) -> None:
        self.brain.plan = {"tool": "delete_everything", "arguments": {}, "reply": "no"}
        plan = self.chat._plan_with_brain("please do that")
        self.assertIsNone(plan.tool)
        response = self.chat.chat("Make a script", context={"niche": "craft", "topic": "paper bridges"})
        self.assertTrue(response.success)
        self.assertEqual(response.tool, "create_script_and_prompts")
        with self.assertRaises(ToolRoutingError):
            self.chat.invoke_tool("arbitrary_shell", {})

    def test_reference_prompts_remain_unlimited_but_video_prompts_are_checked(self) -> None:
        result = self.chat.invoke_tool(
            "create_script_and_prompts",
            {"niche": "cartoon", "topic": "new story", "include_reference_prompts": True},
        )
        self.assertGreater(len(result["reference_prompts"]["E1"]), config.MAX_YT_CREATE_CHARS)
        self.assertLessEqual(len(result["scenes"][0]["video_prompt"]), config.MAX_YT_CREATE_CHARS)

    def test_scene_edit_tool_passes_timing_speed_framing_and_lut(self) -> None:
        result = self.chat.invoke_tool(
            "edit_video_scene",
            {
                "video_path": "scene.mp4",
                "start_seconds": 2,
                "end_seconds": 8,
                "speed": 1.25,
                "framing_mode": "full_screen",
                "center_x": 0.6,
                "lut_name": "brass.cube",
            },
        )
        self.assertEqual(result, "rendered")
        self.assertEqual(self.video.clips[0].start_seconds, 2)
        self.assertEqual(self.video.clips[0].speed, 1.25)
        self.assertEqual(self.video.clips[0].framing_mode, "full_screen")
        self.assertEqual(self.video.kwargs["lut_name"], "brass.cube")

    def test_video_scene_prompt_over_limit_is_rejected(self) -> None:
        self.brain.generate_script = lambda **_kwargs: {"scenes": [{"video_prompt": "x" * 881}]}  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "character limit"):
            self.chat.invoke_tool("create_script_and_prompts", {"niche": "custom", "topic": "story"})

    def test_brain_response_uses_allowed_route_and_returns_structured_result(self) -> None:
        self.brain.plan = {
            "tool": "create_script_and_prompts",
            "arguments": {"niche": "animation", "topic": "a helpful robot"},
            "reply": "I will draft it.",
        }
        response = self.chat.chat("Make the story", context={})
        self.assertTrue(response.success)
        self.assertEqual(response.plan_source, "brain")
        self.assertEqual(response.result["title"], "Fresh Story")


if __name__ == "__main__":
    unittest.main()
