from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
ALLOWED_PALETTE = {
    "#121316",
    "#1A1D24",
    "#2B303B",
    "#F4EFE6",
    "#D6AD55",
    "#2B5B84",
    "#102B4E",
    "#C94A4A",
}


class FrontendContractTests(unittest.TestCase):
    def source_files(self) -> list[Path]:
        return [
            *[path for path in (FRONTEND / "src").rglob("*") if path.suffix in {".ts", ".tsx", ".css"}],
            FRONTEND / "tailwind.config.ts",
            FRONTEND / "next.config.mjs",
        ]

    def test_frontend_uses_only_the_locked_palette_and_no_green_status_tokens(self) -> None:
        files = self.source_files()
        self.assertTrue(files)
        contents = "\n".join(path.read_text(encoding="utf-8") for path in files)
        colors = {item.upper() for item in re.findall(r"#[0-9A-Fa-f]{3,8}\b", contents)}
        self.assertTrue(colors <= ALLOWED_PALETTE, f"Unapproved frontend colors: {sorted(colors - ALLOWED_PALETTE)}")
        forbidden = re.search(r"#00ff00|\b(?:green|emerald|lime|teal)-[a-z0-9_-]+", contents, re.IGNORECASE)
        self.assertIsNone(forbidden, f"Forbidden color/status token: {forbidden.group(0) if forbidden else ''}")

    def test_backend_color_literals_remain_inside_the_locked_palette(self) -> None:
        files = [path for path in (ROOT / "backend").rglob("*") if path.suffix in {".py", ".json"} and "__pycache__" not in path.parts]
        contents = "\n".join(path.read_text(encoding="utf-8") for path in files)
        colors = {item.upper() for item in re.findall(r"#[0-9A-Fa-f]{3,8}\b", contents)}
        self.assertTrue(colors <= ALLOWED_PALETTE, f"Unapproved backend colors: {sorted(colors - ALLOWED_PALETTE)}")
        self.assertNotRegex(contents, r"#00ff00")

    def test_frontend_includes_ibm_plex_and_prompt_limit_controls(self) -> None:
        contents = "\n".join(path.read_text(encoding="utf-8") for path in self.source_files())
        self.assertIn("IBM Plex Sans", contents)
        self.assertIn("IBM Plex Mono", contents)
        self.assertIn("E3B", contents)
        self.assertIn("MAX_YT_CREATE_CHARS = 880", contents)
        self.assertIn("limit={MAX_YT_CREATE_CHARS}", contents)
        self.assertIn("slice(0, limit ?? 100_000)", contents)
        self.assertIn("counter", contents)


if __name__ == "__main__":
    unittest.main()
