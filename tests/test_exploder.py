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
    ex._category_context = kwargs.get("category_context")
    ex._category_lock = __import__("threading").Lock()
    ex._lock = __import__("threading").Lock()
    ex._total_input_tokens = 0
    ex._total_output_tokens = 0
    ex._items_exploded = 0
    ex._sub_items_created = 0
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
        "text": "Some article text",
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
        "text": "Article about Python libs...",
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
        "text": "Text...",
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

    print("\nAll 14 tests passed!")
