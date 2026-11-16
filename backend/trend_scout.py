"""Tier-1 trends, ethical A/B/C variants, and anti-repetition checks.

Trend data is read from the public Google Trends RSS endpoint for US, GB, CA,
and AU. The scout records source links and never promises that a topic or
creative variant will go viral. No browser automation or private platform API
is used for scouting.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import email.utils
import html
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from backend import config
from backend.brain_gemini import AIProviderUnavailable, AIResponseError, Brain


class TrendScoutError(RuntimeError):
    """Base class for trend scout failures."""


class TrendSourceUnavailable(TrendScoutError):
    """All requested RSS sources were unavailable or returned invalid XML."""


@dataclass(frozen=True)
class TrendItem:
    topic: str
    country: str
    approx_traffic: str = ""
    estimated_traffic: int = 0
    published_at: str | None = None
    context: str = ""
    source_url: str = ""
    cross_country_count: int = 1
    trend_score: float = 0.0
    repetition_similarity: float = 0.0
    repeated_recently: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COUNTRY_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
}

# Token noise is ignored for anti-repetition checks, while names and numbers
# remain available to distinguish closely related story ideas.
_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is",
        "it", "of", "on", "or", "the", "this", "to", "was", "what", "when", "where", "why", "with",
    }
)


def _clean_html(value: str) -> str:
    no_tags = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _descendant_text(element: ET.Element, local_name: str) -> str:
    for child in element.iter():
        if _local_name(child.tag) == local_name.casefold() and child.text:
            return child.text.strip()
    return ""


def _traffic_value(raw: str) -> int:
    if not raw:
        return 0
    match = re.search(r"([\d,.]+)\s*([kmb])?", raw.casefold().replace(" ", ""))
    if not match:
        return 0
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(match.group(2), 1)
    return max(0, int(number * multiplier))


def _published_datetime(raw: str) -> str | None:
    if not raw:
        return None
    try:
        value = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalize_words(value: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    words = re.findall(r"[a-z0-9]+", decomposed)
    return [word for word in words if word not in _STOP_WORDS]


def _content_fingerprint(item: str | Mapping[str, Any]) -> tuple[str, str, str]:
    if isinstance(item, str):
        text = item.strip()
        return text, text, ""
    if not isinstance(item, Mapping):
        raise ValueError("episode/candidate must be text or a mapping")
    topic = str(item.get("topic", "")).strip()
    title = str(item.get("title", "")).strip()
    hook = str(item.get("hook", "")).strip()
    if not topic and not title and not hook:
        script = str(item.get("script", "")).strip()
        return script, script, ""
    primary = " ".join(value for value in (topic, title) if value)
    return primary, title or topic, hook


def similarity_score(left: str | Mapping[str, Any], right: str | Mapping[str, Any]) -> float:
    """Return a bounded repetition similarity score between 0.0 and 1.0."""
    left_primary, left_title, left_hook = _content_fingerprint(left)
    right_primary, right_title, right_hook = _content_fingerprint(right)
    if not any((left_primary, left_title, left_hook, right_primary, right_title, right_hook)):
        return 0.0

    def text_score(first: str, second: str) -> float:
        first_words = _normalize_words(first)
        second_words = _normalize_words(second)
        if not first_words or not second_words:
            return 0.0
        first_joined = " ".join(first_words)
        second_joined = " ".join(second_words)
        sequence = SequenceMatcher(None, first_joined, second_joined, autojunk=False).ratio()
        first_set, second_set = set(first_words), set(second_words)
        jaccard = len(first_set & second_set) / max(1, len(first_set | second_set))
        first_bigrams = set(zip(first_words, first_words[1:]))
        second_bigrams = set(zip(second_words, second_words[1:]))
        bigram = len(first_bigrams & second_bigrams) / max(1, len(first_bigrams | second_bigrams))
        return max(sequence, 0.55 * jaccard + 0.25 * bigram)

    # Exact topic repetition matters most; a repeated hook/title also matters,
    # while unrelated phrasing elsewhere in a longer script is not over-weighted.
    return max(
        text_score(left_primary, right_primary),
        0.92 * text_score(left_title, right_title),
        0.85 * text_score(left_hook, right_hook),
    )


class TrendScout:
    """Fetch, merge, score, and package trends for US/UK/CA/AU."""

    def __init__(
        self,
        *,
        brain: Brain | None = None,
        timeout_seconds: int | None = None,
        cache_ttl_seconds: int = 900,
        countries: Sequence[str] = config.TREND_COUNTRIES,
    ) -> None:
        self.brain = brain or Brain()
        self.timeout_seconds = timeout_seconds or config.TREND_FETCH_TIMEOUT_SECONDS
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not 0 <= cache_ttl_seconds <= 86_400:
            raise ValueError("cache_ttl_seconds must be between 0 and 86400")
        self.cache_ttl_seconds = cache_ttl_seconds
        normalized = tuple(dict.fromkeys(str(country).strip().upper() for country in countries))
        unsupported = set(normalized) - set(COUNTRY_NAMES)
        if unsupported:
            raise ValueError(f"Only US/GB/CA/AU are supported in the Tier-1 scout: {sorted(unsupported)}")
        self.countries = normalized
        self.last_errors: dict[str, str] = {}
        self._cache: dict[str, tuple[float, list[TrendItem]]] = {}

    def fetch_country(self, country: str, *, force_refresh: bool = False) -> list[TrendItem]:
        country = country.strip().upper()
        if country not in COUNTRY_NAMES:
            raise ValueError(f"Unsupported country {country!r}; use US, GB, CA, or AU")
        now_monotonic = time.monotonic()
        cached = self._cache.get(country)
        if not force_refresh and cached and now_monotonic - cached[0] < self.cache_ttl_seconds:
            return list(cached[1])
        url = config.TREND_RSS_URL.format(country=urllib.parse.quote(country))
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "DaemonCut-TrendScout/0.1 (+local creator research)",
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise TrendScoutError(f"Google Trends RSS returned HTTP {exc.code}: {detail[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TrendScoutError(f"Could not fetch trends for {country}: {type(exc).__name__}") from exc
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise TrendScoutError(f"Trends feed for {country} was not valid XML") from exc
        items: list[TrendItem] = []
        for node in root.iter():
            if _local_name(node.tag) != "item":
                continue
            title_node = next((child for child in node if _local_name(child.tag) == "title"), None)
            title = (title_node.text or "").strip() if title_node is not None else ""
            if not title:
                continue
            link_node = next((child for child in node if _local_name(child.tag) == "link"), None)
            link = (link_node.text or "").strip() if link_node is not None else ""
            description_node = next((child for child in node if _local_name(child.tag) == "description"), None)
            description = _clean_html(description_node.text or "") if description_node is not None else ""
            traffic = _descendant_text(node, "approx_traffic")
            pub_date_node = next((child for child in node if _local_name(child.tag) in {"pubdate", "published"}), None)
            published = _published_datetime((pub_date_node.text or "").strip()) if pub_date_node is not None else None
            items.append(
                TrendItem(
                    topic=title,
                    country=country,
                    approx_traffic=traffic,
                    estimated_traffic=_traffic_value(traffic),
                    published_at=published,
                    context=description,
                    source_url=link or url,
                )
            )
        if not items:
            raise TrendScoutError(f"Trends feed for {country} contained no usable items")
        self._cache[country] = (now_monotonic, items)
        return list(items)

    def fetch_tier1(self, *, force_refresh: bool = False) -> list[TrendItem]:
        """Fetch country feeds concurrently and keep the partial results on error."""
        if not self.countries:
            raise ValueError("At least one Tier-1 country must be configured")
        collected: list[TrendItem] = []
        errors: dict[str, str] = {}
        max_workers = min(4, len(self.countries))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.fetch_country, country, force_refresh=force_refresh): country
                for country in self.countries
            }
            for future in concurrent.futures.as_completed(futures):
                country = futures[future]
                try:
                    collected.extend(future.result())
                except TrendScoutError as exc:
                    errors[country] = str(exc)
        self.last_errors = errors
        if not collected:
            message = "; ".join(f"{country}: {error}" for country, error in sorted(errors.items()))
            raise TrendSourceUnavailable(message or "No trend sources returned data")
        return collected

    @staticmethod
    def _score(traffic: int, country_count: int, published_at: str | None) -> float:
        # Log scaling avoids allowing one large traffic estimate to dominate.
        traffic_component = min(1.0, math.log10(max(traffic, 1) + 1) / 6.0)
        coverage_component = min(1.0, max(0, country_count - 1) / 3.0)
        recency_component = 0.5
        if published_at:
            try:
                published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                age_hours = max(0.0, (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600)
                recency_component = max(0.0, 1.0 - age_hours / (7 * 24))
            except ValueError:
                pass
        return round(100 * (0.60 * traffic_component + 0.25 * coverage_component + 0.15 * recency_component), 1)

    @staticmethod
    def _fallback_variants(topic: str) -> dict[str, list[dict[str, str]]]:
        safe_topic = topic.strip().rstrip(".?!")
        return {
            "hooks": [
                {"label": "A", "text": f"A closer look at {safe_topic}."},
                {"label": "B", "text": f"What is the story behind {safe_topic}?"},
                {"label": "C", "text": f"One question to ask about {safe_topic}."},
            ],
            "titles": [
                {"label": "A", "text": f"The story behind {safe_topic}"},
                {"label": "B", "text": f"Understanding {safe_topic}"},
                {"label": "C", "text": f"A closer look: {safe_topic}"},
            ],
        }

    def generate_variants(
        self,
        topic: str,
        *,
        context: str = "",
        audience: str = "general audience",
        platform: str = "YouTube Shorts",
        language: str = "English",
        use_ai: bool = True,
    ) -> dict[str, list[dict[str, str]]]:
        """Return exactly three labeled hook and title alternatives (A/B/C)."""
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("topic cannot be empty")
        if use_ai:
            try:
                return self.brain.generate_hook_title_variants(
                    topic=topic.strip(),
                    context=context,
                    audience=audience,
                    platform=platform,
                    language=language,
                )
            except (AIProviderUnavailable, AIResponseError, ValueError):
                pass
        return self._fallback_variants(topic)

    def scout(
        self,
        *,
        limit: int = 20,
        recent_topics: Sequence[str | Mapping[str, Any]] = (),
        generate_variants: bool = True,
        variants_limit: int = 8,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Merge feeds, score cross-country interest, annotate repeats, and add tests."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if isinstance(recent_topics, (str, bytes)) or not isinstance(recent_topics, Sequence):
            raise ValueError("recent_topics must be a sequence of strings or episode mappings")
        if not 0 <= variants_limit <= 20:
            raise ValueError("variants_limit must be between 0 and 20")
        raw = self.fetch_tier1(force_refresh=force_refresh)
        merged: dict[str, dict[str, Any]] = {}
        for item in raw:
            normalized = " ".join(_normalize_words(item.topic)) or item.topic.casefold().strip()
            record = merged.setdefault(
                normalized,
                {
                    "topic": item.topic,
                    "countries": set(),
                    "traffic": 0,
                    "approx_traffic": item.approx_traffic,
                    "published_at": item.published_at,
                    "context": item.context,
                    "sources": [],
                },
            )
            record["countries"].add(item.country)
            record["traffic"] = max(record["traffic"], item.estimated_traffic)
            if item.estimated_traffic and (not record["approx_traffic"] or item.estimated_traffic >= _traffic_value(record["approx_traffic"])):
                record["approx_traffic"] = item.approx_traffic
            if item.published_at and (not record["published_at"] or item.published_at > record["published_at"]):
                record["published_at"] = item.published_at
            if item.context and not record["context"]:
                record["context"] = item.context
            if item.source_url and item.source_url not in record["sources"]:
                record["sources"].append(item.source_url)

        candidates: list[dict[str, Any]] = []
        for record in merged.values():
            countries = sorted(record["countries"])
            score = self._score(record["traffic"], len(countries), record["published_at"])
            best_similarity = max(
                (similarity_score(record["topic"], recent) for recent in recent_topics),
                default=0.0,
            )
            candidates.append(
                {
                    "topic": record["topic"],
                    "countries": countries,
                    "country_names": [COUNTRY_NAMES[country] for country in countries],
                    "cross_country_count": len(countries),
                    "approx_traffic": record["approx_traffic"],
                    "estimated_traffic": record["traffic"],
                    "published_at": record["published_at"],
                    "context": record["context"],
                    "sources": record["sources"],
                    "trend_score": score,
                    "repetition_similarity": round(best_similarity, 3),
                    "repeated_recently": best_similarity >= 0.72,
                    "variants": None,
                }
            )
        candidates.sort(key=lambda item: (item["trend_score"], item["cross_country_count"], item["estimated_traffic"]), reverse=True)
        selected = candidates[:limit]
        if generate_variants and variants_limit:
            for item in selected[: min(variants_limit, len(selected))]:
                item["variants"] = self.generate_variants(item["topic"], context=item["context"])
        return selected

    @staticmethod
    def scan_anti_repetition(
        candidate: str | Mapping[str, Any],
        recent_episodes: Sequence[str | Mapping[str, Any]],
        *,
        threshold: float = 0.72,
    ) -> dict[str, Any]:
        """Compare one candidate against a history of episodes/topics."""
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if isinstance(recent_episodes, (str, bytes)) or not isinstance(recent_episodes, Sequence):
            raise ValueError("recent_episodes must be a sequence")
        matches: list[dict[str, Any]] = []
        for index, recent in enumerate(recent_episodes):
            similarity = similarity_score(candidate, recent)
            if similarity >= threshold:
                recent_id = recent.get("episode_id", recent.get("id", index)) if isinstance(recent, Mapping) else index
                matches.append({"history_index": index, "episode_id": str(recent_id), "similarity": round(similarity, 3)})
        matches.sort(key=lambda item: item["similarity"], reverse=True)
        return {
            "repeated": bool(matches),
            "threshold": threshold,
            "max_similarity": matches[0]["similarity"] if matches else 0.0,
            "matches": matches,
        }

    @staticmethod
    def scan_seven_episode_batch(
        episodes: Sequence[str | Mapping[str, Any]],
        *,
        recent_episodes: Sequence[str | Mapping[str, Any]] = (),
        threshold: float = 0.72,
    ) -> dict[str, Any]:
        """Scan all 21 within-batch pairs and the supplied history for repeats."""
        if isinstance(episodes, (str, bytes)) or not isinstance(episodes, Sequence):
            raise ValueError("episodes must be a sequence of exactly seven entries")
        if len(episodes) != 7:
            raise ValueError(f"Anti-repetition batch scan requires exactly seven episodes; received {len(episodes)}")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        findings: list[dict[str, Any]] = []
        for left_index in range(len(episodes)):
            for right_index in range(left_index + 1, len(episodes)):
                score = similarity_score(episodes[left_index], episodes[right_index])
                if score >= threshold:
                    findings.append(
                        {
                            "kind": "within_batch",
                            "episode_a": left_index + 1,
                            "episode_b": right_index + 1,
                            "similarity": round(score, 3),
                        }
                    )
        for episode_index, candidate in enumerate(episodes, start=1):
            history_scan = TrendScout.scan_anti_repetition(
                candidate,
                recent_episodes,
                threshold=threshold,
            )
            for match in history_scan["matches"]:
                findings.append(
                    {
                        "kind": "recent_history",
                        "episode": episode_index,
                        "history_index": match["history_index"],
                        "episode_id": match["episode_id"],
                        "similarity": match["similarity"],
                    }
                )
        findings.sort(key=lambda item: item["similarity"], reverse=True)
        return {
            "episode_count": 7,
            "pair_count_checked": 21,
            "threshold": threshold,
            "passed": not findings,
            "finding_count": len(findings),
            "findings": findings,
        }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Scout US/UK/CA/AU trends and generate ethical variants")
    parser.add_argument("--limit", type=int, default=12, help="number of merged topics to return (1-100)")
    parser.add_argument("--country", choices=sorted(COUNTRY_NAMES), action="append", help="fetch one or more countries; default is all four")
    parser.add_argument("--no-variants", action="store_true", help="skip hook/title generation")
    parser.add_argument("--variants-limit", type=int, default=8, help="maximum top items with A/B/C alternatives")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    parser.add_argument("--force-refresh", action="store_true", help="bypass the in-memory feed cache")
    args = parser.parse_args()
    try:
        scout = TrendScout(countries=args.country or config.TREND_COUNTRIES)
        results = scout.scout(
            limit=args.limit,
            generate_variants=not args.no_variants,
            variants_limit=args.variants_limit,
            force_refresh=args.force_refresh,
        )
    except (TrendScoutError, ValueError) as exc:
        print(f"Trend scout failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"results": results, "source_errors": scout.last_errors}, ensure_ascii=False, indent=2))
    else:
        for number, item in enumerate(results, start=1):
            regions = ", ".join(item["countries"])
            print(f"{number:02d}. [{item['trend_score']:>4}] {item['topic']} ({regions}; {item['approx_traffic'] or 'traffic n/a'})")
            if item["sources"]:
                print(f"    Source: {item['sources'][0]}")
            if item["repeated_recently"]:
                print(f"    Repetition warning: similarity {item['repetition_similarity']:.3f}")
            if item["variants"]:
                for kind in ("hooks", "titles"):
                    formatted = " | ".join(f"{variant['label']}: {variant['text']}" for variant in item["variants"][kind])
                    print(f"    {kind.title()}: {formatted}")
        if scout.last_errors:
            print("Partial source errors: " + "; ".join(f"{country}: {message}" for country, message in scout.last_errors.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
