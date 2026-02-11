"""
Tests for browser-based fetching (Playwright integration).

Unit tests run without external services.
Integration tests need Playwright + optionally MS Graph credentials.

Usage:
    uv run python tests/test_browser.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Unit tests ────────────────────────────────────────────────────


def test_needs_browser():
    """Test domain detection for browser-needing URLs."""
    from src.email.browser import needs_browser

    # Should match
    assert needs_browser("https://medium.com/@user/article-123")
    assert needs_browser("https://www.medium.com/article")
    assert needs_browser("https://betterprogramming.medium.com/some-post")
    assert needs_browser("https://newsletter.beehiiv.com/p/some-post")
    assert needs_browser("https://www.beehiiv.com/something")

    # Should NOT match
    assert not needs_browser("https://github.com/repo")
    assert not needs_browser("https://example.com")
    assert not needs_browser("https://notmedium.com/article")
    assert not needs_browser("https://medium.com.evil.com/phish")
    assert not needs_browser("")

    print("  [PASS] test_needs_browser")


def test_extract_otp_code():
    """Test OTP code extraction from Medium email HTML."""
    from src.email.browser import BrowserSession

    extract = BrowserSession._extract_otp_code

    # Typical Medium OTP email
    assert extract("<p>Your code is <b>482917</b></p>") == "482917"
    # Code in plain text
    assert extract("<p>Use this one-time code to sign in: 123456</p>") == "123456"
    # No code present
    assert extract("<p>Welcome to Medium</p>") is None
    # Empty / None
    assert extract("") is None
    assert extract(None) is None
    # Should NOT match 5-digit or 7-digit numbers
    assert extract("<p>Code: 12345</p>") is None
    assert extract("<p>Code: 1234567</p>") is None

    print("  [PASS] test_extract_otp_code")


def test_browser_fetcher_public():
    """Test BrowserFetcher with a simple public page."""
    from src.email.browser import BrowserFetcher

    fetcher = BrowserFetcher(state_path=".test_browser_state.json")
    try:
        html, error = fetcher.fetch_page("https://example.com")
        assert error is None, f"Unexpected error: {error}"
        assert html, "HTML should not be empty"
        assert "Example Domain" in html, "Should contain 'Example Domain'"
        print("  [PASS] test_browser_fetcher_public")
    finally:
        fetcher.close()
        # Clean up test state file if created
        state = Path(".test_browser_state.json")
        if state.exists():
            state.unlink()


# ── Integration tests ─────────────────────────────────────────────


def test_search_inbox():
    """Test EmailFetcher.search_inbox() — needs MS Graph credentials."""
    import asyncio
    from dotenv import load_dotenv

    load_dotenv()

    if not os.environ.get("MS_GRAPH_CLIENT_ID"):
        print("  [SKIP] test_search_inbox (no MS_GRAPH_CLIENT_ID)")
        return

    from src.email.fetcher import EmailFetcher

    async def _run():
        fetcher = EmailFetcher()
        messages = await fetcher.search_inbox(top=3)
        assert isinstance(messages, list), "Should return a list"
        if messages:
            msg = messages[0]
            assert "id" in msg
            assert "subject" in msg
            assert "sender" in msg
            assert "body_html" in msg
            print(f"  Found {len(messages)} messages in inbox")
        else:
            print("  Inbox is empty (still valid)")
        return True

    result = asyncio.run(_run())
    assert result
    print("  [PASS] test_search_inbox")


def test_browser_fetcher_medium():
    """Test BrowserFetcher on a Medium page — needs saved session."""
    from src.email.browser import BrowserFetcher

    state_path = ".browser_state.json"
    if not Path(state_path).exists():
        print("  [SKIP] test_browser_fetcher_medium (no .browser_state.json)")
        return

    fetcher = BrowserFetcher(state_path=state_path)
    try:
        html, error = fetcher.fetch_page(
            "https://medium.com/tag/programming/recommended"
        )
        assert error is None, f"Unexpected error: {error}"
        assert html, "HTML should not be empty"
        assert len(html) > 1000, "Medium page should have substantial content"
        print(f"  Fetched Medium page: {len(html)} chars")
        print("  [PASS] test_browser_fetcher_medium")
    finally:
        fetcher.close()


def test_medium_login():
    """Test full Medium OTP login — needs MS Graph + Medium account."""
    import asyncio
    from dotenv import load_dotenv

    load_dotenv()

    if not os.environ.get("MS_GRAPH_CLIENT_ID"):
        print("  [SKIP] test_medium_login (no MS_GRAPH_CLIENT_ID)")
        return

    medium_email = os.environ.get("MEDIUM_EMAIL") or os.environ.get(
        "MS_GRAPH_USER_EMAIL"
    )
    if not medium_email:
        print("  [SKIP] test_medium_login (no MEDIUM_EMAIL or MS_GRAPH_USER_EMAIL)")
        return

    from src.email.fetcher import EmailFetcher
    from src.email.browser import BrowserSession

    test_state_path = ".test_medium_state.json"

    async def _run():
        fetcher = EmailFetcher()
        session = BrowserSession(
            fetcher, state_path=test_state_path, medium_email=medium_email
        )
        result = await session.login_medium()
        return result

    result = asyncio.run(_run())
    state = Path(test_state_path)
    try:
        if result:
            assert state.exists(), "State file should be created on success"
            print("  Medium login succeeded!")
            print("  [PASS] test_medium_login")
        else:
            print("  Medium login failed (may need manual login)")
            print("  [FAIL] test_medium_login")
    finally:
        if state.exists():
            state.unlink()


# ── Runner ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== Browser Tests ===\n")

    print("Unit tests:")
    test_needs_browser()
    test_extract_otp_code()
    test_browser_fetcher_public()

    print("\nIntegration tests:")
    test_search_inbox()
    test_browser_fetcher_medium()
    test_medium_login()

    print("\nDone!")
