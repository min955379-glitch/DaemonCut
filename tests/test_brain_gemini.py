from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.brain_gemini import Brain
from backend.config import MAX_YT_CREATE_CHARS


class BrainPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brain = Brain(provider="ollama")

    def test_scene_prompt_fallback_always_obeys_880_character_law(self) -> None:
        source = "A consistent character crosses the warm, detailed neighborhood courtyard; " * 60
        result = self.brain.enforce_video_scene_prompt_limit(source, max_retries=0)
        self.assertLessEqual(len(result), MAX_YT_CREATE_CHARS)
        self.assertTrue(result.endswith("…"))

    def test_short_scene_prompt_is_preserved(self) -> None:
        source = "A slow push-in on Breezy at the maker courtyard workbench."
        self.assertEqual(self.brain.enforce_video_scene_prompt_limit(source), source)

    def test_reference_output_is_not_subject_to_scene_limit(self) -> None:
        source = "detailed anatomy, fabric weave, rim lighting, and lens notes " * 30
        self.assertGreater(len(source), MAX_YT_CREATE_CHARS)
        with patch.object(
            self.brain,
            "generate_json",
            return_value={key: source for key in ("E1", "E2", "E3", "E3B")},
        ):
            prompts = self.brain.generate_reference_prompts(
                series_bible={"series": {}, "templates": {"custom_universal_niche": {}}}
            )
        self.assertEqual(prompts["E1"], source.strip())
        self.assertGreater(len(prompts["E1"]), MAX_YT_CREATE_CHARS)

    def test_script_and_reference_prompts_resolve_named_template_series(self) -> None:
        generated = {
            "title": "An original block-built mystery",
            "hook": "The block path moved.",
            "script": "The avatar investigates.",
            "description": "An original story.",
            "tags": ["story"],
            "scenes": [{"visual_description": "An original blocky avatar in a simple courtyard.", "video_prompt": "Original block-built avatar studies the path."}],
        }
        with patch.object(self.brain, "generate_json", return_value=generated) as script_call:
            self.brain.generate_script(niche="block-built stories", topic="A moving path", series_name="3d_roblox_story")
        self.assertIn("3D Roblox Story", script_call.call_args.args[1])
        self.assertIn("Do not reproduce official Roblox branding", script_call.call_args.args[1])

        references = {key: f"Original block-built reference {key}" for key in ("E1", "E2", "E3", "E3B")}
        with patch.object(self.brain, "generate_json", return_value=references) as reference_call:
            self.brain.generate_reference_prompts(series_name="3d_roblox_story")
        self.assertIn("3D Roblox Story", reference_call.call_args.args[1])
        self.assertIn("Use original characters", reference_call.call_args.args[1])

    def test_condensing_retry_uses_model_response(self) -> None:
        source = "A long prompt. " * 100
        with patch.object(self.brain, "generate_text", return_value="A concise scene prompt.") as generate:
            result = self.brain.enforce_video_scene_prompt_limit(source)
        self.assertEqual(result, "A concise scene prompt.")
        generate.assert_called_once()

    def test_script_generation_revalidates_each_scene_prompt(self) -> None:
        long_prompt = "Breezy and Mal move through the courtyard. " * 40
        generated = {
            "title": "A small discovery",
            "hook": "Something is missing.",
            "script": "The friends search the workshop.",
            "description": "A short story.",
            "tags": ["story"],
            "scenes": [
                {
                    "scene_number": 1,
                    "duration_seconds": 30,
                    "narration": "They search together.",
                    "visual_description": "The friends search the courtyard.",
                    "video_prompt": long_prompt,
                }
            ],
        }
        with patch.object(self.brain, "generate_json", return_value=generated), patch.object(
            self.brain, "generate_text", return_value="A concise courtyard scene."
        ):
            result = self.brain.generate_script(
                niche="family stories", topic="A missing key", duration_seconds=30, scene_count=1
            )
        self.assertEqual(result["scenes"][0]["video_prompt"], "A concise courtyard scene.")
        self.assertLessEqual(result["scenes"][0]["prompt_char_count"], MAX_YT_CREATE_CHARS)

    def test_json_extractor_ignores_braces_inside_string(self) -> None:
        text = 'prefix ```json\n{"text":"a } inside a string", "count": 2}\n``` suffix'
        cleaned = self.brain._strip_json_wrappers(text)
        parsed = self.brain._parse_json_text(cleaned)
        self.assertEqual(parsed["count"], 2)
        self.assertEqual(parsed["text"], "a } inside a string")

    def test_scene_brief_requires_visual_content(self) -> None:
        with self.assertRaises(ValueError):
            self.brain._scene_brief({"narration": "An audio-only line"})
        self.assertIn("visual_description", self.brain._scene_brief({"visual_description": "A cat waves."}))


if __name__ == "__main__":
    unittest.main()
