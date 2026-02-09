"""
Test script for the Notion client wrapper.

Tests against the real Books & Papers database to verify:
  1. Query -fetch entries and read properties
  2. Schema -inspect database structure
  3. Create -add a test entry
  4. Update -modify the test entry
  5. Cleanup -archive (delete) the test entry

Run with:  uv run python tests/test_notion_client.py
"""

import sys
import os

# Add the project root to the path so we can import src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.notion.client import NotionClient, title, rich_text, select

# We'll test against Books & Papers -a small, safe database.
TEST_DB = "Books & Papers"


def main():
    nc = NotionClient()
    print("Connected to Notion successfully!\n")

    # ── Test 1: Get database schema ──────────────────────────────────
    print("=" * 60)
    print("TEST 1: Database schema")
    print("=" * 60)
    schema = nc.get_database_schema(TEST_DB)
    for prop_name, prop_type in sorted(schema.items()):
        print(f"  {prop_name}: {prop_type}")
    print(f"\n  {len(schema)} properties found.\n")

    # ── Test 2: Query all entries ────────────────────────────────────
    print("=" * 60)
    print("TEST 2: Query all entries")
    print("=" * 60)
    entries = nc.query_database(TEST_DB)
    print(f"Found {len(entries)} entries.\n")

    # Show the first 3 entries (just the title/name)
    for entry in entries[:3]:
        # Find the title property -it's the one with type "title" in the schema
        title_prop = next(
            (name for name, ptype in schema.items() if ptype == "title"),
            None,
        )
        if title_prop:
            print(f"  - {entry.get(title_prop, '(no title)')}")
    if len(entries) > 3:
        print(f"  ... and {len(entries) - 3} more.\n")

    # ── Test 3: Create a test entry ──────────────────────────────────
    print("=" * 60)
    print("TEST 3: Create a test entry")
    print("=" * 60)
    # We need to know the exact property names for this database.
    # From the schema, the title property is typically "Name".
    title_prop = next(
        (name for name, ptype in schema.items() if ptype == "title"),
        "Name",
    )

    test_props = {
        title_prop: title("__TEST__ Newsletter Curator Test Entry"),
    }

    # Add a rich_text property if one exists (e.g., Author or Description)
    text_props = [name for name, ptype in schema.items() if ptype == "rich_text"]
    if text_props:
        test_props[text_props[0]] = rich_text("Created by test script -safe to delete")

    created = nc.create_entry(TEST_DB, test_props)
    created_id = created["id"]
    print(f"Created entry: {created.get(title_prop)}")
    print(f"Page ID: {created_id}\n")

    # ── Test 4: Update the test entry ────────────────────────────────
    print("=" * 60)
    print("TEST 4: Update the test entry")
    print("=" * 60)
    # Add a select property if one exists
    select_props = [name for name, ptype in schema.items() if ptype == "select"]
    if select_props:
        # Get the available options for the first select property
        db_meta = nc._client.databases.retrieve(
            database_id=nc.get_database_id(TEST_DB)
        )
        select_prop_name = select_props[0]
        options = db_meta["properties"][select_prop_name].get("select", {}).get("options", [])
        if options:
            test_option = options[0]["name"]
            updated = nc.update_entry(
                created_id,
                {select_prop_name: select(test_option)},
            )
            print(f"Updated '{select_prop_name}' to '{test_option}'")
        else:
            print(f"No options available for '{select_prop_name}', skipping update.")
    else:
        print("No select properties found, skipping update test.")

    # ── Test 5: Clean up -archive the test entry ────────────────────
    print(f"\n{'=' * 60}")
    print("TEST 5: Cleanup -archive test entry")
    print("=" * 60)
    nc._client.pages.update(page_id=created_id, archived=True)
    print(f"Archived test entry {created_id}")

    print(f"\n{'=' * 60}")
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
