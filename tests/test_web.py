"""
Tests for the web app state logic and DigestStore.update_item_fields().

Tests state behavior without a browser -- instantiate DigestStore,
populate with test data, call state-like operations, verify results.

Run: uv run python tests/test_web.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.digest import DigestStore
from src.web.state import DATABASE_OPTIONS


# ── Helpers ─────────────────────────────────────────────────────

def _make_decision(**overrides) -> dict:
    """Build a minimal routing decision dict."""
    base = {
        "url": "https://example.com/article",
        "link_text": "Example Article",
        "title": "Example Title",
        "author": "Test Author",
        "text": "Some article text " * 50,
        "score": 5,
        "verdict": "strong_fit",
        "item_type": "python_library",
        "reasoning": "Great Python library",
        "signals": ["+3 Python library", "+2 practical tooling"],
        "suggested_name": "ExampleLib",
        "suggested_category": "Libraries",
        "tags": ["python", "testing"],
        "target_database": "Python Libraries",
        "dedup_status": "new",
        "dedup_matches": [],
        "action": "propose",
    }
    base.update(overrides)
    return base


def _seed_store() -> tuple[DigestStore, int, list[int]]:
    """Create an in-memory store with one run and several items."""
    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=3)

    item_ids = []
    item_ids.append(store.add_item(run_id, _make_decision(
        url="https://example.com/1",
        suggested_name="FastLib",
        score=6,
        verdict="strong_fit",
        action="propose",
    )))
    item_ids.append(store.add_item(run_id, _make_decision(
        url="https://example.com/2",
        suggested_name="DataTool",
        score=4,
        verdict="likely_fit",
        item_type="ai_tool",
        target_database="TAAFT",
        action="propose",
    )))
    item_ids.append(store.add_item(run_id, _make_decision(
        url="https://example.com/3",
        suggested_name="OldArticle",
        score=1,
        verdict="maybe",
        item_type="article",
        target_database="Articles & Reads",
        action="propose",
    )))
    item_ids.append(store.add_item(run_id, _make_decision(
        url="https://example.com/4",
        suggested_name="SkippedOne",
        score=-1,
        verdict="reject",
        action="skip",
    )))

    store.finish_run(run_id, {
        "items_extracted": 4,
        "items_scored": 4,
        "items_proposed": 3,
        "items_skipped": 1,
        "status": "completed",
    })

    return store, run_id, item_ids


# ── Test 1: update_item_fields ─────────────────────────────────

def test_update_item_fields():
    """TEST 1: Verify update_item_fields persists edits."""
    print("=" * 60)
    print("TEST 1: update_item_fields")
    print("=" * 60)

    store, run_id, item_ids = _seed_store()
    item_id = item_ids[0]

    # Edit name, category, database, tags
    store.update_item_fields(item_id, {
        "suggested_name": "SuperFastLib",
        "suggested_category": "Performance",
        "target_database": "AI Agents & Coding Tools",
        "tags": ["python", "performance", "fast"],
    })

    item = store.get_item(item_id)
    assert item["suggested_name"] == "SuperFastLib", f"Got {item['suggested_name']}"
    assert item["suggested_category"] == "Performance"
    assert item["target_database"] == "AI Agents & Coding Tools"
    assert item["tags"] == ["python", "performance", "fast"]
    print(f"  Updated: name={item['suggested_name']}, db={item['target_database']}")

    # Disallowed fields are ignored
    store.update_item_fields(item_id, {
        "score": 99,
        "verdict": "hacked",
        "action": "evil",
    })
    item = store.get_item(item_id)
    assert item["score"] == 6, "Score should not have changed"
    assert item["verdict"] == "strong_fit", "Verdict should not have changed"
    assert item["action"] == "propose", "Action should not have changed"
    print("  Disallowed fields correctly ignored")

    # Empty update is no-op
    store.update_item_fields(item_id, {})
    item = store.get_item(item_id)
    assert item["suggested_name"] == "SuperFastLib"
    print("  Empty update is no-op")

    print("PASS\n")


# ── Test 2: Accept workflow ────────────────────────────────────

def test_accept_workflow():
    """TEST 2: Edit fields, then accept -- simulates the UI flow."""
    print("=" * 60)
    print("TEST 2: Accept workflow (edit + accept)")
    print("=" * 60)

    store, run_id, item_ids = _seed_store()
    item_id = item_ids[0]

    # Simulate: user edits in the dialog
    store.update_item_fields(item_id, {
        "suggested_name": "RenamedLib",
        "target_database": "TAAFT",
        "tags": ["renamed"],
    })

    # Then accepts
    store.set_decision(item_id, "accepted")

    item = store.get_item(item_id)
    assert item["user_decision"] == "accepted"
    assert item["suggested_name"] == "RenamedLib"
    assert item["target_database"] == "TAAFT"
    assert item["decided_at"] is not None
    print(f"  Accepted: name={item['suggested_name']}, db={item['target_database']}")

    # Feedback row records the state at time of acceptance
    feedback = store.get_feedback(limit=1)
    assert len(feedback) == 1
    assert feedback[0]["user_decision"] == "accepted"
    assert feedback[0]["suggested_name"] == "RenamedLib"
    print(f"  Feedback recorded: name={feedback[0]['suggested_name']}")

    # Pending count decreased
    pending = store.get_pending_count(run_id)
    assert pending == 2, f"Expected 2 pending, got {pending}"
    print(f"  Pending count: {pending}")

    print("PASS\n")


# ── Test 3: Reject workflow ───────────────────────────────────

def test_reject_workflow():
    """TEST 3: Quick reject from table."""
    print("=" * 60)
    print("TEST 3: Reject workflow")
    print("=" * 60)

    store, run_id, item_ids = _seed_store()
    item_id = item_ids[2]  # OldArticle

    store.set_decision(item_id, "rejected")

    item = store.get_item(item_id)
    assert item["user_decision"] == "rejected"
    assert item["decided_at"] is not None
    print(f"  Rejected: {item['suggested_name']}")

    pending = store.get_pending_count(run_id)
    assert pending == 2, f"Expected 2 pending, got {pending}"
    print(f"  Pending count: {pending}")

    print("PASS\n")


# ── Test 4: DATABASE_OPTIONS populated ─────────────────────────

def test_database_options():
    """TEST 4: Verify DATABASE_OPTIONS is populated from ROUTING_TABLE."""
    print("=" * 60)
    print("TEST 4: DATABASE_OPTIONS")
    print("=" * 60)

    assert len(DATABASE_OPTIONS) > 0, "DATABASE_OPTIONS should not be empty"
    assert "Python Libraries" in DATABASE_OPTIONS
    assert "TAAFT" in DATABASE_OPTIONS
    assert "Articles & Reads" in DATABASE_OPTIONS
    print(f"  {len(DATABASE_OPTIONS)} database options available")
    print(f"  Sample: {DATABASE_OPTIONS[:5]}")

    print("PASS\n")


# ── Test 5: Empty database ────────────────────────────────────

def test_empty_database():
    """TEST 5: Operations on empty database don't crash."""
    print("=" * 60)
    print("TEST 5: Empty database")
    print("=" * 60)

    store = DigestStore(":memory:")

    runs = store.get_runs()
    assert runs == [], "Empty store should have no runs"
    print("  No runs: OK")

    stats = store.stats()
    assert stats["total_runs"] == 0
    assert stats["total_items"] == 0
    print(f"  Stats: {stats}")

    print("PASS\n")


# ── Test 6: Multiple runs ─────────────────────────────────────

def test_multiple_runs():
    """TEST 6: Two runs, items isolated by run_id."""
    print("=" * 60)
    print("TEST 6: Multiple runs")
    print("=" * 60)

    store = DigestStore(":memory:")

    run1 = store.create_run(emails_fetched=2)
    store.add_item(run1, _make_decision(suggested_name="Run1Item"))

    run2 = store.create_run(emails_fetched=1)
    store.add_item(run2, _make_decision(suggested_name="Run2Item"))

    items1 = store.get_items(run1)
    items2 = store.get_items(run2)
    assert len(items1) == 1 and items1[0]["suggested_name"] == "Run1Item"
    assert len(items2) == 1 and items2[0]["suggested_name"] == "Run2Item"
    print(f"  Run 1 items: {[i['suggested_name'] for i in items1]}")
    print(f"  Run 2 items: {[i['suggested_name'] for i in items2]}")

    runs = store.get_runs()
    assert len(runs) == 2
    assert runs[0]["id"] == run2, "Newest run should be first"
    print(f"  Runs ordered newest first: IDs = {[r['id'] for r in runs]}")

    print("PASS\n")


# ── Main ──────────────────────────────────────────────────────

def main():
    test_update_item_fields()
    test_accept_workflow()
    test_reject_workflow()
    test_database_options()
    test_empty_database()
    test_multiple_runs()

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
