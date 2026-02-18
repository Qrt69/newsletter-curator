"""
Integration tests for the Scorer.

Tests JSON parsing, scoring of different item types, no-text fallback,
and batch scoring with token usage tracking.

Supports both backends via SCORER_BACKEND env var (default: "local").

Run: uv run python tests/test_scorer.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


from src.intelligence.scorer import Scorer

# Read backend from env (tests follow the configured backend)
_BACKEND = os.environ.get("SCORER_BACKEND", "local")


# ── Test 1: Parse response (unit test, no API call) ───────────

def test_parse_response():
    """TEST 1: JSON parsing, code fence stripping, verdict correction, defaults."""
    print("=" * 60)
    print("TEST 1: Parse response (unit test)")
    print("=" * 60)

    # Clean JSON
    raw = '{"score": 7, "verdict": "strong_fit", "item_type": "python_library", "reasoning": "Great lib", "signals": ["+3 Python"], "suggested_name": "Pydantic", "suggested_category": "Data Validation", "tags": ["python", "validation"]}'
    result = Scorer._parse_response(raw)
    assert result["score"] == 7
    assert result["verdict"] == "strong_fit"
    assert result["item_type"] == "python_library"
    assert len(result["tags"]) == 2
    print("  Clean JSON: OK")

    # With code fences
    fenced = '```json\n{"score": 5, "verdict": "strong_fit", "item_type": "ai_tool", "reasoning": "Nice tool"}\n```'
    result = Scorer._parse_response(fenced)
    assert result["score"] == 5
    assert result["verdict"] == "strong_fit"
    print("  Code fences: OK")

    # Invalid verdict -> corrected from score
    bad_verdict = '{"score": 4, "verdict": "excellent", "item_type": "article", "reasoning": "test"}'
    result = Scorer._parse_response(bad_verdict)
    assert result["verdict"] == "likely_fit", f"Expected 'likely_fit', got '{result['verdict']}'"
    print("  Verdict correction (score 4 -> likely_fit): OK")

    # Negative score -> reject
    neg = '{"score": -2, "verdict": "bad", "item_type": "article", "reasoning": "not relevant"}'
    result = Scorer._parse_response(neg)
    assert result["verdict"] == "reject"
    assert result["score"] == -2
    print("  Negative score -> reject: OK")

    # Invalid item_type -> defaults to article
    bad_type = '{"score": 3, "verdict": "likely_fit", "item_type": "unknown_thing", "reasoning": "test"}'
    result = Scorer._parse_response(bad_type)
    assert result["item_type"] == "article"
    print("  Invalid item_type -> article: OK")

    # Missing fields -> defaults
    minimal = '{"score": 1}'
    result = Scorer._parse_response(minimal)
    assert result["verdict"] == "maybe"
    assert result["signals"] == []
    assert result["tags"] == []
    assert result["suggested_name"] == ""
    print("  Missing fields -> defaults: OK")

    # Trailing commas (common local LLM issue)
    trailing = '{"score": 5, "verdict": "strong_fit", "item_type": "ai_tool", "tags": ["a", "b",],}'
    result = Scorer._parse_response(trailing)
    assert result["score"] == 5
    assert result["tags"] == ["a", "b"]
    print("  Trailing commas: OK")

    # Extra text around JSON
    wrapped = 'Here is my analysis:\n\n{"score": 3, "verdict": "likely_fit", "item_type": "article", "reasoning": "good"}\n\nI hope this helps!'
    result = Scorer._parse_response(wrapped)
    assert result["score"] == 3
    assert result["verdict"] == "likely_fit"
    print("  Extra text around JSON: OK")

    # Code fences with extra text before
    prefixed = 'Sure, here is the JSON:\n```json\n{"score": 2, "item_type": "concept_pattern"}\n```'
    result = Scorer._parse_response(prefixed)
    assert result["score"] == 2
    assert result["verdict"] == "maybe"
    print("  Code fences with prefix text: OK")

    print("PASS\n")


# ── Test 2: Score a Python library (API call) ─────────────────

def test_score_python_library():
    """TEST 2: Score Pydantic - expect strong_fit or likely_fit, score >= 3."""
    print("=" * 60)
    print(f"TEST 2: Score Python library (Pydantic) [backend={_BACKEND}]")
    print("=" * 60)

    scorer = Scorer(backend=_BACKEND)
    item = {
        "resolved_url": "https://github.com/pydantic/pydantic",
        "link_text": "Pydantic v2.10 - Data validation using Python type hints",
        "title": "Pydantic - Data validation using Python type annotations",
        "author": "Samuel Colvin",
        "sitename": "GitHub",
        "hostname": "github.com",
        "description": "Data validation using Python type annotations.",
        "text": (
            "Pydantic is the most widely used data validation library for Python. "
            "It uses Python type hints to validate data, serialize it, and generate "
            "JSON Schema. It's fast, extensible, and works with all major Python "
            "frameworks. Version 2 is a complete rewrite with a Rust core for "
            "performance. Key features: validation of complex data types, "
            "custom validators, JSON schema generation, integration with FastAPI, "
            "SQLModel, and other libraries."
        ),
        "extraction_status": "ok",
    }

    result = scorer.score_item(item)
    print(f"  Score:    {result['score']}")
    print(f"  Verdict:  {result['verdict']}")
    print(f"  Type:     {result['item_type']}")
    print(f"  Reason:   {result['reasoning']}")
    print(f"  Signals:  {result['signals']}")
    print(f"  Name:     {result['suggested_name']}")
    print(f"  Category: {result['suggested_category']}")
    print(f"  Tags:     {result['tags']}")

    assert result["score"] >= 3, f"Expected score >= 3, got {result['score']}"
    assert result["verdict"] in ("strong_fit", "likely_fit"), (
        f"Expected strong_fit or likely_fit, got '{result['verdict']}'"
    )
    assert result["item_type"] == "python_library", (
        f"Expected 'python_library', got '{result['item_type']}'"
    )

    print("PASS\n")


# ── Test 3: Score a frontend framework (should reject) ────────

def test_score_frontend_framework():
    """TEST 3: Score React - expect reject, score <= 0."""
    print("=" * 60)
    print(f"TEST 3: Score frontend framework (React) [backend={_BACKEND}]")
    print("=" * 60)

    scorer = Scorer(backend=_BACKEND)
    item = {
        "resolved_url": "https://react.dev/blog/2025/02/react-19",
        "link_text": "React 19 is here - New hooks and server components",
        "title": "React 19 Release",
        "author": "React Team",
        "sitename": "react.dev",
        "hostname": "react.dev",
        "description": "React 19 introduces new hooks and server components.",
        "text": (
            "React 19 introduces several new features including server components, "
            "new hooks like useFormStatus and useOptimistic, and improved "
            "concurrent rendering. The new compiler automatically optimizes "
            "re-renders. Server Actions allow calling server-side functions "
            "directly from components."
        ),
        "extraction_status": "ok",
    }

    result = scorer.score_item(item)
    print(f"  Score:    {result['score']}")
    print(f"  Verdict:  {result['verdict']}")
    print(f"  Type:     {result['item_type']}")
    print(f"  Reason:   {result['reasoning']}")
    print(f"  Signals:  {result['signals']}")

    assert result["score"] <= 0, f"Expected score <= 0, got {result['score']}"
    assert result["verdict"] == "reject", (
        f"Expected 'reject', got '{result['verdict']}'"
    )

    print("PASS\n")


# ── Test 4: Score item with no extracted text ─────────────────

def test_score_no_text():
    """TEST 4: Score a RAG tool with no text - should still produce a result."""
    print("=" * 60)
    print(f"TEST 4: Score item with no extracted text [backend={_BACKEND}]")
    print("=" * 60)

    scorer = Scorer(backend=_BACKEND)
    item = {
        "resolved_url": "https://github.com/cognee-ai/cognee",
        "link_text": "Cognee - Build and manage RAG knowledge graphs with Python",
        "title": None,
        "author": None,
        "sitename": None,
        "hostname": "github.com",
        "description": None,
        "text": None,
        "extraction_status": "fetch_failed",
    }

    result = scorer.score_item(item)
    print(f"  Score:    {result['score']}")
    print(f"  Verdict:  {result['verdict']}")
    print(f"  Type:     {result['item_type']}")
    print(f"  Reason:   {result['reasoning']}")

    assert result["verdict"] != "error", (
        f"Should produce a real verdict, not 'error': {result['reasoning']}"
    )
    assert result["score"] is not None

    print("PASS\n")


# ── Test 5: Score batch + verify stats ────────────────────────

def test_score_batch():
    """TEST 5: Score 3 items in batch, verify stats."""
    print("=" * 60)
    print(f"TEST 5: Score batch (3 items) [backend={_BACKEND}]")
    print("=" * 60)

    scorer = Scorer(backend=_BACKEND)
    items = [
        {
            "resolved_url": "https://github.com/duckdb/duckdb",
            "link_text": "DuckDB 1.2 - New JSON and spatial extensions",
            "title": "DuckDB",
            "hostname": "github.com",
            "text": "DuckDB is an analytical database with new extensions.",
            "extraction_status": "ok",
        },
        {
            "resolved_url": "https://example.com/ai-art-fun",
            "link_text": "Make fun AI art with your friends",
            "title": "AI Art Party",
            "hostname": "example.com",
            "text": "Create fun images with AI for social media.",
            "extraction_status": "ok",
        },
        {
            "resolved_url": "https://github.com/vllm-project/vllm",
            "link_text": "vLLM - High throughput LLM serving",
            "title": "vLLM",
            "hostname": "github.com",
            "text": "vLLM is a high-throughput serving engine for LLMs.",
            "extraction_status": "ok",
        },
    ]

    results = scorer.score_batch(items)
    assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    stats = scorer.stats()
    print(f"\n  Stats:")
    for key, val in stats.items():
        print(f"    {key}: {val}")

    assert stats["backend"] == _BACKEND
    assert stats["model"], "Model should be set"
    assert stats["items_scored"] == 3, f"Expected 3 items scored, got {stats['items_scored']}"
    assert stats["total_tokens"] == stats["total_input_tokens"] + stats["total_output_tokens"]

    print("PASS\n")


# ── Main ──────────────────────────────────────────────────────

def main():
    print(f"Running scorer tests with backend={_BACKEND}\n")

    # Unit test (no API call)
    test_parse_response()

    # Integration tests (require LM Studio running or ANTHROPIC_API_KEY)
    test_score_python_library()
    test_score_frontend_framework()
    test_score_no_text()
    test_score_batch()

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
