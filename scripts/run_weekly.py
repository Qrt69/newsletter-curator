"""
Newsletter Curator - Weekly Pipeline

Orchestrates the full ingest pipeline:
  1. Fetch emails from M365 "To qualify" folder
  2. Extract article links + content from each email
  3. Score extracted items via Claude API
  4. Route scored items to Notion databases (dedup check)
  5. Store routing decisions in digest DB

After this runs, items appear in the Reflex web app for review.
Accepted items are written to Notion via the web app or a separate write step.

Usage:
    uv run python scripts/run_weekly.py             # run once
    uv run python scripts/run_weekly.py --schedule   # run on APScheduler weekly
    uv run python scripts/run_weekly.py --write RUN_ID  # write accepted items for a run
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.email.fetcher import EmailFetcher
from src.email.extractor import ContentExtractor
from src.email.browser import BrowserSession, BrowserFetcher
from src.intelligence.scorer import Scorer
from src.intelligence.router import Router
from src.intelligence.feedback import FeedbackProcessor
from src.notion.client import NotionClient
from src.notion.dedup import DedupIndex
from src.notion.writer import NotionWriter
from src.storage.digest import DigestStore

import os
import time

DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "digest.db")
LOCK_FILE = os.path.join(DATA_DIR, ".pipeline_running")
LOCK_STALE_SECONDS = 30 * 60  # 30 minutes


def is_pipeline_locked() -> bool:
    """Check if the pipeline lock file exists and is not stale."""
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age > LOCK_STALE_SECONDS:
            print(f"  Stale lock file detected ({age:.0f}s old), removing.")
            os.remove(LOCK_FILE)
            return False
    except OSError:
        return False
    return True


def _acquire_lock() -> bool:
    """Create the lock file. Returns False if already locked."""
    if is_pipeline_locked():
        return False
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def _release_lock():
    """Remove the lock file."""
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


async def run_pipeline():
    """Run the full ingest pipeline once."""
    if not _acquire_lock():
        print("Pipeline is already running. Skipping.")
        return

    try:
        await _run_pipeline_inner()
    finally:
        _release_lock()


async def _run_pipeline_inner():
    """Inner pipeline logic (called with lock held)."""
    print("=" * 60)
    print("Newsletter Curator - Pipeline Run")
    print("=" * 60)

    store = DigestStore(DB_PATH)

    # 1. Fetch emails
    print("\n[1/5] Fetching emails...")
    fetcher = EmailFetcher()
    emails = await fetcher.fetch_emails()
    print(f"  Fetched {len(emails)} emails from 'To qualify'")

    if not emails:
        print("  No emails to process. Done.")
        return

    run_id = store.create_run(emails_fetched=len(emails))
    print(f"  Run ID: {run_id}")

    # 1b. Ensure browser session for Medium/Beehiiv
    print("\n[1b] Checking browser session for Medium/Beehiiv...")
    session = BrowserSession(fetcher)
    logged_in = await session.ensure_logged_in()
    browser_fetcher = BrowserFetcher(state_path=session.state_path) if logged_in else BrowserFetcher()
    print(f"  Medium session: {'active' if logged_in else 'not available (will try without auth)'}")

    # 2. Extract content
    print("\n[2/5] Extracting content...")
    extractor = ContentExtractor(browser_fetcher=browser_fetcher)
    all_items = []
    for i, email in enumerate(emails, 1):
        subject = email["subject"][:50].encode("ascii", errors="replace").decode("ascii")
        print(f"  [{i}/{len(emails)}] {subject}")
        items = extractor.extract_from_email(email["body_html"])
        # Tag items with email metadata
        for item in items:
            item["_email_meta"] = {
                "email_id": email["id"],
                "email_subject": email["subject"],
                "email_sender": email.get("sender_name") or email["sender"],
            }
        all_items.extend(items)
    extractor.close()
    print(f"  Extracted {len(all_items)} items total")

    if not all_items:
        store.finish_run(run_id, {
            "items_extracted": 0, "items_scored": 0,
            "items_proposed": 0, "items_skipped": 0, "status": "completed",
        })
        print("  No items extracted. Done.")
        return

    # 3. Score items (with feedback learning)
    print("\n[3/5] Scoring items...")
    feedback_proc = FeedbackProcessor(store)
    feedback_examples = feedback_proc.format_examples()
    override_count = len(feedback_proc.get_overrides())
    if feedback_examples:
        print(f"  Injecting {override_count} feedback overrides into scorer prompt")
    else:
        print("  No feedback overrides to inject")

    scorer = Scorer(feedback_examples=feedback_examples)
    scored = scorer.score_batch(all_items)
    print(f"  Scored {len(scored)} items")
    print(f"  Token usage: {scorer.stats()}")

    # Copy email metadata and extractor fields to scored items
    # (Scorer only passes through url + link_text; we need title/author/text for DigestStore)
    for original, result in zip(all_items, scored):
        result["_email_meta"] = original.get("_email_meta", {})
        for field in ("title", "author", "text"):
            if field not in result and original.get(field):
                result[field] = original[field]

    # 3b. Explode listicles
    from src.intelligence.exploder import ListicleExploder
    exploder = ListicleExploder()
    pre_count = len(scored)
    scored = exploder.process_batch(scored)
    if len(scored) != pre_count:
        print(f"  Exploded listicles: {pre_count} -> {len(scored)} items")
    exploder_stats = exploder.stats()
    if exploder_stats["items_exploded"] > 0:
        print(f"  Exploder stats: {exploder_stats}")

    # 4. Route items
    print("\n[4/5] Routing items...")
    nc = NotionClient()
    dedup = DedupIndex(nc)
    dedup.build()  # Always fresh from Notion — never trust cache for pipeline runs
    router = Router(dedup)
    decisions = router.route_batch(scored)
    summary = Router.summary(decisions)
    print(f"  Routing summary: {summary['by_action']}")

    # Copy email metadata and extractor fields to decisions
    # (Router doesn't pass through title/author/text either)
    for original, decision in zip(scored, decisions):
        decision["_email_meta"] = original.get("_email_meta", {})
        for field in ("title", "author", "text", "source_article"):
            if field not in decision and original.get(field):
                decision[field] = original[field]

    # 5. Store in digest DB
    print("\n[5/5] Storing in digest DB...")
    for decision in decisions:
        email_meta = decision.pop("_email_meta", None)
        store.add_item(run_id, decision, email_meta)

    store.finish_run(run_id, {
        "items_extracted": len(all_items),
        "items_scored": len(scored),
        "items_proposed": summary["by_action"].get("propose", 0),
        "items_skipped": summary["by_action"].get("skip", 0),
        "status": "completed",
    })

    print(f"\n  Run {run_id} complete!")
    print(f"  Proposed: {summary['by_action'].get('propose', 0)}")
    print(f"  Skipped:  {summary['by_action'].get('skip', 0)}")
    print(f"  Review:   {summary['by_action'].get('review', 0)}")

    # Feedback analysis
    fb_stats = feedback_proc.stats()
    if fb_stats["rule_proposals"] > 0:
        print(f"  Feedback: {fb_stats['rule_proposals']} rule proposals detected")
        for proposal in feedback_proc.get_rule_proposals():
            print(f"    -> {proposal['proposal']}")

    print("  -> Open the web app to review proposed items.")

    # 6. Move processed emails
    print("\n[+] Moving emails to 'Processed'...")
    moved = 0
    for email in emails:
        try:
            await fetcher.move_to_processed(email["id"])
            moved += 1
        except Exception as exc:
            print(f"  Failed to move {email['id']}: {exc}")
    print(f"  Moved {moved}/{len(emails)} emails")


def write_accepted(run_id: int) -> dict:
    """Write all accepted items for a run to Notion. Returns result summary."""
    print(f"Writing accepted items for run {run_id}...")
    nc = NotionClient()
    store = DigestStore(DB_PATH)
    dedup = DedupIndex(nc)
    dedup.load()  # Cache is fine here — relations are non-destructive
    writer = NotionWriter(nc, store, dedup_index=dedup)
    result = writer.write_batch(run_id)
    print(f"  Created: {result['created']}")
    print(f"  Updated: {result['updated']}")
    print(f"  Failed:  {result['failed']}")
    if result["errors"]:
        for err in result["errors"]:
            print(f"    - {err}")

    # Invalidate dedup cache so next pipeline run rebuilds with newly written items
    if result["created"] > 0 or result["updated"] > 0:
        from src.notion.dedup import _cache_file
        cache = _cache_file()
        if cache.exists():
            cache.unlink()
            print("  Dedup cache invalidated (will rebuild on next pipeline run)")

    return result


def start_scheduler():
    """Start APScheduler with a weekly job."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler()

    def _run_job():
        asyncio.run(run_pipeline())

    scheduler.add_job(
        _run_job,
        CronTrigger(day_of_week="sun", hour=18, minute=0),
        id="weekly_pipeline",
        name="Weekly newsletter pipeline",
    )

    print("Scheduler started. Pipeline will run every Sunday at 18:00.")
    print("Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


def main():
    parser = argparse.ArgumentParser(description="Newsletter Curator Pipeline")
    parser.add_argument("--schedule", action="store_true",
                        help="Start APScheduler (weekly Sunday 18:00)")
    parser.add_argument("--write", type=int, metavar="RUN_ID",
                        help="Write accepted items for a run to Notion")
    parser.add_argument("--browser-login", action="store_true",
                        help="Open browser for manual Medium login (saves session)")
    args = parser.parse_args()

    if args.browser_login:
        from src.email.browser import manual_login
        asyncio.run(manual_login())
    elif args.write:
        write_accepted(args.write)
    elif args.schedule:
        start_scheduler()
    else:
        asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
