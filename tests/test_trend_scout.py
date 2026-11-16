from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from backend.trend_scout import TrendScout, similarity_score


class TrendScoutTests(unittest.TestCase):
    def test_fallback_variants_have_three_distinct_labels(self) -> None:
        variants = TrendScout().generate_variants("A science museum reopens", use_ai=False)
        self.assertEqual([item["label"] for item in variants["hooks"]], ["A", "B", "C"])
        self.assertEqual([item["label"] for item in variants["titles"]], ["A", "B", "C"])
        self.assertEqual(len({item["text"] for item in variants["hooks"]}), 3)

    def test_seven_episode_repetition_scan(self) -> None:
        repeated = [{"topic": "A tiny robot finds a lost key", "title": "The lost key"} for _ in range(7)]
        result = TrendScout.scan_seven_episode_batch(repeated)
        self.assertFalse(result["passed"])
        self.assertEqual(result["pair_count_checked"], 21)
        self.assertEqual(result["finding_count"], 21)

    def test_seven_episode_batch_requires_exactly_seven(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly seven"):
            TrendScout.scan_seven_episode_batch(["idea"] * 6)

    def test_similarity_is_bounded(self) -> None:
        value = similarity_score("A quiet robot finds a key", "A quiet robot finds a key")
        self.assertEqual(value, 1.0)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)

    def test_google_trends_rss_parsing_keeps_source_and_traffic(self) -> None:
        rss = b'''<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item>
        <title>Example science story</title>
        <link>https://example.test/story</link>
        <pubDate>Mon, 16 Nov 2026 00:00:00 GMT</pubDate>
        <description><![CDATA[<a>Context for this topic</a>]]></description>
        <ht:approx_traffic>200K+</ht:approx_traffic>
        </item></channel></rss>'''
        with patch("backend.trend_scout.urllib.request.urlopen", return_value=io.BytesIO(rss)):
            items = TrendScout(countries=["US"]).fetch_country("US", force_refresh=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].topic, "Example science story")
        self.assertEqual(items[0].estimated_traffic, 200_000)
        self.assertEqual(items[0].source_url, "https://example.test/story")
        self.assertEqual(items[0].context, "Context for this topic")


if __name__ == "__main__":
    unittest.main()
