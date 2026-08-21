"""
Tests for browser-based fetching (Playwright integration).

Unit tests run without external services.
Integration tests need Playwright + optionally MS Graph credentials.

Usage:
    uv run python tests/test_browser.py
"""

import os
import sys
import time
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


def _pool_of_fakes(size, fake_cls):
    """Build a BrowserPool of stub fetchers, so no Chromium is launched."""
    import src.email.browser as browser_mod

    real = browser_mod.BrowserFetcher
    browser_mod.BrowserFetcher = fake_cls
    try:
        return browser_mod.BrowserPool(size=size)
    finally:
        browser_mod.BrowserFetcher = real


def test_browser_pool_calls_run_in_parallel():
    """Pool of N must run N calls at once — a serialized pool trips the barrier."""
    import threading
    import concurrent.futures

    barrier = threading.Barrier(3, timeout=5)

    class FakeFetcher:
        is_wedged = False

        def __init__(self, state_path=None):
            pass

        def resolve_url(self, url):
            barrier.wait()  # BrokenBarrierError if the calls are serialized
            return url, None

    pool = _pool_of_fakes(3, FakeFetcher)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
        results = [f.result() for f in [
            workers.submit(pool.resolve_url, f"https://example.com/{i}") for i in range(3)
        ]]

    assert sorted(u for u, _ in results) == [f"https://example.com/{i}" for i in range(3)]
    print("  [PASS] test_browser_pool_calls_run_in_parallel")


def test_browser_pool_bounds_concurrency():
    """More callers than fetchers must queue, never launch extra Chromiums."""
    import threading
    import concurrent.futures

    lock = threading.Lock()
    live = 0
    peak = 0

    class FakeFetcher:
        is_wedged = False

        def __init__(self, state_path=None):
            pass

        def fetch_page(self, url, retries=2, retry_delay=5.0):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1
            return "<html></html>", None

    pool = _pool_of_fakes(2, FakeFetcher)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
        for f in [workers.submit(pool.fetch_page, "https://medium.com/p") for _ in range(8)]:
            f.result()

    assert peak <= 2, f"expected at most 2 concurrent browser calls, saw {peak}"
    print("  [PASS] test_browser_pool_bounds_concurrency")


def test_browser_pool_closes_every_fetcher():
    """One failing close must not leave the other Chromiums running."""
    closed = []

    class FakeFetcher:
        is_wedged = False

        def __init__(self, state_path=None):
            self.index = len(closed)

        def close(self):
            closed.append(self)
            if len(closed) == 1:
                raise RuntimeError("close failed")

    pool = _pool_of_fakes(3, FakeFetcher)
    pool.close()

    assert len(closed) == 3, f"expected all 3 fetchers closed, got {len(closed)}"
    pool.close()  # idempotent
    assert len(closed) == 3

    print("  [PASS] test_browser_pool_closes_every_fetcher")


def test_run_deadline_wedges_fetcher():
    """A Playwright call that never returns must not park the run forever."""
    import threading
    from src.email.browser import BrowserFetcher, BrowserTimeout

    released = threading.Event()
    killed = []

    fetcher = BrowserFetcher()
    fetcher._kill_tracked_pids = lambda: (killed.append(True), released.set())

    try:
        fetcher._run(lambda: released.wait(10), timeout=0.2)
    except BrowserTimeout:
        pass
    else:
        raise AssertionError("a call past its deadline should raise BrowserTimeout")

    assert killed, "wedging must kill the browser processes — that is what unblocks it"
    assert fetcher.is_wedged

    # A wedged fetcher is unusable, and says so instead of blocking again.
    html, error = fetcher.fetch_page("https://medium.com/p")
    assert html == "" and "browser_unavailable" in error, error
    url, error = fetcher.resolve_url("https://medium.com/p")
    assert url == "https://medium.com/p" and "browser_unavailable" in error, error

    fetcher.close()  # Must not queue behind the dead call
    released.set()

    print("  [PASS] test_run_deadline_wedges_fetcher")


def test_fetch_page_reports_timeout():
    """A hung fetch returns an error tuple, so extraction of the link continues."""
    import threading
    from src.email import browser as browser_mod

    released = threading.Event()
    fetcher = browser_mod.BrowserFetcher()
    fetcher._kill_tracked_pids = lambda: released.set()
    fetcher._fetch_page = lambda url, retries, retry_delay: released.wait(10)

    real = browser_mod._fetch_timeout
    browser_mod._fetch_timeout = lambda retries, retry_delay: 0.2
    try:
        html, error = fetcher.fetch_page("https://medium.com/p")
    finally:
        browser_mod._fetch_timeout = real
        released.set()

    assert html == "", html
    assert error and error.startswith("browser_timeout:"), error
    assert fetcher.is_wedged

    print("  [PASS] test_fetch_page_reports_timeout")


def test_browser_pool_replaces_wedged_fetcher():
    """A wedged fetcher must leave the pool without shrinking it."""
    from src.email import browser as browser_mod

    class FakeFetcher:
        def __init__(self, state_path=None):
            self.is_wedged = False
            self.calls = 0

        def fetch_page(self, url, retries=2, retry_delay=5.0):
            self.calls += 1
            self.is_wedged = True  # Every call hangs
            return "", "browser_timeout: hung"

    real = browser_mod.BrowserFetcher
    browser_mod.BrowserFetcher = FakeFetcher  # Also covers the replacements
    try:
        pool = browser_mod.BrowserPool(size=2)
        wedged = list(pool._fetchers)

        for _ in range(4):
            pool.fetch_page("https://medium.com/p")
    finally:
        browser_mod.BrowserFetcher = real

    assert pool.size == 2, f"pool shrank to {pool.size}"
    assert not any(f.is_wedged for f in pool._fetchers), "a wedged fetcher stayed in the pool"
    assert all(f.calls <= 1 for f in wedged), "a wedged fetcher was handed out again"

    print("  [PASS] test_browser_pool_replaces_wedged_fetcher")


def test_browser_pool_size_validated():
    """A pool of zero would silently disable the browser fallback."""
    from src.email.browser import BrowserPool

    for bad in (0, -1):
        try:
            BrowserPool(size=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"BrowserPool(size={bad}) should raise ValueError")

    print("  [PASS] test_browser_pool_size_validated")


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
    test_browser_pool_calls_run_in_parallel()
    test_browser_pool_bounds_concurrency()
    test_browser_pool_closes_every_fetcher()
    test_browser_pool_size_validated()
    test_run_deadline_wedges_fetcher()
    test_fetch_page_reports_timeout()
    test_browser_pool_replaces_wedged_fetcher()
    test_browser_fetcher_public()

    print("\nIntegration tests:")
    test_search_inbox()
    test_browser_fetcher_medium()
    test_medium_login()

    print("\nDone!")
