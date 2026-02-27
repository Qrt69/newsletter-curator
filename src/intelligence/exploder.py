"""
Listicle Exploder for Newsletter Curator.

Detects listicle articles (e.g. "10 Python Libraries for Data Science") and
extracts individual sub-items via a second LLM call. Sub-items are fed
into the normal routing/dedup/review pipeline as standalone entries.

Supports two backends (same pattern as Scorer):
  - "local"     (default): LM Studio or any OpenAI-compatible local server
  - "anthropic": Claude API via the Anthropic SDK

Set SCORER_BACKEND env var to choose (default: "local").
"""

import json
import os
import re
import threading
from collections import defaultdict

import anthropic
import openai
from json_repair import repair_json

from src.intelligence.prompts import INTEREST_PROFILE_BLOCK


# Item types that can be exploded into individual database entries
EXPLODABLE_TYPES = {
    "python_library",
    "duckdb_extension",
    "ai_tool",
    "coding_tool",
    "vibe_coding_tool",
    "platform_infra",
}

# Defaults for local LM Studio backend
_DEFAULT_LLM_BASE_URL = "http://localhost:1234/v1"
_DEFAULT_LLM_API_KEY = "lm-studio"

_EXTRACTION_SYSTEM_PROMPT = """\
You are extracting individual tools/libraries/products from a listicle article. \
For each distinct item mentioned in the article, extract its details as a separate entry.

""" + INTEREST_PROFILE_BLOCK + """
IMPORTANT: The score is the SUM of all applicable signals. Start at 0 and add/subtract \
points for EVERY signal that applies.

Return ONLY valid JSON (no markdown fences, no extra text) with this structure:
{
    "items": [
        {
            "suggested_name": "<clean name of the tool/library/product>",
            "description": "<1-2 sentence description of what it does>",
            "suggested_category": "<e.g. 'Data Validation', 'LLM Framework'>",
            "tags": ["<2-5 relevant tags>"],
            "score": <integer, can be negative — sum of all applicable signals>,
            "reasoning": "<1 sentence explaining the score>",
            "signals": ["+3 matches Python libraries", "+2 has GitHub repo", ...],
            "url": "<direct URL to tool's homepage/GitHub/docs if found in article, else null>"
        }
    ]
}

Guidelines:
- Only extract items that are concrete tools, libraries, or products -- skip generic advice or filler
- Each item should be independently useful as a database entry
- Use the same verdict thresholds: 5+ = strong_fit, 3-4 = likely_fit, 1-2 = maybe, 0- = reject
- If the article mentions a tool only in passing (1 sentence, no detail), still include it but score lower
- If the article contains a direct URL for an item (homepage, GitHub, docs), extract it. Only use URLs actually present in the article text.
"""

_PYTHON_LIBRARY_SYSTEM_PROMPT = """\
You are extracting individual Python libraries from a listicle article. \
For each distinct library mentioned in the article, extract its details as a separate entry.

""" + INTEREST_PROFILE_BLOCK.replace("{", "{{").replace("}", "}}") + """
IMPORTANT: The score is the SUM of all applicable signals. Start at 0 and add/subtract \
points for EVERY signal that applies.

Return ONLY valid JSON (no markdown fences, no extra text) with this structure:
{{
    "items": [
        {{
            "suggested_name": "<clean name of the library>",
            "description": "<1-2 sentence description of what it does>",
            "suggested_category": "<e.g. 'Data Validation', 'LLM Framework'>",
            "pillar": "<one of: Core Python, Data science, AI/ML/NLP, UI/Apps, Infrastructure>",
            "overlap": "<name similar/competing libraries, e.g. 'Similar to requests; async alternative to aiohttp'>",
            "relevance": "<1 sentence on why this matters for a Python/AI developer>",
            "usefulness": "<High|Medium|Low>",
            "usefulness_notes": "<brief note on practical use>",
            "tags": ["<2-5 relevant tags>"],
            "score": <integer, can be negative -- sum of all applicable signals>,
            "reasoning": "<1 sentence explaining the score>",
            "signals": ["+3 matches Python libraries", "+2 has GitHub repo", ...],
            "url": "<direct URL to library's homepage/GitHub/PyPI if found in article, else null>"
        }}
    ]
}}

### Pillar definitions
- "Core Python": utilities, CLI, testing, packaging, type checking
- "Data science": pandas, polars, visualization, data processing, statistics
- "AI/ML/NLP": pytorch, transformers, LLM tools, NLP, vector DBs, RAG
- "UI/Apps": streamlit, reflex, nicegui, web frameworks, dashboards
- "Infrastructure": airflow, dagster, orchestration, deployment, DevOps

{{category_context}}

Guidelines:
- Only extract items that are concrete Python libraries or packages -- skip generic advice or filler
- Each item should be independently useful as a database entry
- Use the same verdict thresholds: 5+ = strong_fit, 3-4 = likely_fit, 1-2 = maybe, 0- = reject
- If the article mentions a library only in passing (1 sentence, no detail), still include it but score lower
- Prefer assigning categories that already exist in the Notion database (listed above) when they fit
- If the article contains a direct URL for a library (homepage, GitHub, PyPI), extract it. Only use URLs actually present in the article text.
"""

_EXTRACTION_USER_TEMPLATE = """\
Extract individual {item_type} items from this listicle article.

Title: {title}
URL: {url}

Article text (first {max_text_chars} chars):
{text}
"""


class ListicleExploder:
    """
    Detects and explodes listicle articles into individual sub-items.

    Usage:
        exploder = ListicleExploder()
        scored_items = exploder.process_batch(scored_items)
    """

    def __init__(
        self,
        backend: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_text_chars: int = 6000,
        notion_client=None,
        dedup_index=None,
    ):
        self._backend = backend or os.environ.get("SCORER_BACKEND", "local")
        self._max_text_chars = max_text_chars
        self._notion_client = notion_client
        self._dedup_index = dedup_index

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
            base_url = os.environ.get("LLM_BASE_URL", _DEFAULT_LLM_BASE_URL)
            llm_key = api_key or os.environ.get("LLM_API_KEY", _DEFAULT_LLM_API_KEY)
            self._openai_client = openai.OpenAI(base_url=base_url, api_key=llm_key)
            self._anthropic_client = None
            self._model = model or os.environ.get("LLM_MODEL", "")
            if not self._model:
                self._model = self._auto_detect_model()
            self._local_json_mode = True

        # Lazy-loaded Notion category context
        self._category_context: str | None = None
        self._category_lock = threading.Lock()

        # Stats tracking
        self._lock = threading.Lock()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._items_exploded = 0
        self._sub_items_created = 0
        self._dedup_filtered = 0
        self._errors = 0

    def _auto_detect_model(self) -> str:
        """Query the local server for available models and pick the first one."""
        try:
            models = self._openai_client.models.list()
            if models.data:
                model_id = models.data[0].id
                print(f"  [Exploder] Auto-detected local model: {model_id}")
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

    # -- LLM call abstraction --

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
                max_tokens=2048,
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
                max_tokens=2048,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            try:
                if self._local_json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                response = self._openai_client.chat.completions.create(**kwargs)
            except (openai.BadRequestError, openai.APIError):
                self._local_json_mode = False
                kwargs.pop("response_format", None)
                response = self._openai_client.chat.completions.create(**kwargs)
            raw_text = response.choices[0].message.content or ""
            usage = response.usage
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0
            return raw_text, input_tokens, output_tokens

    # -- Notion category context --

    def _build_category_context(self) -> str:
        """
        Query Python Libraries DB and build a pillar->categories context string.

        Returns a formatted block like:
            ### Existing categories in Notion
            **Core Python:** Testing, CLI, Packaging
            **Data science:** Visualization, Data Processing
            ...

        Returns empty string if no NotionClient available or query fails.
        """
        if self._notion_client is None:
            return ""

        try:
            entries = self._notion_client.query_database("Python Libraries")
        except Exception as exc:
            print(f"  [Exploder] Failed to query Python Libraries DB: {exc}")
            return ""

        # Group by Pillar -> set of Categories
        pillar_categories: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            pillar = entry.get("Pillar")
            category = entry.get("Category")
            if pillar and category:
                pillar_categories[pillar].add(category)

        if not pillar_categories:
            return ""

        lines = ["### Existing categories in the Notion Python Libraries database"]
        lines.append("Prefer assigning one of these existing categories when they fit:")
        for pillar in sorted(pillar_categories):
            cats = sorted(pillar_categories[pillar])
            lines.append(f"- **{pillar}:** {', '.join(cats)}")

        return "\n".join(lines)

    def _get_category_context(self) -> str:
        """Lazy-load category context (thread-safe, called once)."""
        if self._category_context is None:
            with self._category_lock:
                if self._category_context is None:
                    self._category_context = self._build_category_context()
                    if self._category_context:
                        print("  [Exploder] Loaded Notion category context for python_library exploding")
        return self._category_context

    # -- Public methods --

    def should_explode(self, scored_item: dict) -> bool:
        """Check if a scored item is an explodable listicle."""
        return (
            scored_item.get("is_listicle", False)
            and scored_item.get("listicle_item_type") in EXPLODABLE_TYPES
            and scored_item.get("verdict") not in ("reject", "error")
        )

    def explode_item(self, scored_item: dict) -> list[dict]:
        """
        Extract individual sub-items from a listicle article via LLM call.

        Returns list of sub-item dicts (same shape as scorer output, with
        source_article set to the parent's suggested_name). Returns empty
        list on failure.
        """
        item_type = scored_item.get("listicle_item_type", "article")
        title = scored_item.get("suggested_name") or scored_item.get("title") or ""
        url = scored_item.get("url", "")
        text = (scored_item.get("text") or "")[:self._max_text_chars]

        if not text:
            text = "[No article text available]"

        user_prompt = _EXTRACTION_USER_TEMPLATE.format(
            item_type=item_type,
            title=title,
            url=url,
            text=text,
            max_text_chars=self._max_text_chars,
        )

        # Pick the right system prompt
        if item_type == "python_library":
            category_context = self._get_category_context()
            system_prompt = _PYTHON_LIBRARY_SYSTEM_PROMPT.format(
                category_context=category_context,
            )
        else:
            system_prompt = _EXTRACTION_SYSTEM_PROMPT

        try:
            raw_text, in_tok, out_tok = self._call_llm(system_prompt, user_prompt)

            with self._lock:
                self._total_input_tokens += in_tok
                self._total_output_tokens += out_tok

            raw_items = self._parse_extraction_response(raw_text)

            if not raw_items:
                return []

            sub_items = []
            for raw in raw_items:
                score = int(raw.get("score", 0))

                # Derive verdict from score (same as Scorer)
                if score >= 5:
                    verdict = "strong_fit"
                elif score >= 3:
                    verdict = "likely_fit"
                elif score >= 1:
                    verdict = "maybe"
                else:
                    verdict = "reject"

                sub = {
                    "score": score,
                    "verdict": verdict,
                    "item_type": item_type,
                    "description": raw.get("description", ""),
                    "reasoning": raw.get("reasoning", ""),
                    "signals": raw.get("signals", []),
                    "suggested_name": raw.get("suggested_name", ""),
                    "suggested_category": raw.get("suggested_category", ""),
                    "tags": raw.get("tags", []),
                    "is_listicle": False,
                    "listicle_item_type": None,
                    "source_article": title,
                    # Python library extra fields
                    "pillar": raw.get("pillar", ""),
                    "overlap": raw.get("overlap", ""),
                    "relevance": raw.get("relevance", ""),
                    "usefulness": raw.get("usefulness", ""),
                    "usefulness_notes": raw.get("usefulness_notes", ""),
                    # Prefer individual URL from LLM, fall back to parent
                    "url": raw.get("url") or url,
                    "link_text": scored_item.get("link_text", ""),
                    "title": scored_item.get("title"),
                    "author": scored_item.get("author"),
                    "text": scored_item.get("text"),
                    "_email_meta": scored_item.get("_email_meta", {}),
                }
                sub_items.append(sub)

            # Pre-dedup filtering: remove sub-items already in Notion
            if self._dedup_index and sub_items:
                filtered = []
                for sub in sub_items:
                    name = sub.get("suggested_name", "")
                    sub_url = sub.get("url", "")
                    matches = self._dedup_index.search(name=name, url=sub_url)
                    if matches:
                        match_name = matches[0].get("name", "?")
                        print(f"    [dedup] Skipping '{name}' -- already in Notion as '{match_name}'")
                        with self._lock:
                            self._dedup_filtered += 1
                    else:
                        filtered.append(sub)
                sub_items = filtered

            with self._lock:
                self._items_exploded += 1
                self._sub_items_created += len(sub_items)
            return sub_items

        except (anthropic.APIError, openai.APIError, openai.APIConnectionError) as exc:
            with self._lock:
                self._errors += 1
            print(f"  Exploder API error for '{title}': {exc}")
            return []

    def process_batch(self, scored_items: list[dict]) -> list[dict]:
        """
        Process a batch of scored items: replace eligible listicles with
        their extracted sub-items, pass through everything else unchanged.

        If extraction fails for a listicle, the parent item is kept.
        """
        result = []
        for item in scored_items:
            if self.should_explode(item):
                name = (item.get("suggested_name") or "?")[:50]
                name = name.encode("ascii", errors="replace").decode("ascii")
                print(f"  Exploding listicle: {name}")
                sub_items = self.explode_item(item)
                if sub_items:
                    print(f"    -> Extracted {len(sub_items)} sub-items")
                    result.extend(sub_items)
                else:
                    # Graceful degradation: keep the parent if extraction fails
                    print(f"    -> No sub-items extracted, keeping parent")
                    result.append(item)
            else:
                result.append(item)
        return result

    def stats(self) -> dict:
        """Return token usage and explosion statistics."""
        return {
            "backend": self._backend,
            "model": self._model,
            "items_exploded": self._items_exploded,
            "sub_items_created": self._sub_items_created,
            "dedup_filtered": self._dedup_filtered,
            "errors": self._errors,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
        }

    # -- Internal methods --

    @staticmethod
    def _parse_extraction_response(raw_text: str) -> list[dict]:
        """
        Parse LLM extraction response into a list of item dicts.

        Uses json_repair to handle common LLM JSON issues (missing commas,
        trailing commas, truncated output, code fences, etc.).
        """
        text = raw_text.strip()

        # Strip markdown code fences
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

        data = repair_json(text, return_objects=True)

        if isinstance(data, dict):
            return data.get("items", [])

        if isinstance(data, list):
            # LLM returned bare array instead of {"items": [...]}
            return data

        return []
