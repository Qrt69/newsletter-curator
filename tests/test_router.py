"""
Tests for the Router.

Tests routing table mapping, dedup integration, action logic,
batch processing, and summary stats.

Run: uv run python tests/test_router.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.intelligence.router import Router, ROUTING_TABLE
from src.notion.dedup import DedupIndex
from src.notion.client import NotionClient, DATABASES


# ── Helper: mock DedupIndex with empty entries ────────────────

class EmptyDedupIndex:
    """Fake DedupIndex that always returns no matches."""

    def search(self, name=None, url=None, threshold=80):
        return []


# ── Test 1: Route all item types (unit test) ─────────────────

def test_route_all_item_types():
    """TEST 1: All 13 item_types map to the correct database."""
    print("=" * 60)
    print("TEST 1: Route all item types (unit test)")
    print("=" * 60)

    router = Router(EmptyDedupIndex())

    expected = {
        "python_library": "Python Libraries",
        "duckdb_extension": "DuckDB Extensions",
        "ai_tool": "TAAFT",
        "agent_workflow": "Overview",
        "model_release": "Model information",
        "platform_infra": "Platforms & Infrastructure",
        "concept_pattern": "Topics & Concepts",
        "article": "Articles & Reads",
        "book_paper": "Books & Papers",
        "coding_tool": "AI Agents & Coding Tools",
        "vibe_coding_tool": "Vibe Coding Tools",
        "ai_architecture": "AI Architecture Topics",
        "infra_reference": "Infrastructure Knowledge Base",
    }

    for item_type, expected_db in expected.items():
        scored = {
            "score": 5,
            "verdict": "strong_fit",
            "item_type": item_type,
            "reasoning": "test",
            "signals": [],
            "suggested_name": f"Test {item_type}",
            "suggested_category": "Test",
            "tags": [],
            "url": f"https://example.com/{item_type}",
            "link_text": f"Test {item_type}",
        }
        decision = router.route_item(scored)
        assert decision["target_database"] == expected_db, (
            f"{item_type}: expected '{expected_db}', got '{decision['target_database']}'"
        )
        assert decision["action"] == "propose"
        print(f"  {item_type} -> {expected_db}: OK")

    # Verify all 13 databases are covered
    routed_dbs = set(expected.values())
    all_dbs = set(DATABASES.keys())
    unrouted = all_dbs - routed_dbs - {"Notes & Insights"}
    assert not unrouted, f"Unrouted databases: {unrouted}"
    print(f"  All 13 databases covered (Notes & Insights excluded): OK")

    print("PASS\n")


# ── Test 2: Route new item ───────────────────────────────────

def test_route_new_item():
    """TEST 2: Item with unique name/URL -> new, propose."""
    print("=" * 60)
    print("TEST 2: Route new item")
    print("=" * 60)

    nc = NotionClient()
    dedup = DedupIndex(nc)
    dedup.load()

    router = Router(dedup)
    scored = {
        "score": 6,
        "verdict": "strong_fit",
        "item_type": "python_library",
        "reasoning": "Unique test library",
        "signals": ["+3 Python library"],
        "suggested_name": "ZzzzTestLibraryThatDoesNotExist99999",
        "suggested_category": "Testing",
        "tags": ["python", "test"],
        "url": "https://example.com/zzz-nonexistent-library-99999",
        "link_text": "ZzzzTestLibrary",
    }

    decision = router.route_item(scored)
    print(f"  target_database: {decision['target_database']}")
    print(f"  dedup_status:    {decision['dedup_status']}")
    print(f"  action:          {decision['action']}")
    print(f"  dedup_matches:   {len(decision['dedup_matches'])}")

    assert decision["target_database"] == "Python Libraries"
    assert decision["dedup_status"] == "new"
    assert decision["action"] == "propose"
    assert decision["dedup_matches"] == []

    print("PASS\n")


# ── Test 3: Route rejected item ──────────────────────────────

def test_route_rejected_item():
    """TEST 3: verdict=reject -> action=skip regardless of dedup."""
    print("=" * 60)
    print("TEST 3: Route rejected item")
    print("=" * 60)

    router = Router(EmptyDedupIndex())
    scored = {
        "score": -2,
        "verdict": "reject",
        "item_type": "article",
        "reasoning": "Not relevant",
        "signals": ["-3 frontend framework"],
        "suggested_name": "React 19",
        "suggested_category": "Frontend",
        "tags": ["react"],
        "url": "https://react.dev",
        "link_text": "React 19",
    }

    decision = router.route_item(scored)
    print(f"  verdict: {decision['verdict']}")
    print(f"  action:  {decision['action']}")

    assert decision["action"] == "skip"
    assert decision["verdict"] == "reject"

    print("PASS\n")


# ── Test 4: Route duplicate ──────────────────────────────────

def test_route_duplicate():
    """TEST 4: Item matching an existing dedup entry -> duplicate, skip."""
    print("=" * 60)
    print("TEST 4: Route duplicate")
    print("=" * 60)

    nc = NotionClient()
    dedup = DedupIndex(nc)
    dedup.load()

    # Pick an entry we know exists in the index
    stats = dedup.stats()
    print(f"  Dedup index: {stats['total']} entries")

    # Find an actual entry from the Python Libraries database
    test_entry = None
    for entry in dedup._entries:
        if entry["database"] == "Python Libraries" and entry["name"]:
            test_entry = entry
            break

    if test_entry is None:
        print("  SKIP: No Python Libraries entries in dedup index")
        print("PASS (skipped)\n")
        return

    print(f"  Using existing entry: '{test_entry['name']}' in {test_entry['database']}")

    router = Router(dedup)
    scored = {
        "score": 5,
        "verdict": "strong_fit",
        "item_type": "python_library",
        "reasoning": "Test duplicate",
        "signals": [],
        "suggested_name": test_entry["name"],
        "suggested_category": "Test",
        "tags": [],
        "url": test_entry.get("url") or "",
        "link_text": test_entry["name"],
    }

    decision = router.route_item(scored)
    print(f"  dedup_status:  {decision['dedup_status']}")
    print(f"  action:        {decision['action']}")
    print(f"  dedup_matches: {len(decision['dedup_matches'])} match(es)")

    assert decision["dedup_status"] == "duplicate", (
        f"Expected 'duplicate', got '{decision['dedup_status']}'"
    )
    assert decision["action"] == "skip"
    assert len(decision["dedup_matches"]) > 0

    print("PASS\n")


# ── Test 5: Route batch + summary ────────────────────────────

def test_route_batch_summary():
    """TEST 5: Route 3 items, verify summary counts."""
    print("=" * 60)
    print("TEST 5: Route batch + summary")
    print("=" * 60)

    router = Router(EmptyDedupIndex())
    scored_items = [
        {
            "score": 6,
            "verdict": "strong_fit",
            "item_type": "python_library",
            "reasoning": "Great Python lib",
            "signals": ["+3 Python"],
            "suggested_name": "TestLib1",
            "suggested_category": "Libraries",
            "tags": ["python"],
            "url": "https://example.com/lib1",
            "link_text": "TestLib1",
        },
        {
            "score": -1,
            "verdict": "reject",
            "item_type": "article",
            "reasoning": "Not relevant",
            "signals": ["-3 basic content"],
            "suggested_name": "Basic Intro",
            "suggested_category": "General",
            "tags": [],
            "url": "https://example.com/basic",
            "link_text": "Basic Intro",
        },
        {
            "score": 4,
            "verdict": "likely_fit",
            "item_type": "ai_tool",
            "reasoning": "Useful AI tool",
            "signals": ["+3 AI tool"],
            "suggested_name": "TestTool",
            "suggested_category": "AI",
            "tags": ["ai"],
            "url": "https://example.com/tool",
            "link_text": "TestTool",
        },
    ]

    decisions = router.route_batch(scored_items)
    assert len(decisions) == 3, f"Expected 3 decisions, got {len(decisions)}"

    summary = Router.summary(decisions)
    print(f"\n  Summary:")
    print(f"    total:          {summary['total']}")
    print(f"    by_action:      {summary['by_action']}")
    print(f"    by_database:    {summary['by_database']}")
    print(f"    by_dedup_status: {summary['by_dedup_status']}")

    assert summary["total"] == 3
    assert summary["by_action"].get("propose", 0) == 2
    assert summary["by_action"].get("skip", 0) == 1
    assert summary["by_database"].get("Python Libraries", 0) == 1
    assert summary["by_database"].get("Articles & Reads", 0) == 1
    assert summary["by_database"].get("TAAFT", 0) == 1
    assert summary["by_dedup_status"].get("new", 0) == 3

    print("PASS\n")


# ── Main ──────────────────────────────────────────────────────

def main():
    # Unit test (no API calls)
    test_route_all_item_types()

    # Integration tests (require NOTION_API_KEY for dedup cache)
    test_route_new_item()
    test_route_rejected_item()
    test_route_duplicate()
    test_route_batch_summary()

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
