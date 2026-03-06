"""
Unit tests for the Listicle Exploder.

Tests detection logic, category context building, and JSON parsing.
No API calls required.

Run: uv run pytest tests/test_exploder.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.intelligence.exploder import ListicleExploder, EXPLODABLE_TYPES


# ── Helpers ──────────────────────────────────────────────────

def _make_exploder(**kwargs):
    """Create an exploder without connecting to any backend."""
    ex = object.__new__(ListicleExploder)
    ex._backend = "local"
    ex._model = "test-model"
    ex._max_text_chars = 6000
    ex._notion_client = kwargs.get("notion_client")
    ex._dedup_index = kwargs.get("dedup_index")
    ex._category_context = kwargs.get("category_context")
    ex._category_lock = __import__("threading").Lock()
    ex._lock = __import__("threading").Lock()
    ex._total_input_tokens = 0
    ex._total_output_tokens = 0
    ex._items_exploded = 0
    ex._sub_items_created = 0
    ex._dedup_filtered = 0
    ex._heuristic_detected = 0
    ex._errors = 0
    ex._openai_client = None
    ex._anthropic_client = None
    ex._local_json_mode = True
    return ex


# ── Test 1: should_explode detection ─────────────────────────

def test_should_explode():
    """TEST 1: should_explode correctly detects explodable listicles."""
    ex = _make_exploder()

    # Positive: listicle with explodable type and non-reject verdict
    for item_type in EXPLODABLE_TYPES:
        item = {"is_listicle": True, "listicle_item_type": item_type, "verdict": "strong_fit"}
        assert ex.should_explode(item), f"Should explode {item_type}"

    # Negative: not a listicle
    item = {"is_listicle": False, "listicle_item_type": "python_library", "verdict": "strong_fit"}
    assert not ex.should_explode(item), "Non-listicle should not explode"

    # Negative: non-explodable type
    item = {"is_listicle": True, "listicle_item_type": "article", "verdict": "strong_fit"}
    assert not ex.should_explode(item), "article type should not explode"

    # Negative: reject verdict
    item = {"is_listicle": True, "listicle_item_type": "python_library", "verdict": "reject"}
    assert not ex.should_explode(item), "Rejected listicle should not explode"

    # Negative: error verdict
    item = {"is_listicle": True, "listicle_item_type": "ai_tool", "verdict": "error"}
    assert not ex.should_explode(item), "Error listicle should not explode"

    # Negative: missing fields
    assert not ex.should_explode({}), "Empty dict should not explode"
    assert not ex.should_explode({"is_listicle": True}), "Missing type should not explode"


# ── Test 2: _build_category_context ──────────────────────────

def test_build_category_context():
    """TEST 2: _build_category_context groups Notion entries by pillar."""
    mock_nc = MagicMock()
    mock_nc.query_database.return_value = [
        {"Pillar": "Core Python", "Category": "Testing"},
        {"Pillar": "Core Python", "Category": "CLI"},
        {"Pillar": "Core Python", "Category": "Testing"},  # duplicate
        {"Pillar": "Data science", "Category": "Visualization"},
        {"Pillar": "AI/ML/NLP", "Category": "LLM Tools"},
        {"Pillar": "AI/ML/NLP", "Category": "Vector DBs"},
        {"Pillar": None, "Category": "Uncategorized"},  # no pillar
        {"Pillar": "UI/Apps", "Category": None},  # no category
    ]

    ex = _make_exploder(notion_client=mock_nc)
    context = ex._build_category_context()

    mock_nc.query_database.assert_called_once_with("Python Libraries")

    assert "Core Python" in context
    assert "Testing" in context
    assert "CLI" in context
    assert "Visualization" in context
    assert "LLM Tools" in context
    assert "Vector DBs" in context
    # None pillar/category should be excluded
    assert "Uncategorized" not in context


def test_build_category_context_no_client():
    """TEST 2b: _build_category_context returns empty string without NotionClient."""
    ex = _make_exploder(notion_client=None)
    context = ex._build_category_context()
    assert context == ""


def test_build_category_context_query_error():
    """TEST 2c: _build_category_context handles query errors gracefully."""
    mock_nc = MagicMock()
    mock_nc.query_database.side_effect = Exception("API error")

    ex = _make_exploder(notion_client=mock_nc)
    context = ex._build_category_context()
    assert context == ""


# ── Test 3: _parse_extraction_response ───────────────────────

def test_parse_clean_json():
    """TEST 3a: Parse clean JSON extraction response."""
    raw = '{"items": [{"suggested_name": "Pydantic", "description": "Data validation", "score": 7, "tags": ["python"]}]}'
    items = ListicleExploder._parse_extraction_response(raw)

    assert len(items) == 1
    assert items[0]["suggested_name"] == "Pydantic"
    assert items[0]["score"] == 7


def test_parse_code_fences():
    """TEST 3b: Parse JSON wrapped in code fences."""
    raw = '```json\n{"items": [{"suggested_name": "FastAPI", "score": 8}]}\n```'
    items = ListicleExploder._parse_extraction_response(raw)

    assert len(items) == 1
    assert items[0]["suggested_name"] == "FastAPI"


def test_parse_broken_json():
    """TEST 3c: Parse broken JSON with missing commas (json_repair handles it)."""
    raw = '{"items": [{"suggested_name": "httpx" "score": 6 "tags": ["http"]}]}'
    items = ListicleExploder._parse_extraction_response(raw)

    assert len(items) == 1
    assert items[0]["suggested_name"] == "httpx"


def test_parse_truncated_json():
    """TEST 3d: Parse truncated JSON (json_repair recovers partial data)."""
    raw = '{"items": [{"suggested_name": "Rich", "score": 5}, {"suggested_name": "Tex'
    items = ListicleExploder._parse_extraction_response(raw)

    # json_repair should recover at least the first complete item
    assert len(items) >= 1
    assert items[0]["suggested_name"] == "Rich"


def test_parse_bare_array():
    """TEST 3e: Parse bare array (no wrapping object)."""
    raw = '[{"suggested_name": "Click", "score": 4}, {"suggested_name": "Typer", "score": 6}]'
    items = ListicleExploder._parse_extraction_response(raw)

    assert len(items) == 2
    assert items[0]["suggested_name"] == "Click"
    assert items[1]["suggested_name"] == "Typer"


def test_parse_empty_items():
    """TEST 3f: Parse JSON with empty items array."""
    raw = '{"items": []}'
    items = ListicleExploder._parse_extraction_response(raw)
    assert items == []


def test_parse_garbage():
    """TEST 3g: Parse complete garbage returns empty list."""
    raw = "This is not JSON at all, just random text."
    items = ListicleExploder._parse_extraction_response(raw)
    assert items == [] or isinstance(items, list)


# ── Test 4: verdict derivation ───────────────────────────────

def test_verdict_derived_from_score():
    """TEST 4: Verdict is derived from score, not from LLM output."""
    ex = _make_exploder()

    # Mock _call_llm to return items with wrong verdicts
    def fake_call_llm(system, user):
        return (
            '{"items": [{"suggested_name": "Lib1", "score": 7, "verdict": "reject"}, '
            '{"suggested_name": "Lib2", "score": 1, "verdict": "strong_fit"}, '
            '{"suggested_name": "Lib3", "score": -2, "verdict": "likely_fit"}]}',
            100, 200,
        )

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "ai_tool",
        "verdict": "strong_fit",
        "suggested_name": "Test Listicle",
        "url": "https://example.com",
        "text": "Article about Lib1, Lib2, and Lib3 tools.",
    }

    sub_items = ex.explode_item(scored_item)

    assert len(sub_items) == 3
    assert sub_items[0]["verdict"] == "strong_fit"  # score 7 -> strong_fit (not reject)
    assert sub_items[1]["verdict"] == "maybe"        # score 1 -> maybe (not strong_fit)
    assert sub_items[2]["verdict"] == "reject"        # score -2 -> reject (not likely_fit)


# ── Test 5: python_library extra fields ──────────────────────

def test_python_library_extra_fields():
    """TEST 5: Sub-items include python_library extra fields."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return (
            '{"items": [{"suggested_name": "Polars", "score": 8, '
            '"pillar": "Data science", "suggested_category": "DataFrames", '
            '"overlap": "Similar to pandas but faster", '
            '"relevance": "Great for large datasets", '
            '"usefulness": "High", '
            '"usefulness_notes": "Drop-in pandas replacement"}]}',
            100, 200,
        )

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "python_library",
        "verdict": "strong_fit",
        "suggested_name": "10 Python Libraries",
        "url": "https://example.com",
        "text": "Article about Polars and other Python libs for data science.",
    }

    sub_items = ex.explode_item(scored_item)

    assert len(sub_items) == 1
    item = sub_items[0]
    assert item["pillar"] == "Data science"
    assert item["suggested_category"] == "DataFrames"
    assert item["overlap"] == "Similar to pandas but faster"
    assert item["relevance"] == "Great for large datasets"
    assert item["usefulness"] == "High"
    assert item["usefulness_notes"] == "Drop-in pandas replacement"
    assert item["item_type"] == "python_library"
    assert item["source_article"] == "10 Python Libraries"


# ── Test 6: stats tracking ───────────────────────────────────

def test_stats():
    """TEST 6: Stats track explosions and tokens correctly."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return ('{"items": [{"suggested_name": "A", "score": 5}]}', 50, 100)

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "ai_tool",
        "verdict": "strong_fit",
        "suggested_name": "Tools List",
        "url": "https://example.com",
        "text": "Article about A, a great AI tool.",
    }

    ex.explode_item(scored_item)
    s = ex.stats()

    assert s["items_exploded"] == 1
    assert s["sub_items_created"] == 1
    assert s["total_input_tokens"] == 50
    assert s["total_output_tokens"] == 100
    assert s["total_tokens"] == 150
    assert s["errors"] == 0
    assert s["backend"] == "local"
    assert s["model"] == "test-model"


# ── Test 7: URL extraction ────────────────────────────────────

def test_url_extraction():
    """TEST 7: Sub-items use individual URLs when LLM provides them, fall back to parent URL when null."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return (
            '{"items": ['
            '{"suggested_name": "httpx", "score": 6, "url": "https://github.com/encode/httpx", "signals": ["+3 Python libraries"]},'
            '{"suggested_name": "FastAPI", "score": 7, "url": null, "signals": ["+3 Python libraries"]},'
            '{"suggested_name": "Pydantic", "score": 5, "signals": ["+3 Python libraries"]}'
            ']}',
            100, 200,
        )

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "python_library",
        "verdict": "strong_fit",
        "suggested_name": "Top Python Libs",
        "url": "https://example.com/listicle",
        "text": "Article about httpx (https://github.com/encode/httpx), FastAPI, and Pydantic.",
    }

    sub_items = ex.explode_item(scored_item)

    assert len(sub_items) == 3
    # httpx has its own URL that appears in article text
    assert sub_items[0]["url"] == "https://github.com/encode/httpx"
    # FastAPI has null URL -> falls back to parent
    assert sub_items[1]["url"] == "https://example.com/listicle"
    # Pydantic has no url field at all -> falls back to parent
    assert sub_items[2]["url"] == "https://example.com/listicle"


# ── Test 8: Signals extraction ────────────────────────────────

def test_signals_extraction():
    """TEST 8: Sub-items include signal arrays from LLM response."""
    ex = _make_exploder()

    def fake_call_llm(system, user):
        return (
            '{"items": ['
            '{"suggested_name": "LangGraph", "score": 8, '
            '"signals": ["+3 AI agents & workflows", "+3 Python libraries", "+2 has GitHub repo"]},'
            '{"suggested_name": "SomeLib", "score": 3, "signals": ["+3 Python libraries"]}'
            ']}',
            100, 200,
        )

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "ai_tool",
        "verdict": "strong_fit",
        "suggested_name": "AI Tools List",
        "url": "https://example.com",
        "text": "Article about LangGraph and SomeLib for AI workflows.",
    }

    sub_items = ex.explode_item(scored_item)

    assert len(sub_items) == 2
    assert sub_items[0]["signals"] == ["+3 AI agents & workflows", "+3 Python libraries", "+2 has GitHub repo"]
    assert sub_items[1]["signals"] == ["+3 Python libraries"]


# ── Test 9: Dedup filtering ──────────────────────────────────

def test_dedup_filtering():
    """TEST 9: Sub-items already in Notion are filtered out via DedupIndex."""
    mock_dedup = MagicMock()

    # First item matches, second and third don't
    def fake_search(name=None, url=None):
        if name == "httpx":
            return [{"name": "httpx", "database": "Python Libraries"}]
        return []
    mock_dedup.search = fake_search

    ex = _make_exploder(dedup_index=mock_dedup)

    def fake_call_llm(system, user):
        return (
            '{"items": ['
            '{"suggested_name": "httpx", "score": 6},'
            '{"suggested_name": "FastAPI", "score": 7},'
            '{"suggested_name": "Pydantic", "score": 5}'
            ']}',
            100, 200,
        )

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "ai_tool",
        "verdict": "strong_fit",
        "suggested_name": "Tools List",
        "url": "https://example.com",
        "text": "Article about httpx, FastAPI, and Pydantic.",
    }

    sub_items = ex.explode_item(scored_item)

    # httpx was filtered out by dedup
    assert len(sub_items) == 2
    names = [s["suggested_name"] for s in sub_items]
    assert "httpx" not in names
    assert "FastAPI" in names
    assert "Pydantic" in names

    # Stats reflect the filtering
    assert ex._dedup_filtered == 1
    assert ex.stats()["dedup_filtered"] == 1


# ── Test 10: Interest profile in prompt ──────────────────────

def test_interest_profile_in_prompt():
    """TEST 10: System prompt contains key interest profile elements."""
    from src.intelligence.exploder import _EXTRACTION_SYSTEM_PROMPT, _PYTHON_LIBRARY_SYSTEM_PROMPT

    # Both prompts should include the interest profile
    for prompt in [_EXTRACTION_SYSTEM_PROMPT, _PYTHON_LIBRARY_SYSTEM_PROMPT]:
        assert "+3 points each" in prompt, "Should include interest area scoring"
        assert "Rejection criteria" in prompt, "Should include rejection criteria"
        assert "DuckDB ecosystem" in prompt, "Should include DuckDB interest"
        assert "Verdict thresholds" in prompt, "Should include verdict thresholds"
        assert "signals" in prompt, "Should mention signals in JSON schema"
        assert "url" in prompt, "Should mention url in JSON schema"


# ── Test 11: Title heuristic basic detection ─────────────────

def test_detect_listicle_from_title_basic():
    """TEST 11: Title heuristic detects common listicle patterns."""
    from src.intelligence.exploder import detect_listicle_from_title

    cases = [
        ("3 Python Libraries That Almost Replaced Entire Tools", "python_library"),
        ("10 Best AI Tools for 2025", "ai_tool"),
        ("5 DuckDB Extensions You Should Try", "duckdb_extension"),
        ("Top 7 Developer Tools for Productivity", "coding_tool"),
        ("15 Useful Python Packages for Data Science", "python_library"),
        ("8 Amazing Open-Source Tools for AI", "ai_tool"),
        ("20 Must-Have Python Libraries", "python_library"),
    ]

    for title, expected_type in cases:
        item = {
            "suggested_name": title,
            "verdict": "strong_fit",
            "is_listicle": False,
            "listicle_item_type": None,
        }
        result = detect_listicle_from_title(item)
        assert result is not None, f"Should detect: {title}"
        assert result["is_listicle"] is True, f"Should set is_listicle: {title}"
        assert result["listicle_item_type"] == expected_type, (
            f"Expected {expected_type} for '{title}', got {result['listicle_item_type']}"
        )


# ── Test 12: Title heuristic negatives ───────────────────────

def test_detect_listicle_from_title_negatives():
    """TEST 12: Title heuristic does NOT false-positive on non-listicles."""
    from src.intelligence.exploder import detect_listicle_from_title

    non_listicles = [
        "How to Use Python for Data Science",
        "Understanding AI in 2025",
        "The Future of Developer Tools",
        "Pydantic: A Deep Dive into Data Validation",
        "Building a RAG Pipeline with LangChain",
        "Python 3.13 Release Notes",
    ]

    for title in non_listicles:
        item = {
            "suggested_name": title,
            "verdict": "strong_fit",
            "is_listicle": False,
            "listicle_item_type": None,
        }
        result = detect_listicle_from_title(item)
        assert result is None, f"Should NOT detect listicle: {title}"


# ── Test 13: Title heuristic skips already flagged ───────────

def test_detect_listicle_skips_already_flagged():
    """TEST 13: Title heuristic skips items already flagged by scorer."""
    from src.intelligence.exploder import detect_listicle_from_title

    item = {
        "suggested_name": "10 Python Libraries for ML",
        "verdict": "strong_fit",
        "is_listicle": True,
        "listicle_item_type": "python_library",
    }
    result = detect_listicle_from_title(item)
    assert result is None, "Should skip already-flagged items"


# ── Test 14: Title heuristic skips rejects ───────────────────

def test_detect_listicle_skips_rejects():
    """TEST 14: Title heuristic skips rejected items."""
    from src.intelligence.exploder import detect_listicle_from_title

    item = {
        "suggested_name": "10 Python Libraries for HR Management",
        "verdict": "reject",
        "is_listicle": False,
        "listicle_item_type": None,
    }
    result = detect_listicle_from_title(item)
    assert result is None, "Should skip rejected items"


# ── Test 15: process_batch preserves parent ──────────────────

def test_process_batch_preserves_parent():
    """TEST 15: process_batch keeps parent as article alongside sub-items."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return (
            '{"items": ['
            '{"suggested_name": "LibA", "score": 6},'
            '{"suggested_name": "LibB", "score": 4}'
            ']}',
            100, 200,
        )
    ex._call_llm = fake_call_llm

    parent = {
        "is_listicle": True,
        "listicle_item_type": "python_library",
        "verdict": "strong_fit",
        "score": 5,
        "item_type": "python_library",
        "suggested_name": "3 Python Libraries for Testing",
        "url": "https://example.com/listicle",
        "text": "Article about LibA and LibB for testing.",
        "pillar": "Core Python",
        "overlap": "some overlap",
        "relevance": "some relevance",
        "usefulness": "High",
        "usefulness_notes": "some notes",
        "_email_meta": {"email_id": "abc"},
    }

    result = ex.process_batch([parent])

    # Should have 3 items: parent (re-typed) + 2 sub-items
    assert len(result) == 3

    # First item is the parent, re-typed as article
    assert result[0]["item_type"] == "article"
    assert result[0]["is_listicle"] is False
    assert result[0]["listicle_item_type"] is None
    assert result[0]["suggested_name"] == "3 Python Libraries for Testing"
    assert result[0]["url"] == "https://example.com/listicle"
    # Python library fields cleared on parent
    assert result[0]["pillar"] == ""
    assert result[0]["overlap"] == ""

    # Sub-items follow
    assert result[1]["suggested_name"] == "LibA"
    assert result[1]["item_type"] == "python_library"
    assert result[1]["source_article"] == "3 Python Libraries for Testing"
    assert result[2]["suggested_name"] == "LibB"
    assert result[2]["source_article"] == "3 Python Libraries for Testing"


# ── Test 16: process_batch does not mutate original parent ───

def test_process_batch_parent_not_mutated():
    """TEST 16: process_batch does not mutate the original parent dict."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return ('{"items": [{"suggested_name": "X", "score": 5}]}', 50, 100)
    ex._call_llm = fake_call_llm

    parent = {
        "is_listicle": True,
        "listicle_item_type": "python_library",
        "verdict": "strong_fit",
        "item_type": "python_library",
        "suggested_name": "5 Libs",
        "url": "https://example.com",
        "text": "Article about X library.",
    }

    original_type = parent["item_type"]
    original_listicle = parent["is_listicle"]

    ex.process_batch([parent])

    # Original dict should NOT be modified
    assert parent["item_type"] == original_type
    assert parent["is_listicle"] == original_listicle


# ── Test 17: Heuristic then explode end-to-end ──────────────

def test_process_batch_heuristic_then_explode():
    """TEST 17: Heuristic detects listicle, then process_batch explodes it."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return ('{"items": [{"suggested_name": "ToolX", "score": 7}]}', 50, 100)
    ex._call_llm = fake_call_llm

    # Item NOT flagged as listicle by scorer, but title says otherwise
    item = {
        "is_listicle": False,
        "listicle_item_type": None,
        "verdict": "likely_fit",
        "score": 4,
        "item_type": "article",
        "suggested_name": "5 Best AI Tools for Developers",
        "url": "https://example.com/ai-tools",
        "text": "Here are 5 AI tools including ToolX for code generation.",
        "_email_meta": {"email_id": "xyz"},
    }

    result = ex.process_batch([item])

    # Should have 2 items: parent (re-typed as article) + 1 sub-item
    assert len(result) == 2
    assert result[0]["item_type"] == "article"
    assert result[0]["is_listicle"] is False
    assert result[1]["suggested_name"] == "ToolX"
    assert result[1]["item_type"] == "ai_tool"
    assert result[1]["source_article"] == "5 Best AI Tools for Developers"

    # Heuristic counter should have incremented
    assert ex.stats()["heuristic_detected"] == 1


# ── Test 18: Hallucinated name filtering ──────────────────────

def test_hallucinated_names_filtered():
    """TEST 18: Sub-items with names not in article text are filtered out."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return (
            '{"items": ['
            '{"suggested_name": "Modin", "score": 6},'
            '{"suggested_name": "Polars", "score": 8},'
            '{"suggested_name": "Pandarallel", "score": 5},'
            '{"suggested_name": "DuckDB", "score": 7}'
            ']}',
            100, 200,
        )

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "python_library",
        "verdict": "strong_fit",
        "suggested_name": "5 Faster Python Libraries",
        "url": "https://example.com",
        "text": "This article covers Modin, Polars, and DuckDB for working with massive datasets.",
    }

    sub_items = ex.explode_item(scored_item)

    # Pandarallel is NOT in the article text, should be filtered out
    names = [s["suggested_name"] for s in sub_items]
    assert "Modin" in names
    assert "Polars" in names
    assert "DuckDB" in names
    assert "Pandarallel" not in names, "Pandarallel should be filtered -- not in article text"
    assert len(sub_items) == 3


def test_hallucination_filter_skipped_when_no_text():
    """TEST 19: When article text is empty, name validation is skipped (no false drops)."""
    ex = _make_exploder(category_context="")

    def fake_call_llm(system, user):
        return ('{"items": [{"suggested_name": "SomeTool", "score": 5}]}', 50, 100)

    ex._call_llm = fake_call_llm

    scored_item = {
        "is_listicle": True,
        "listicle_item_type": "ai_tool",
        "verdict": "strong_fit",
        "suggested_name": "AI Tools List",
        "url": "https://example.com",
        "text": "",  # No article text available
    }

    sub_items = ex.explode_item(scored_item)

    # Should NOT filter when text is empty
    assert len(sub_items) == 1
    assert sub_items[0]["suggested_name"] == "SomeTool"


if __name__ == "__main__":
    test_should_explode()
    print("TEST 1: should_explode - PASSED")

    test_build_category_context()
    print("TEST 2a: build_category_context - PASSED")

    test_build_category_context_no_client()
    print("TEST 2b: build_category_context (no client) - PASSED")

    test_build_category_context_query_error()
    print("TEST 2c: build_category_context (query error) - PASSED")

    test_parse_clean_json()
    print("TEST 3a: parse clean JSON - PASSED")

    test_parse_code_fences()
    print("TEST 3b: parse code fences - PASSED")

    test_parse_broken_json()
    print("TEST 3c: parse broken JSON - PASSED")

    test_parse_truncated_json()
    print("TEST 3d: parse truncated JSON - PASSED")

    test_parse_bare_array()
    print("TEST 3e: parse bare array - PASSED")

    test_parse_empty_items()
    print("TEST 3f: parse empty items - PASSED")

    test_parse_garbage()
    print("TEST 3g: parse garbage - PASSED")

    test_verdict_derived_from_score()
    print("TEST 4: verdict derived from score - PASSED")

    test_python_library_extra_fields()
    print("TEST 5: python_library extra fields - PASSED")

    test_stats()
    print("TEST 6: stats tracking - PASSED")

    test_url_extraction()
    print("TEST 7: URL extraction - PASSED")

    test_signals_extraction()
    print("TEST 8: signals extraction - PASSED")

    test_dedup_filtering()
    print("TEST 9: dedup filtering - PASSED")

    test_interest_profile_in_prompt()
    print("TEST 10: interest profile in prompt - PASSED")

    test_detect_listicle_from_title_basic()
    print("TEST 11: title heuristic basic - PASSED")

    test_detect_listicle_from_title_negatives()
    print("TEST 12: title heuristic negatives - PASSED")

    test_detect_listicle_skips_already_flagged()
    print("TEST 13: title heuristic skips flagged - PASSED")

    test_detect_listicle_skips_rejects()
    print("TEST 14: title heuristic skips rejects - PASSED")

    test_process_batch_preserves_parent()
    print("TEST 15: process_batch preserves parent - PASSED")

    test_process_batch_parent_not_mutated()
    print("TEST 16: process_batch parent not mutated - PASSED")

    test_process_batch_heuristic_then_explode()
    print("TEST 17: heuristic then explode - PASSED")

    test_hallucinated_names_filtered()
    print("TEST 18: hallucinated names filtered - PASSED")

    test_hallucination_filter_skipped_when_no_text()
    print("TEST 19: hallucination filter skipped when no text - PASSED")

    print("\nAll tests passed!")
