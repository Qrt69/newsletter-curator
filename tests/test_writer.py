"""
Tests for the NotionWriter.

Tests 1-4 are unit tests (in-memory DB, no API calls).
Tests 5-6 are integration tests (require NOTION_API_KEY, create real entries).

Run: uv run python tests/test_writer.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.digest import DigestStore
from src.notion.writer import PROPERTY_MAP


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
        "description": "A Python library for testing purposes.",
        "reasoning": "Great Python library for testing",
        "signals": ["+3 Python library", "+2 practical tooling"],
        "suggested_name": "ExampleLib",
        "suggested_category": "Testing",
        "tags": ["python", "testing"],
        "target_database": "Python Libraries",
        "dedup_status": "new",
        "dedup_matches": [],
        "action": "propose",
    }
    base.update(overrides)
    return base


def _make_item(store, run_id, **overrides) -> dict:
    """Add a decision to the store and return the full item dict."""
    decision = _make_decision(**overrides)
    item_id = store.add_item(run_id, decision)
    store.set_decision(item_id, "accepted")
    return store.get_item(item_id)


# ── Test 1: get_accepted_items ────────────────────────────────────

def test_get_accepted_items():
    """TEST 1: Only accepted items with no notion_page_id are returned."""
    print("=" * 60)
    print("TEST 1: get_accepted_items")
    print("=" * 60)

    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=1)

    # Item 1: accepted, no page_id -> should be returned
    id1 = store.add_item(run_id, _make_decision(suggested_name="Accepted1"))
    store.set_decision(id1, "accepted")

    # Item 2: rejected -> should NOT be returned
    id2 = store.add_item(run_id, _make_decision(suggested_name="Rejected1"))
    store.set_decision(id2, "rejected")

    # Item 3: accepted, no page_id -> should be returned
    id3 = store.add_item(run_id, _make_decision(suggested_name="Accepted2"))
    store.set_decision(id3, "accepted")

    # Item 4: not yet reviewed -> should NOT be returned
    store.add_item(run_id, _make_decision(suggested_name="Pending1"))

    items = store.get_accepted_items(run_id)
    assert len(items) == 2, f"Expected 2 accepted items, got {len(items)}"
    assert items[0]["suggested_name"] == "Accepted1"
    assert items[1]["suggested_name"] == "Accepted2"
    print(f"  Found {len(items)} accepted items: {[i['suggested_name'] for i in items]}")

    print("PASS\n")


# ── Test 2: set_notion_page_id ──────────────────────────────────

def test_set_notion_page_id():
    """TEST 2: After setting page_id, item is excluded from get_accepted_items."""
    print("=" * 60)
    print("TEST 2: set_notion_page_id")
    print("=" * 60)

    store = DigestStore(":memory:")
    run_id = store.create_run(emails_fetched=1)

    id1 = store.add_item(run_id, _make_decision(suggested_name="Item1"))
    store.set_decision(id1, "accepted")
    id2 = store.add_item(run_id, _make_decision(suggested_name="Item2"))
    store.set_decision(id2, "accepted")

    # Before: both returned
    assert len(store.get_accepted_items(run_id)) == 2

    # Set page_id on first
    store.set_notion_page_id(id1, "fake-page-id-123")

    # After: only second returned
    remaining = store.get_accepted_items(run_id)
    assert len(remaining) == 1, f"Expected 1 remaining, got {len(remaining)}"
    assert remaining[0]["suggested_name"] == "Item2"

    # Verify page_id stored
    item = store.get_item(id1)
    assert item["notion_page_id"] == "fake-page-id-123"
    print(f"  Item1 page_id: {item['notion_page_id']}")
    print(f"  Remaining unwritten: {[i['suggested_name'] for i in remaining]}")

    print("PASS\n")


# ── Test 3: PROPERTY_MAP covers all databases ─────────────────────

def test_property_map_all_databases():
    """TEST 3: Every PROPERTY_MAP entry produces a valid dict."""
    print("=" * 60)
    print("TEST 3: PROPERTY_MAP covers all databases")
    print("=" * 60)

    from src.intelligence.router import ROUTING_TABLE

    item = {
        "suggested_name": "Test Item",
        "url": "https://example.com/test",
        "suggested_category": "Testing",
        "tags": ["test", "demo"],
        "description": "A test item for validation.",
        "item_type": "python_library",
        "reasoning": "Good for testing.",
        "author": "Test Author",
        "email_sender": "test@example.com",
    }

    # Every database in ROUTING_TABLE should have a PROPERTY_MAP entry
    for item_type, db_name in ROUTING_TABLE.items():
        assert db_name in PROPERTY_MAP, f"Missing PROPERTY_MAP entry for {db_name}"

        builder = PROPERTY_MAP[db_name]
        props = builder(item)

        assert isinstance(props, dict), f"Expected dict for {db_name}, got {type(props)}"
        assert len(props) > 0, f"Empty props for {db_name}"

        # Every entry should have a title field
        has_title = any(
            "title" in v for v in props.values()
            if isinstance(v, dict)
        )
        assert has_title, f"No title field in {db_name} props"

        print(f"  {db_name}: {list(props.keys())}")

    print(f"\n  All {len(ROUTING_TABLE)} databases covered.")
    print("PASS\n")


# ── Test 4: PROPERTY_MAP skips empty fields ───────────────────────

def test_property_map_skips_empty():
    """TEST 4: None/empty fields are omitted from properties."""
    print("=" * 60)
    print("TEST 4: PROPERTY_MAP skips empty fields")
    print("=" * 60)

    item = {
        "suggested_name": "Minimal Item",
        "url": "",           # empty -> should be skipped
        "suggested_category": None,  # None -> should be skipped
        "tags": [],          # empty list -> should be skipped
        "description": "",   # empty -> should be skipped
        "item_type": "",
        "reasoning": "",
        "author": "",
        "email_sender": "",
    }

    props = PROPERTY_MAP["Python Libraries"](item)

    # Should have Name (title) only
    assert "Name" in props
    assert "Category" not in props, "None category should be skipped"
    assert "Short Description" not in props, "Empty description should be skipped"
    assert "Primary Use" not in props, "Empty reasoning should be skipped"
    print(f"  Minimal props: {list(props.keys())}")

    # Now with all fields populated
    full_item = {
        "suggested_name": "Full Item",
        "url": "https://example.com",
        "suggested_category": "Libraries",
        "tags": ["python"],
        "description": "A full item.",
        "item_type": "python_library",
        "reasoning": "Good.",
        "author": "Author",
        "email_sender": "sender@example.com",
    }

    props = PROPERTY_MAP["Python Libraries"](full_item)
    assert "Category" in props, "Category should be present"
    assert "Short Description" in props, "Short Description should be present"
    assert "Primary Use" in props, "Primary Use should be present"
    print(f"  Full props: {list(props.keys())}")

    # Also test Articles & Reads (different property names)
    props = PROPERTY_MAP["Articles & Reads"](full_item)
    assert "URL" in props, "URL should be present"
    assert "Tags" in props, "Tags should be present"
    assert "Source" in props, "Source should be present"
    assert "Short Summary" in props, "Short Summary should be present"
    print(f"  Articles & Reads full props: {list(props.keys())}")

    # Infrastructure Knowledge Base uses "Title" not "Name"
    props = PROPERTY_MAP["Infrastructure Knowledge Base"](full_item)
    assert "Title" in props, "Title should be present"
    assert "Category" in props, "Category should be present"
    assert "Description" in props, "Description should be present"
    assert "Tags" in props, "Tags should be present"
    print(f"  Infra KB full props: {list(props.keys())}")

    print("PASS\n")


# ── Test 5: Integration — create real entry ──────────────────────

def test_write_item_create():
    """TEST 5 (integration): Create a real __TEST__ entry in Articles & Reads."""
    print("=" * 60)
    print("TEST 5: write_item create (integration)")
    print("=" * 60)

    from src.notion.client import NotionClient
    from src.notion.writer import NotionWriter

    nc = NotionClient()
    store = DigestStore(":memory:")
    writer = NotionWriter(nc, store)

    run_id = store.create_run(emails_fetched=1)
    item = _make_item(
        store, run_id,
        suggested_name="__TEST__ Writer Create",
        target_database="Articles & Reads",
        item_type="article",
        url="https://example.com/test-writer",
        description="Integration test entry for NotionWriter.",
        tags=["test", "automated"],
        email_sender="test@example.com",
    )

    page_id = writer.write_item(item)
    assert page_id, "Expected a page_id back"
    print(f"  Created page: {page_id}")

    # Verify page_id stored in DB
    stored = store.get_item(item["id"])
    assert stored["notion_page_id"] == page_id
    print(f"  Stored page_id: {stored['notion_page_id']}")

    # Verify idempotent: item no longer in get_accepted_items
    remaining = store.get_accepted_items(run_id)
    assert len(remaining) == 0, "Item should no longer appear in accepted items"
    print("  Idempotent check: no items returned after write")

    # Clean up: archive the test page
    nc._client.pages.update(page_id=page_id, archived=True)
    print(f"  Cleaned up: archived page {page_id}")

    print("PASS\n")


# ── Test 6: Integration — create then update entry ────────────────

def test_write_item_update():
    """TEST 6 (integration): Create entry, then update it via dedup_status."""
    print("=" * 60)
    print("TEST 6: write_item update (integration)")
    print("=" * 60)

    from src.notion.client import NotionClient
    from src.notion.writer import NotionWriter

    nc = NotionClient()
    store = DigestStore(":memory:")
    writer = NotionWriter(nc, store)

    # First create an entry
    run_id = store.create_run(emails_fetched=1)
    create_item = _make_item(
        store, run_id,
        suggested_name="__TEST__ Writer Update",
        target_database="Articles & Reads",
        item_type="article",
        url="https://example.com/test-update",
        description="Original description.",
        tags=["test"],
        email_sender="test@example.com",
    )

    page_id = writer.write_item(create_item)
    assert page_id, "Expected page_id from create"
    print(f"  Created page: {page_id}")

    # Now simulate an update_candidate item pointing to that page
    update_decision = _make_decision(
        suggested_name="__TEST__ Writer Update v2",
        target_database="Articles & Reads",
        item_type="article",
        url="https://example.com/test-update",
        description="Updated description.",
        tags=["test", "updated"],
        dedup_status="update_candidate",
        dedup_matches=[{"page_id": page_id, "name": "__TEST__ Writer Update", "database": "Articles & Reads"}],
        email_sender="test@example.com",
    )
    update_id = store.add_item(run_id, update_decision)
    store.set_decision(update_id, "accepted")
    update_item = store.get_item(update_id)

    updated_page_id = writer.write_item(update_item)
    assert updated_page_id == page_id, f"Expected same page_id, got {updated_page_id}"
    print(f"  Updated page: {updated_page_id} (same as original)")

    # Clean up: archive
    nc._client.pages.update(page_id=page_id, archived=True)
    print(f"  Cleaned up: archived page {page_id}")

    print("PASS\n")


# ── Main ──────────────────────────────────────────────────────────

def main():
    # Unit tests (always run)
    test_get_accepted_items()
    test_set_notion_page_id()
    test_property_map_all_databases()
    test_property_map_skips_empty()

    # Integration tests (require NOTION_API_KEY)
    import os
    if os.environ.get("NOTION_API_KEY"):
        test_write_item_create()
        test_write_item_update()
        print("=" * 60)
        print("ALL TESTS PASSED (unit + integration)")
        print("=" * 60)
    else:
        print("=" * 60)
        print("UNIT TESTS PASSED (skipped integration -- no NOTION_API_KEY)")
        print("=" * 60)


if __name__ == "__main__":
    main()
