"""
Scorer for Newsletter Curator.

Uses Claude API to evaluate newsletter items against Kurt's interest
profile, producing a score, verdict, item type, and reasoning.
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import anthropic

from .prompts import SCORER_SYSTEM_PROMPT, format_user_prompt

# Valid values for structured fields
_VALID_VERDICTS = {"strong_fit", "likely_fit", "maybe", "reject"}
_VALID_ITEM_TYPES = {
    "python_library", "duckdb_extension", "ai_tool", "agent_workflow",
    "model_release", "platform_infra", "concept_pattern", "article",
    "book_paper", "coding_tool", "vibe_coding_tool", "ai_architecture",
    "infra_reference",
}


class Scorer:
    """
    Scores newsletter items using Claude API.

    Usage:
        scorer = Scorer()
        result = scorer.score_item(item)
        print(result["verdict"], result["score"])
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-5-20250929",
        max_text_chars: int = 3000,
        max_retries: int = 2,
        feedback_examples: str = "",
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found. Pass api_key= or set the env var."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model
        self._max_text_chars = max_text_chars
        self._max_retries = max_retries
        self._feedback_examples = feedback_examples

        # Token usage tracking (guarded by _lock for thread safety)
        self._lock = threading.Lock()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._items_scored = 0
        self._errors = 0

    # ── Public methods ─────────────────────────────────────────

    def score_item(self, item: dict) -> dict:
        """
        Score a single item via Claude API.

        Args:
            item: Dict from ContentExtractor (has url, link_text, title, text, etc.)

        Returns:
            Scored dict with score, verdict, item_type, reasoning, etc.
        """
        user_prompt = format_user_prompt(item, self._max_text_chars)
        url = item.get("resolved_url") or item.get("source_url") or item.get("url", "")
        link_text = item.get("link_text", "")

        system_prompt = SCORER_SYSTEM_PROMPT
        if self._feedback_examples:
            system_prompt = system_prompt + "\n" + self._feedback_examples

        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=512,
                    temperature=0.2,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )

                raw_text = response.content[0].text
                result = self._parse_response(raw_text)
                result["url"] = url
                result["link_text"] = link_text

                # Track token usage (thread-safe)
                with self._lock:
                    self._total_input_tokens += response.usage.input_tokens
                    self._total_output_tokens += response.usage.output_tokens
                    self._items_scored += 1
                return result

            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = str(exc)
                if attempt < self._max_retries:
                    continue
            except anthropic.APIError as exc:
                last_error = str(exc)
                break

        with self._lock:
            self._errors += 1
        return self._error_result(item, f"scoring failed after retries: {last_error}")

    def score_batch(self, items: list[dict], max_workers: int = 4) -> list[dict]:
        """
        Score a list of items in parallel with progress output.

        Args:
            items: List of dicts from ContentExtractor.
            max_workers: Number of concurrent scoring threads (default 4).

        Returns:
            List of scored dicts (same order as input).
        """
        total = len(items)

        def _score_one(args: tuple[int, dict]) -> dict:
            i, item = args
            link_text = item.get("link_text", "?")[:40]
            link_text = link_text.encode("ascii", errors="replace").decode("ascii")
            print(f"  [{i}/{total}] Scoring: {link_text}")

            result = self.score_item(item)
            verdict = result.get("verdict", "?")
            score = result.get("score", "?")
            print(f"  [{i}/{total}] -> {verdict} (score: {score})")
            return result

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_score_one, enumerate(items, 1)))

        return results

    def stats(self) -> dict:
        """Return token usage and scoring statistics."""
        return {
            "items_scored": self._items_scored,
            "errors": self._errors,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
        }

    # ── Internal methods ───────────────────────────────────────

    @staticmethod
    def _parse_response(raw_text: str) -> dict:
        """
        Parse LLM response text into a structured dict.

        Handles code fences, validates verdict, and fills defaults.
        """
        # Strip markdown code fences if present
        text = raw_text.strip()
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        data = json.loads(text)

        # Ensure score is int
        score = int(data.get("score", 0))

        # Validate/correct verdict based on score
        verdict = data.get("verdict", "")
        if verdict not in _VALID_VERDICTS:
            if score >= 5:
                verdict = "strong_fit"
            elif score >= 3:
                verdict = "likely_fit"
            elif score >= 1:
                verdict = "maybe"
            else:
                verdict = "reject"

        # Validate item_type
        item_type = data.get("item_type", "article")
        if item_type not in _VALID_ITEM_TYPES:
            item_type = "article"

        return {
            "score": score,
            "verdict": verdict,
            "item_type": item_type,
            "description": data.get("description", ""),
            "reasoning": data.get("reasoning", ""),
            "signals": data.get("signals", []),
            "suggested_name": data.get("suggested_name", ""),
            "suggested_category": data.get("suggested_category", ""),
            "tags": data.get("tags", []),
            "is_listicle": bool(data.get("is_listicle", False)),
            "listicle_item_type": data.get("listicle_item_type"),
            "pillar": data.get("pillar", ""),
            "overlap": data.get("overlap", ""),
            "relevance": data.get("relevance", ""),
            "usefulness": data.get("usefulness", ""),
            "usefulness_notes": data.get("usefulness_notes", ""),
        }

    @staticmethod
    def _error_result(item: dict, error_msg: str) -> dict:
        """Build a fallback result dict when scoring fails."""
        return {
            "score": 0,
            "verdict": "error",
            "item_type": "article",
            "description": "",
            "reasoning": error_msg,
            "signals": [],
            "suggested_name": "",
            "suggested_category": "",
            "tags": [],
            "url": item.get("resolved_url") or item.get("source_url") or item.get("url", ""),
            "link_text": item.get("link_text", ""),
            "is_listicle": False,
            "listicle_item_type": None,
            "pillar": "",
            "overlap": "",
            "relevance": "",
            "usefulness": "",
            "usefulness_notes": "",
        }
