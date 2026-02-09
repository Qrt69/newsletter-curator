"""
Integration test for the EmailFetcher.

Tests against live M365 mailbox — requires MS_GRAPH_* vars in .env.
Run: uv run python tests/test_email.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.email.fetcher import EmailFetcher


async def main():
    # -- 1. Create fetcher and verify connection ----
    print("=" * 60)
    print("TEST 1: Create EmailFetcher and find folders")
    print("=" * 60)
    fetcher = EmailFetcher()
    await fetcher._find_folders()
    print(f"  'to qualify' folder ID:  {fetcher._qualify_folder_id}")
    print(f"  'processed' folder ID:   {fetcher._processed_folder_id}")
    assert fetcher._qualify_folder_id, "Should find 'to qualify' folder"
    assert fetcher._processed_folder_id, "Should find 'processed' subfolder"
    print("PASS\n")

    # -- 2. Fetch emails from "to qualify" ----------
    print("=" * 60)
    print("TEST 2: Fetch emails from 'to qualify'")
    print("=" * 60)
    emails = await fetcher.fetch_emails()
    print(f"  Found {len(emails)} email(s)")
    for email in emails[:10]:
        subj = email["subject"][:60].encode("ascii", errors="replace").decode("ascii")
        date = email["received_at"][:10]
        print(f"  - [{date}] {subj}")
    print("PASS\n")

    # -- 3. Get body of first email -----------------
    if emails:
        print("=" * 60)
        print("TEST 3: Get email body")
        print("=" * 60)
        body = await fetcher.get_email_body(emails[0]["id"])
        print(f"  Body length: {len(body)} chars")
        print(f"  Starts with: {body[:100]}...")
        assert len(body) > 0, "Body should not be empty"
        print("PASS\n")
    else:
        print("TEST 3: SKIPPED (no emails in folder)\n")

    # -- 4. Move to processed (manual, disabled) ----
    # Uncomment to test moving. This actually moves the email!
    # if emails:
    #     print("=" * 60)
    #     print("TEST 4: Move first email to processed")
    #     print("=" * 60)
    #     await fetcher.move_to_processed(emails[0]["id"])
    #     print(f"  Moved: {emails[0]['subject'][:60]}")
    #     print("PASS\n")

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
