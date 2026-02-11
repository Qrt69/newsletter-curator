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

from src.email.extractor import ContentExtractor, _is_non_article_url, _is_boilerplate_text
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


def test_non_article_url_filter():
    """TEST: _is_non_article_url correctly identifies non-article URLs."""
    print("=" * 60)
    print("TEST: Non-article URL filter")
    print("=" * 60)

    # Should be filtered (non-article)
    non_articles = [
        "https://medium.com/@johndoe",
        "https://medium.com/towards-data-science",
        "https://medium.com/tag/python",
        "https://medium.com/m/signin",
        "https://medium.com",
        "https://twitter.com/elonmusk",
        "https://x.com/someuser",
        "https://www.linkedin.com/in/john-doe",
        "https://www.linkedin.com/company/anthropic",
        "https://github.com/anthropics",
        "https://apps.apple.com/app/some-app/id123456",
        "https://play.google.com/store/apps/details?id=com.example",
    ]
    for url in non_articles:
        assert _is_non_article_url(url), f"Should filter: {url}"
        print(f"  FILTERED (correct): {url}")

    # Should NOT be filtered (real articles)
    articles = [
        "https://medium.com/@johndoe/my-awesome-article-abc123",
        "https://medium.com/towards-data-science/some-article-def456",
        "https://twitter.com/user/status/123456789",
        "https://x.com/user/status/987654321",
        "https://www.linkedin.com/pulse/some-article",
        "https://github.com/anthropics/claude-code",
        "https://example.com/blog/my-article",
        "https://docs.python.org/3/library/asyncio.html",
    ]
    for url in articles:
        assert not _is_non_article_url(url), f"Should NOT filter: {url}"
        print(f"  KEPT (correct):     {url}")

    print("PASS\n")


def test_boilerplate_link_text_filter():
    """TEST: _is_boilerplate_text correctly identifies boilerplate phrases."""
    print("=" * 60)
    print("TEST: Boilerplate link text filter")
    print("=" * 60)

    # Should be filtered
    boilerplate = [
        "Read more",
        "Continue reading",
        "Follow",
        "Subscribe",
        "Sign up",
        "View in browser",
        "Open in app",
        "Learn more",
        "Click here",
        "Download app",
        "SUBSCRIBE",
        "Share",
        "Tweet",
    ]
    for text in boilerplate:
        assert _is_boilerplate_text(text), f"Should filter: '{text}'"
        print(f"  FILTERED (correct): '{text}'")

    # Should NOT be filtered (real article titles)
    real_titles = [
        "DuckDB",
        "7 Underrated Python Libraries You Should Know",
        "How to Build a RAG Pipeline",
        "Claude 3.5 Sonnet Released",
        "The State of AI in 2025",
        "Read This Before You Deploy",
        "Following the Money: VC Trends",
    ]
    for text in real_titles:
        assert not _is_boilerplate_text(text), f"Should NOT filter: '{text}'"
        print(f"  KEPT (correct):     '{text}'")

    print("PASS\n")


def test_parse_links_medium_newsletter():
    """TEST: parse_links filters Medium newsletter HTML correctly."""
    print("=" * 60)
    print("TEST: Parse links from Medium-style newsletter")
    print("=" * 60)

    medium_html = """
    <html><body>
    <h1>Weekly Digest</h1>

    <!-- Real articles (should survive) -->
    <a href="https://medium.com/@author1/amazing-python-tips-abc123">Amazing Python Tips</a>
    <a href="https://medium.com/towards-data-science/rag-pipeline-guide-def456">RAG Pipeline Guide</a>
    <a href="https://github.com/anthropics/claude-code">Claude Code</a>
    <a href="https://example.com/blog/great-article">A Great Article on Testing</a>

    <!-- Author profiles (should be filtered) -->
    <a href="https://medium.com/@author1">Author One</a>
    <a href="https://medium.com/@author2">Author Two</a>

    <!-- Publication pages (should be filtered) -->
    <a href="https://medium.com/towards-data-science">Towards Data Science</a>
    <a href="https://medium.com/better-programming">Better Programming</a>

    <!-- Tag pages (should be filtered) -->
    <a href="https://medium.com/tag/python">Python</a>
    <a href="https://medium.com/tag/machine-learning">Machine Learning</a>

    <!-- Social profiles (should be filtered) -->
    <a href="https://twitter.com/someauthor">Follow on Twitter</a>
    <a href="https://www.linkedin.com/in/some-person">LinkedIn Profile</a>
    <a href="https://github.com/someuser">GitHub Profile</a>

    <!-- Boilerplate text (should be filtered) -->
    <a href="https://example.com/some-page">Read more</a>
    <a href="https://example.com/another-page">Subscribe</a>
    <a href="https://example.com/yet-another">Follow</a>
    <a href="https://example.com/app">Open in app</a>

    <!-- Already caught by _SKIP_URL_PATTERNS -->
    <a href="https://example.com/unsubscribe">Unsubscribe</a>
    <a href="https://medium.com/m/signin">Sign in</a>
    </body></html>
    """

    extractor = ContentExtractor()
    links = extractor.parse_links(medium_html)

    print(f"  Found {len(links)} link(s):")
    for link in links:
        print(f"    - {link['link_text'][:40]:40s} -> {link['url'][:60]}")

    urls = [l["url"] for l in links]
    texts = [l["link_text"] for l in links]

    # Should keep the 4 real article links
    assert len(links) == 4, f"Expected 4 links, got {len(links)}: {urls}"
    assert any("amazing-python-tips" in u for u in urls), "Should keep article link"
    assert any("rag-pipeline-guide" in u for u in urls), "Should keep article link"
    assert any("claude-code" in u for u in urls), "Should keep GitHub repo link"
    assert any("great-article" in u for u in urls), "Should keep blog link"

    # Should NOT have any profiles, tags, or boilerplate
    assert not any("medium.com/@author1" == u for u in urls), "Should filter author profile"
    assert not any("medium.com/tag/" in u for u in urls), "Should filter tag pages"
    assert not any("twitter.com/someauthor" == u.rstrip("/") for u in urls), "Should filter Twitter profile"
    assert "Read more" not in texts, "Should filter boilerplate text"
    assert "Subscribe" not in texts, "Should filter boilerplate text"

    extractor.close()
    print("PASS\n")


def main():
    # Sync tests
    test_parse_links()
    test_resolve_url()
    test_extract_article()
    test_bad_url()

    # New filter tests
    test_non_article_url_filter()
    test_boilerplate_link_text_filter()
    test_parse_links_medium_newsletter()

    # Async test (needs EmailFetcher)
    asyncio.run(test_full_pipeline())

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
