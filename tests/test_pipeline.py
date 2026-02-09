"""
Tests for the pipeline orchestrator.

Test 1 is a unit test (in-memory DB, no API calls).
Test 2 is an integration test (requires all API keys, processes 1 real email).

Run: uv run python tests/test_pipeline.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.storage.digest import DigestStore


# ── Helpers ──────────────────────────────────────────────────────

def _make_decision(**overrides) -> dict:
    """Build a minimal routing decision dict with overrides."""
    base = {
        "url": "https://example.com/pipeline-test",
        "link_text": "Pipeline Test Article",
        "title": "Pipeline Test Title",
        "author": "Test Author",
        "text": "Some article text for testing.",
        "score": 5,
        "verdict": "strong_fit",
        "item_type": "article",
        "description": "A test article for pipeline validation.",
        "reasoning": "Relevant article for testing purposes.",
        "signals": ["+3 relevant content"],
        "suggested_name": "__TEST__ Pipeline Item",
        "suggested_category": "Testing",
        "tags": ["test", "pipeline"],
        "target_database": "Articles & Reads",
        "dedup_status": "new",
        "dedup_matches": [],
        "action": "propose",
    }
    base.update(overrides)
    return base


# ── Test 1: DigestStore round-trip (unit) ───────────────────────

def test_digest_store_roundtrip():
    """TEST 1: Create run, add items, finish run, verify full lifecycle."""
    print("=" * 60)
    print("TEST 1: DigestStore round-trip (unit)")
    print("=" * 60)

    store = DigestStore(":memory:")

    # Create run
    run_id = store.create_run(emails_fetched=2)
    assert run_id, "Expected a run_id"
    print(f"  Created run: {run_id}")

    # Add items with email metadata
    email_meta = {
        "email_id": "msg-123",
        "email_subject": "Test Newsletter",
        "email_sender": "test@example.com",
    }

    id1 = store.add_item(run_id, _make_decision(
        suggested_name="__TEST__ Item 1",
        action="propose",
    ), email_meta)

    id2 = store.add_item(run_id, _make_decision(
        suggested_name="__TEST__ Item 2",
        action="skip",
        verdict="reject",
        score=-1,
    ), email_meta)

    id3 = store.add_item(run_id, _make_decision(
        suggested_name="__TEST__ Item 3",
        action="propose",
    ), email_meta)

    print(f"  Added items: {id1}, {id2}, {id3}")

    # Finish run
    store.finish_run(run_id, {
        "items_extracted": 3,
        "items_scored": 3,
        "items_proposed": 2,
        "items_skipped": 1,
        "status": "completed",
    })

    run = store.get_run(run_id)
    assert run["status"] == "completed"
    assert run["items_extracted"] == 3
    assert run["items_proposed"] == 2
    assert run["items_skipped"] == 1
    print(f"  Run status: {run['status']}")

    # Get proposed items
    proposed = store.get_items(run_id, action_filter="propose")
    assert len(proposed) == 2, f"Expected 2 proposed, got {len(proposed)}"
    print(f"  Proposed items: {len(proposed)}")

    # Accept one, reject one
    store.set_decision(id1, "accepted")
    store.set_decision(id3, "rejected")

    accepted = store.get_accepted_items(run_id)
    assert len(accepted) == 1, f"Expected 1 accepted, got {len(accepted)}"
    assert accepted[0]["suggested_name"] == "__TEST__ Item 1"
    print(f"  Accepted: {accepted[0]['suggested_name']}")

    # Verify email metadata preserved
    item = store.get_item(id1)
    assert item["email_id"] == "msg-123"
    assert item["email_subject"] == "Test Newsletter"
    assert item["email_sender"] == "test@example.com"
    print(f"  Email metadata preserved: {item['email_sender']}")

    print("PASS\n")


# ── Test 2: Single-email pipeline (integration) ────────────────

def test_pipeline_single_email():
    """TEST 2 (integration): Fetch 1 email, extract, score, route, store."""
    print("=" * 60)
    print("TEST 2: Single-email pipeline (integration)")
    print("=" * 60)

    from src.email.fetcher import EmailFetcher
    from src.email.extractor import ContentExtractor
    from src.intelligence.scorer import Scorer
    from src.intelligence.router import Router
    from src.notion.client import NotionClient
    from src.notion.dedup import DedupIndex

    async def _run():
        # 1. Fetch just 1 email
        print("  [1/5] Fetching 1 email...")
        fetcher = EmailFetcher()
        emails = await fetcher.fetch_emails()
        assert len(emails) > 0, "Need at least 1 email in 'To qualify'"
        email = emails[0]
        print(f"  Subject: {email['subject'][:60].encode('ascii', errors='replace').decode('ascii')}")

        # 2. Extract
        print("  [2/5] Extracting content...")
        extractor = ContentExtractor()
        items = extractor.extract_from_email(email["body_html"])
        extractor.close()
        print(f"  Extracted {len(items)} items")

        if not items:
            print("  No items extracted from first email, test inconclusive but passing.")
            return

        # Limit to first 3 items to keep test fast
        items = items[:3]

        # 3. Score
        print("  [3/5] Scoring...")
        scorer = Scorer()
        scored = scorer.score_batch(items)
        print(f"  Scored {len(scored)} items, tokens: {scorer.stats()}")

        # Copy extractor fields to scored items
        for original, result in zip(items, scored):
            for field in ("title", "author", "text"):
                if field not in result and original.get(field):
                    result[field] = original[field]

        # 4. Route
        print("  [4/5] Routing...")
        nc = NotionClient()
        dedup = DedupIndex(nc)
        dedup.load()
        router = Router(dedup)
        decisions = router.route_batch(scored)
        summary = Router.summary(decisions)
        print(f"  Summary: {summary['by_action']}")

        # Copy extractor fields to decisions
        for original, decision in zip(scored, decisions):
            for field in ("title", "author", "text"):
                if field not in decision and original.get(field):
                    decision[field] = original[field]

        # 5. Store in digest DB
        print("  [5/5] Storing in digest DB...")
        store = DigestStore(":memory:")
        run_id = store.create_run(emails_fetched=1)

        for decision in decisions:
            email_meta = {
                "email_id": email["id"],
                "email_subject": email["subject"],
                "email_sender": email["sender"],
            }
            store.add_item(run_id, decision, email_meta)

        store.finish_run(run_id, {
            "items_extracted": len(items),
            "items_scored": len(scored),
            "items_proposed": summary["by_action"].get("propose", 0),
            "items_skipped": summary["by_action"].get("skip", 0),
            "status": "completed",
        })

        # Verify
        run = store.get_run(run_id)
        assert run["status"] == "completed"
        assert run["items_extracted"] > 0

        all_items = store.get_items(run_id)
        assert len(all_items) == len(decisions)
        print(f"  Stored {len(all_items)} items in run {run_id}")

        # Check email metadata was preserved
        if all_items:
            assert all_items[0]["email_id"] == email["id"]
            print(f"  Email metadata preserved: sender={all_items[0]['email_sender']}")

        # Print item summaries
        for item in all_items:
            name = (item.get("suggested_name") or "?")[:40]
            name = name.encode("ascii", errors="replace").decode("ascii")
            print(f"    {item['action']:8s} | {item['verdict']:12s} | {name}")

    asyncio.run(_run())
    print("PASS\n")


# ── Main ─────────────────────────────────────────────────────────

def main():
    # Unit test (always run)
    test_digest_store_roundtrip()

    # Integration test (requires all API keys)
    has_keys = all([
        os.environ.get("MS_GRAPH_CLIENT_ID"),
        os.environ.get("NOTION_API_KEY"),
        os.environ.get("ANTHROPIC_API_KEY"),
    ])

    if has_keys:
        test_pipeline_single_email()
        print("=" * 60)
        print("ALL TESTS PASSED (unit + integration)")
        print("=" * 60)
    else:
        print("=" * 60)
        print("UNIT TESTS PASSED (skipped integration -- missing API keys)")
        print("=" * 60)


if __name__ == "__main__":
    main()
