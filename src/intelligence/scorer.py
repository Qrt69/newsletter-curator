"""
Scorer for Newsletter Curator.

Supports two backends:
  - "local"     (default): LM Studio or any OpenAI-compatible local server
  - "anthropic": Claude API via the Anthropic SDK

Set SCORER_BACKEND env var to choose (default: "local").
"""

import json
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import anthropic
import openai
from json_repair import repair_json

from .prompts import SCORER_SYSTEM_PROMPT, format_user_prompt

# Valid values for structured fields
_VALID_VERDICTS = {"strong_fit", "likely_fit", "maybe", "reject"}
_VALID_ITEM_TYPES = {
    "python_library", "duckdb_extension", "ai_tool", "agent_workflow",
    "model_release", "platform_infra", "concept_pattern", "article",
    "book_paper", "coding_tool", "vibe_coding_tool", "ai_architecture",
    "infra_reference",
}

# Defaults for local LM Studio backend
_DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"
_DEFAULT_LLM_API_KEY = "lm-studio"


class Scorer:
    """
    Scores newsletter items using an LLM backend.

    Usage:
        scorer = Scorer()                   # uses local LM Studio by default
        scorer = Scorer(backend="anthropic") # uses Claude API
        result = scorer.score_item(item)
        print(result["verdict"], result["score"])
    """

    def __init__(
        self,
        backend: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_text_chars: int = 3000,
        max_retries: int = 2,
        feedback_examples: str = "",
    ):
        self._backend = backend or os.environ.get("SCORER_BACKEND", "local")
        self._max_text_chars = max_text_chars
        self._max_retries = max_retries
        self._feedback_examples = feedback_examples

        if self._backend == "anthropic":
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY not found. Pass api_key= or set the env var."
                )
            self._anthropic_client = anthropic.Anthropic(api_key=key)
            self._openai_client = None
            self._model = model or "claude-sonnet-4-5-20250929"
        else:
            # Local (LM Studio / OpenAI-compatible)
            base_url = os.environ.get("LLM_BASE_URL", _DEFAULT_LLM_BASE_URL)
            llm_key = api_key or os.environ.get("LLM_API_KEY", _DEFAULT_LLM_API_KEY)
            self._openai_client = openai.OpenAI(base_url=base_url, api_key=llm_key)
            self._anthropic_client = None
            self._model = model or os.environ.get("LLM_MODEL", "")
            if not self._model:
                self._model = self._auto_detect_model()
            self._local_json_mode = True  # try JSON mode, disable on error

        # Token usage tracking (guarded by _lock for thread safety)
        self._lock = threading.Lock()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._items_scored = 0
        self._errors = 0

    def _auto_detect_model(self) -> str:
        """Query the local server for available models and pick the first one."""
        try:
            models = self._openai_client.models.list()
            if models.data:
                model_id = models.data[0].id
                print(f"  [Scorer] Auto-detected local model: {model_id}")
                return model_id
        except Exception as exc:
            raise ConnectionError(
                f"Cannot reach LM Studio at {self._openai_client.base_url}. "
                f"Start LM Studio and load a model, or set SCORER_BACKEND=anthropic. "
                f"Error: {exc}"
            ) from exc
        raise ConnectionError(
            f"LM Studio at {self._openai_client.base_url} has no models loaded. "
            "Load a model in LM Studio first."
        )

    # ── LLM call abstraction ───────────────────────────────────

    def _call_llm(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str, int, int]:
        """
        Call the configured LLM backend.

        Returns:
            (raw_text, input_tokens, output_tokens)
        """
        if self._backend == "anthropic":
            response = self._anthropic_client.messages.create(
                model=self._model,
                max_tokens=512,
                temperature=0.2,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return (
                response.content[0].text,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
        else:
            kwargs = dict(
                model=self._model,
                max_tokens=512,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            # Try JSON mode first (supported by LM Studio 0.3+)
            try:
                if self._local_json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                response = self._openai_client.chat.completions.create(**kwargs)
            except (openai.BadRequestError, openai.APIError):
                # Model/server doesn't support response_format, fall back
                self._local_json_mode = False
                kwargs.pop("response_format", None)
                response = self._openai_client.chat.completions.create(**kwargs)
            raw_text = response.choices[0].message.content or ""
            usage = response.usage
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0
            return raw_text, input_tokens, output_tokens

    # ── Public methods ─────────────────────────────────────────

    def score_item(self, item: dict) -> dict:
        """
        Score a single item via the configured LLM backend.

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

        text_chars = self._max_text_chars
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                raw_text, in_tok, out_tok = self._call_llm(system_prompt, user_prompt)

                result = self._parse_response(raw_text)
                result["url"] = url
                result["link_text"] = link_text

                # Fallback: ensure suggested_name is never blank
                if not result.get("suggested_name"):
                    result["suggested_name"] = (
                        item.get("title")
                        or link_text
                        or url
                    )

                # Track token usage (thread-safe)
                with self._lock:
                    self._total_input_tokens += in_tok
                    self._total_output_tokens += out_tok
                    self._items_scored += 1
                return result

            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = str(exc)
                if attempt < self._max_retries:
                    print(f"  [Scorer] Retry {attempt+1}/{self._max_retries} - parse error: {exc}")
                    print(f"  [Scorer] Raw (first 300 chars): {raw_text[:300]}")
                    continue
            except (openai.BadRequestError, anthropic.BadRequestError) as exc:
                err_str = str(exc)
                # Context overflow: prompt too long for model — retry with less text
                if "n_keep" in err_str or "n_ctx" in err_str or "context" in err_str.lower():
                    text_chars = text_chars // 2
                    if text_chars < 100:
                        last_error = err_str
                        break
                    print(f"  [Scorer] Context overflow, retrying with {text_chars} chars")
                    user_prompt = format_user_prompt(item, text_chars)
                    continue
                last_error = err_str
                break
            except (anthropic.APIError, openai.APIError, openai.APIConnectionError) as exc:
                last_error = str(exc)
                break

        with self._lock:
            self._errors += 1
        return self._error_result(item, f"scoring failed after retries: {last_error}")

    def score_batch(
        self,
        items: list[dict],
        max_workers: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """
        Score a list of items in parallel with progress output.

        Args:
            items: List of dicts from ContentExtractor.
            max_workers: Number of concurrent scoring threads.
                         Defaults to 1 for local backend, 4 for anthropic.
            on_progress: Optional callback(current, total) called after each item is scored.

        Returns:
            List of scored dicts (same order as input).
        """
        if max_workers is None:
            max_workers = 1 if self._backend == "local" else 4

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
            if on_progress is not None:
                on_progress(i, total)
            return result

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_score_one, enumerate(items, 1)))

        return results

    def stats(self) -> dict:
        """Return token usage and scoring statistics."""
        return {
            "backend": self._backend,
            "model": self._model,
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

        Uses json_repair to handle all common LLM JSON issues (missing commas,
        trailing commas, truncated output, unquoted keys, code fences, etc.).
        """
        text = raw_text.strip()

        # Strip markdown code fences
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

        # json_repair handles everything else: missing/trailing commas,
        # unquoted keys, truncated output, single quotes, etc.
        data = repair_json(text, return_objects=True)

        # repair_json may return a list or string if the input was very broken
        if not isinstance(data, dict):
            raise json.JSONDecodeError("repair_json did not produce a dict", text, 0)

        # Ensure score is int
        score = int(data.get("score", 0))

        # Always derive verdict from score to prevent contradictions
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
        url = item.get("resolved_url") or item.get("source_url") or item.get("url", "")
        return {
            "score": 0,
            "verdict": "error",
            "item_type": "article",
            "description": "",
            "reasoning": error_msg,
            "signals": [],
            "suggested_name": item.get("title") or item.get("link_text", "") or url,
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
