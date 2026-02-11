"""
Listicle Exploder for Newsletter Curator.

Detects listicle articles (e.g. "10 Python Libraries for Data Science") and
extracts individual sub-items via a second Claude API call. Sub-items are fed
into the normal routing/dedup/review pipeline as standalone entries.
"""

import json
import os
import re

import anthropic


# Item types that can be exploded into individual database entries
EXPLODABLE_TYPES = {
    "python_library",
    "duckdb_extension",
    "ai_tool",
    "coding_tool",
    "vibe_coding_tool",
    "platform_infra",
}

_EXTRACTION_SYSTEM_PROMPT = """\
You are extracting individual tools/libraries/products from a listicle article. \
For each distinct item mentioned in the article, extract its details as a separate entry.

Return ONLY valid JSON (no markdown fences, no extra text) with this structure:
{
    "items": [
        {
            "suggested_name": "<clean name of the tool/library/product>",
            "description": "<1-2 sentence description of what it does>",
            "suggested_category": "<e.g. 'Data Validation', 'LLM Framework'>",
            "tags": ["<2-5 relevant tags>"],
            "score": <integer 0-10, based on relevance to a Python/AI developer>,
            "verdict": "<strong_fit|likely_fit|maybe|reject>",
            "reasoning": "<1 sentence explaining the score>"
        }
    ]
}

Guidelines:
- Only extract items that are concrete tools, libraries, or products — skip generic advice or filler
- Each item should be independently useful as a database entry
- Use the same verdict thresholds: 5+ = strong_fit, 3-4 = likely_fit, 1-2 = maybe, 0- = reject
- If the article mentions a tool only in passing (1 sentence, no detail), still include it but score lower
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
        api_key: str | None = None,
        model: str = "claude-sonnet-4-5-20250929",
        max_text_chars: int = 6000,
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found. Pass api_key= or set the env var."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model
        self._max_text_chars = max_text_chars

        # Stats tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._items_exploded = 0
        self._sub_items_created = 0
        self._errors = 0

    def should_explode(self, scored_item: dict) -> bool:
        """Check if a scored item is an explodable listicle."""
        return (
            scored_item.get("is_listicle", False)
            and scored_item.get("listicle_item_type") in EXPLODABLE_TYPES
            and scored_item.get("verdict") not in ("reject", "error")
        )

    def explode_item(self, scored_item: dict) -> list[dict]:
        """
        Extract individual sub-items from a listicle article via Claude API.

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

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                temperature=0.2,
                system=_EXTRACTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            self._total_input_tokens += response.usage.input_tokens
            self._total_output_tokens += response.usage.output_tokens

            raw_text = response.content[0].text.strip()
            raw_text = re.sub(r"^```(?:json)?\s*\n?", "", raw_text)
            raw_text = re.sub(r"\n?```\s*$", "", raw_text)
            raw_text = raw_text.strip()

            data = json.loads(raw_text)
            raw_items = data.get("items", [])

            if not raw_items:
                return []

            sub_items = []
            for raw in raw_items:
                sub = {
                    "score": int(raw.get("score", 0)),
                    "verdict": raw.get("verdict", "maybe"),
                    "item_type": item_type,
                    "description": raw.get("description", ""),
                    "reasoning": raw.get("reasoning", ""),
                    "signals": [],
                    "suggested_name": raw.get("suggested_name", ""),
                    "suggested_category": raw.get("suggested_category", ""),
                    "tags": raw.get("tags", []),
                    "is_listicle": False,
                    "listicle_item_type": None,
                    "source_article": title,
                    # Inherit parent fields
                    "url": url,
                    "link_text": scored_item.get("link_text", ""),
                    "title": scored_item.get("title"),
                    "author": scored_item.get("author"),
                    "text": scored_item.get("text"),
                    "_email_meta": scored_item.get("_email_meta", {}),
                }
                sub_items.append(sub)

            self._items_exploded += 1
            self._sub_items_created += len(sub_items)
            return sub_items

        except (json.JSONDecodeError, KeyError, IndexError, anthropic.APIError) as exc:
            self._errors += 1
            print(f"  Exploder error for '{title}': {exc}")
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
            "items_exploded": self._items_exploded,
            "sub_items_created": self._sub_items_created,
            "errors": self._errors,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_tokens": self._total_input_tokens + self._total_output_tokens,
        }
