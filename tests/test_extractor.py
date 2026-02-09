"""
Integration test for the ContentExtractor.

Tests link parsing, redirect resolution, article extraction, and the
full pipeline with a real newsletter email from the mailbox.

Run: uv run python tests/test_extractor.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.email.extractor import ContentExtractor
from src.email.fetcher import EmailFetcher

# Sample newsletter HTML with a mix of real links and boilerplate
SAMPLE_HTML = """
<html><body>
<p>Here are this week's top picks:</p>

<a href="https://github.com/anthropics/claude-code">Claude Code on GitHub</a>

<a href="https://httpbin.org/redirect-to?url=https://example.com&status_code=302">
    Redirected Link
</a>

<a href="https://example.com/nonexistent-page-xyz-404">Some Article</a>

<!-- Boilerplate that should be filtered -->
<a href="mailto:hello@example.com">Email us</a>
<a href="https://example.com/unsubscribe">Unsubscribe</a>
<a href="https://twitter.com/intent/tweet?text=hello">Share on Twitter</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=test">Share on Facebook</a>
<a href="https://example.com/manage-preferences">Manage preferences</a>
<a href="javascript:void(0)">Click here</a>
<a href="#top">Back to top</a>

<!-- Image-only anchor (no text) should be filtered -->
<a href="https://example.com/img-link"><img src="logo.png"></a>

<!-- Duplicate URL should be filtered -->
<a href="https://github.com/anthropics/claude-code">Claude Code again</a>
</body></html>
"""


def test_parse_links():
    """TEST 1: Parse links from sample HTML, verify boilerplate filtered."""
    print("=" * 60)
    print("TEST 1: Parse links from sample HTML")
    print("=" * 60)

    extractor = ContentExtractor()
    links = extractor.parse_links(SAMPLE_HTML)

    print(f"  Found {len(links)} link(s):")
    for link in links:
        print(f"    - {link['link_text'][:40]:40s} -> {link['url'][:60]}")

    # Should have exactly 3 links (GitHub, redirect, nonexistent)
    assert len(links) == 3, f"Expected 3 links, got {len(links)}"

    urls = [l["url"] for l in links]
    assert any("github.com" in u for u in urls), "Should include GitHub link"
    assert not any("unsubscribe" in u for u in urls), "Should filter unsubscribe"
    assert not any("twitter.com/intent" in u for u in urls), "Should filter Twitter share"
    assert not any("facebook.com/sharer" in u for u in urls), "Should filter Facebook share"
    assert not any("mailto:" in u for u in urls), "Should filter mailto"

    extractor.close()
    print("PASS\n")


def test_resolve_url():
    """TEST 2: Resolve a known redirect URL."""
    print("=" * 60)
    print("TEST 2: Resolve redirect URL")
    print("=" * 60)

    extractor = ContentExtractor()
    resolved, error = extractor.resolve_url(
        "https://httpbin.org/redirect-to?url=https://example.com&status_code=302"
    )
    print(f"  Resolved to: {resolved}")
    print(f"  Error: {error}")

    assert error is None, f"Should resolve without error, got: {error}"
    assert "example.com" in resolved, f"Should resolve to example.com, got: {resolved}"

    extractor.close()
    print("PASS\n")


def test_extract_article():
    """TEST 3: Extract article from a real public URL."""
    print("=" * 60)
    print("TEST 3: Extract article from GitHub README")
    print("=" * 60)

    extractor = ContentExtractor()
    article = extractor.extract_article("https://github.com/anthropics/claude-code")
    print(f"  Title:    {article.get('title', '')}")
    print(f"  Status:   {article['extraction_status']}")
    print(f"  Text len: {article['text_length']}")
    print(f"  Hostname: {article.get('hostname', '')}")

    assert article["extraction_status"] == "ok", (
        f"Expected 'ok', got '{article['extraction_status']}': {article.get('error')}"
    )
    assert article["text_length"] > 0, "Should extract some text"
    assert article["hostname"] == "github.com"

    extractor.close()
    print("PASS\n")


def test_bad_url():
    """TEST 4: Handle a bad URL gracefully (no crash)."""
    print("=" * 60)
    print("TEST 4: Handle bad URL gracefully")
    print("=" * 60)

    extractor = ContentExtractor()
    article = extractor.extract_article("https://this-domain-does-not-exist-xyz.invalid/page")
    print(f"  Status: {article['extraction_status']}")
    print(f"  Error:  {article.get('error', '')[:80]}")

    assert article["extraction_status"] == "fetch_failed", (
        f"Expected 'fetch_failed', got '{article['extraction_status']}'"
    )
    assert article["text_length"] == 0
    assert article["error"] is not None

    extractor.close()
    print("PASS\n")


async def test_full_pipeline():
    """TEST 5: Full pipeline with a real newsletter email body."""
    print("=" * 60)
    print("TEST 5: Full pipeline with real newsletter email")
    print("=" * 60)

    fetcher = EmailFetcher()
    emails = await fetcher.fetch_emails()
    if not emails:
        print("  SKIPPED (no emails in 'to qualify' folder)")
        return

    # Pick the first email with a body
    body_html = ""
    email_subject = ""
    for email in emails[:5]:
        body = await fetcher.get_email_body(email["id"])
        if body and len(body) > 100:
            body_html = body
            email_subject = email["subject"]
            break

    if not body_html:
        print("  SKIPPED (no email with substantial body found)")
        return

    subj_display = email_subject[:60].encode("ascii", errors="replace").decode("ascii")
    print(f"  Email: {subj_display}")

    extractor = ContentExtractor()
    items = extractor.extract_from_email(body_html)
    print(f"  Extracted {len(items)} item(s):")
    for item in items[:10]:
        title = (item.get("title") or item.get("link_text", "?"))[:50]
        title = title.encode("ascii", errors="replace").decode("ascii")
        print(f"    [{item['extraction_status']:17s}] {title} ({item['text_length']} chars)")

    assert len(items) > 0, "Should extract at least one link from a newsletter"

    # TEST 6: Summary statistics
    print()
    print("=" * 60)
    print("TEST 6: Summary statistics")
    print("=" * 60)
    stats = ContentExtractor.summary(items)
    for key, val in stats.items():
        print(f"  {key}: {val}")
    assert stats["total"] == len(items)
    assert stats["total"] == stats["ok"] + stats["redirect_failed"] + stats["fetch_failed"] + stats["extraction_empty"]

    extractor.close()
    print("PASS\n")


def main():
    # Sync tests
    test_parse_links()
    test_resolve_url()
    test_extract_article()
    test_bad_url()

    # Async test (needs EmailFetcher)
    asyncio.run(test_full_pipeline())

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
