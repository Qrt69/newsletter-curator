"""
Tests for the DigestStore.

Tests run/item CRUD, batch inserts, action filtering,
user decision + feedback tracking, and summary stats.

Run: uv run python tests/test_digest.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.digest import DigestStore


# ── Helpers ─────────────────────────────────────────────────────

def _make_decision(**overrides) -> dict:
    """Build a minimal routing decision dict with overrides."""
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


def _make_email_meta(**overrides) -> dict:
    """Build a minimal email_meta dict."""
    base = {
        "email_id": "msg-123",
        "email_subject": "Weekly Python Digest",
        "email_sender": "digest@example.com",
    }
    base.update(overrides)
    return base


# ── Test 1: Create run + finish ─────────────────────────────────

def test_create_run_and_finish():
    """TEST 1: Create a run, finish with stats, verify fields."""
    print("=" * 60)
    print("TEST 1: Create run + finish")
    print("=" * 60)

    store = DigestStore(":memory:")

    # Create
    run_id = store.create_run(emails_fetched=5)
    assert run_id == 1, f"Expected run_id=1, got {run_id}"

    run = store.get_run(run_id)
    assert run is not None
    assert run["emails_fetched"] == 5
    assert run["status"] == "running"
    assert run["finished_at"] is None
    print(f"  Created run {run_id}: status={run['status']}, emails_fetched={run['emails_fetched']}")

    # Finish
    store.finish_run(run_id, {
        "items_extracted": 20,
        "items_scored": 18,
        "items_proposed": 12,
        "items_skipped": 6,
        "status": "completed",
    })

    run = store.get_run(run_id)
    assert run["status"] == "completed"
    assert run["finished_at"] is not None
    assert run["items_extracted"] == 20
    assert run["items_scored"] == 18
    assert run["items_proposed"] == 12
    assert run["items_skipped"] == 6
    print(f"  Finished run: status={run['status']}, extracted={run['items_extracted']}, proposed={run['items_proposed']}")

    # get_runs returns list
    runs = store.get_runs()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id

    print("PASS\n")


# ── Test 2: Add item + retrieve ──────────────────────────────────

def test_add_item_and_retrieve():
    """TEST 2: Add one item, retrieve it, verify all fields round-trip."""
    print("=" * 60)
    print("TEST 2: Add item + retrieve")
    print("=" * 60)

    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=1)

    decision = _make_decision()
    email_meta = _make_email_meta()
    item_id = store.add_item(run_id, decision, email_meta)
    assert item_id == 1, f"Expected item_id=1, got {item_id}"

    item = store.get_item(item_id)
    assert item is not None

    # Email context
    assert item["email_id"] == "msg-123"
    assert item["email_subject"] == "Weekly Python Digest"
    assert item["email_sender"] == "digest@example.com"

    # Content
    assert item["url"] == "https://example.com/article"
    assert item["link_text"] == "Example Article"
    assert item["title"] == "Example Title"
    assert item["author"] == "Test Author"
    assert len(item["text"]) <= 500  # truncated

    # Scorer output
    assert item["score"] == 5
    assert item["verdict"] == "strong_fit"
    assert item["item_type"] == "python_library"
    assert item["suggested_name"] == "ExampleLib"
    assert isinstance(item["signals"], list)
    assert len(item["signals"]) == 2
    assert isinstance(item["tags"], list)
    assert "python" in item["tags"]

    # Router output
    assert item["target_database"] == "Python Libraries"
    assert item["dedup_status"] == "new"
    assert isinstance(item["dedup_matches"], list)
    assert item["action"] == "propose"

    # Review state (not yet reviewed)
    assert item["user_decision"] is None
    assert item["decided_at"] is None
    assert item["notion_page_id"] is None

    print(f"  Item {item_id}: {item['suggested_name']} -> {item['target_database']}")
    print(f"  Signals: {item['signals']}")
    print(f"  Tags: {item['tags']}")
    print(f"  Text length: {len(item['text'])} chars (truncated)")

    print("PASS\n")


# ── Test 3: Add batch ────────────────────────────────────────────

def test_add_batch():
    """TEST 3: Add 3 items from same email, verify all stored."""
    print("=" * 60)
    print("TEST 3: Add batch")
    print("=" * 60)

    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=1)

    decisions = [
        _make_decision(url="https://example.com/1", suggested_name="Lib1"),
        _make_decision(url="https://example.com/2", suggested_name="Lib2", action="skip", verdict="reject"),
        _make_decision(url="https://example.com/3", suggested_name="Lib3"),
    ]
    email_meta = _make_email_meta()

    item_ids = store.add_batch(run_id, decisions, email_meta)
    assert len(item_ids) == 3, f"Expected 3 item_ids, got {len(item_ids)}"

    items = store.get_items(run_id)
    assert len(items) == 3
    assert items[0]["suggested_name"] == "Lib1"
    assert items[1]["suggested_name"] == "Lib2"
    assert items[2]["suggested_name"] == "Lib3"

    # All share the same email context
    for item in items:
        assert item["email_id"] == "msg-123"
        assert item["email_subject"] == "Weekly Python Digest"

    print(f"  Stored {len(item_ids)} items: {[i['suggested_name'] for i in items]}")

    print("PASS\n")


# ── Test 4: Filter by action ─────────────────────────────────────

def test_filter_by_action():
    """TEST 4: Add propose + skip items, filter by action."""
    print("=" * 60)
    print("TEST 4: Filter by action")
    print("=" * 60)

    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=1)

    decisions = [
        _make_decision(url="https://example.com/a", suggested_name="ProposeA", action="propose"),
        _make_decision(url="https://example.com/b", suggested_name="SkipB", action="skip", verdict="reject"),
        _make_decision(url="https://example.com/c", suggested_name="ProposeC", action="propose"),
        _make_decision(url="https://example.com/d", suggested_name="ReviewD", action="review"),
    ]
    store.add_batch(run_id, decisions)

    # All items
    all_items = store.get_items(run_id)
    assert len(all_items) == 4

    # Only propose
    proposed = store.get_items(run_id, action_filter="propose")
    assert len(proposed) == 2
    assert all(i["action"] == "propose" for i in proposed)
    print(f"  Proposed: {[i['suggested_name'] for i in proposed]}")

    # Only skip
    skipped = store.get_items(run_id, action_filter="skip")
    assert len(skipped) == 1
    assert skipped[0]["suggested_name"] == "SkipB"
    print(f"  Skipped: {[i['suggested_name'] for i in skipped]}")

    # Only review
    review = store.get_items(run_id, action_filter="review")
    assert len(review) == 1
    assert review[0]["suggested_name"] == "ReviewD"
    print(f"  Review: {[i['suggested_name'] for i in review]}")

    print("PASS\n")


# ── Test 5: Set decision + feedback ───────────────────────────────

def test_set_decision_and_feedback():
    """TEST 5: Accept an item, verify user_decision updated + feedback row created."""
    print("=" * 60)
    print("TEST 5: Set decision + feedback")
    print("=" * 60)

    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=1)

    decision = _make_decision()
    item_id = store.add_item(run_id, decision, _make_email_meta())

    # Before decision
    assert store.get_pending_count(run_id) == 1

    # Accept the item
    store.set_decision(item_id, "accepted", reason="Looks great")

    # Verify item updated
    item = store.get_item(item_id)
    assert item["user_decision"] == "accepted"
    assert item["decided_at"] is not None
    print(f"  Item decision: {item['user_decision']} at {item['decided_at']}")

    # Verify pending count dropped
    assert store.get_pending_count(run_id) == 0

    # Verify feedback row created
    feedback = store.get_feedback()
    assert len(feedback) == 1
    fb = feedback[0]
    assert fb["item_id"] == item_id
    assert fb["user_decision"] == "accepted"
    assert fb["verdict"] == "strong_fit"
    assert fb["score"] == 5
    assert fb["suggested_name"] == "ExampleLib"
    assert fb["url"] == "https://example.com/article"
    assert fb["reason"] == "Looks great"
    print(f"  Feedback: decision={fb['user_decision']}, verdict={fb['verdict']}, score={fb['score']}")

    # Reject another item
    item_id2 = store.add_item(run_id, _make_decision(suggested_name="RejectMe"))
    store.set_decision(item_id2, "rejected", reason="Not relevant")

    feedback = store.get_feedback()
    assert len(feedback) == 2
    assert feedback[0]["user_decision"] == "rejected"  # newest first
    assert feedback[1]["user_decision"] == "accepted"
    print(f"  Total feedback entries: {len(feedback)}")

    print("PASS\n")


# ── Test 6: Stats ─────────────────────────────────────────────────

def test_stats():
    """TEST 6: Create run with items, verify summary stats."""
    print("=" * 60)
    print("TEST 6: Stats")
    print("=" * 60)

    store = DigestStore(":memory:")

    # Run 1: 3 items (2 propose, 1 skip)
    run1 = store.create_run(emails_fetched=2)
    store.add_item(run1, _make_decision(action="propose", suggested_name="A"))
    store.add_item(run1, _make_decision(action="propose", suggested_name="B"))
    store.add_item(run1, _make_decision(action="skip", suggested_name="C", verdict="reject"))
    store.finish_run(run1, {"items_extracted": 3, "items_scored": 3, "items_proposed": 2, "items_skipped": 1})

    # Run 2: 2 items (1 propose, 1 skip)
    run2 = store.create_run(emails_fetched=1)
    item_id = store.add_item(run2, _make_decision(action="propose", suggested_name="D"))
    store.add_item(run2, _make_decision(action="skip", suggested_name="E", verdict="reject"))

    # Accept one item
    store.set_decision(item_id, "accepted")

    s = store.stats()
    print(f"  Stats: {s}")

    assert s["total_runs"] == 2
    assert s["total_items"] == 5
    assert s["proposed"] == 3
    assert s["skipped"] == 2
    assert s["reviewed"] == 1
    assert s["accepted"] == 1
    assert s["rejected"] == 0
    assert s["feedback_entries"] == 1

    print("PASS\n")


# ── Main ──────────────────────────────────────────────────────────

def main():
    test_create_run_and_finish()
    test_add_item_and_retrieve()
    test_add_batch()
    test_filter_by_action()
    test_set_decision_and_feedback()
    test_stats()

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
