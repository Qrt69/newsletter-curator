"""
Browser-based fetching for Newsletter Curator.

Provides Playwright-based fallback for domains that block non-browser
requests (Medium, Beehiiv). Includes Medium OTP code login flow.

Two classes:
  - BrowserFetcher (sync) — used by ContentExtractor for page fetching
  - BrowserSession (async) — handles Medium login via emailed OTP code

Storage state file (.browser_state.json) bridges async login and sync fetching.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Domains that need browser-based fetching
BROWSER_DOMAINS = {"medium.com", "beehiiv.com"}


def _default_state_path() -> str:
    data_dir = os.environ.get("DATA_DIR", ".")
    return str(Path(data_dir) / ".browser_state.json")

_SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds
_OTP_TIMEOUT = 120  # seconds to wait for OTP code email
_OTP_POLL_INTERVAL = 5  # seconds between inbox polls


def needs_browser(url: str) -> bool:
    """Check if a URL belongs to a domain that needs browser-based fetching."""
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return False
    hostname = hostname.lower()
    for domain in BROWSER_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


class BrowserFetcher:
    """
    Sync Playwright-based page fetcher.

    Used as a fallback by ContentExtractor when httpx gets blocked (403).
    Lazy-launches Chromium only when actually needed.

    Usage:
        fetcher = BrowserFetcher()
        html, error = fetcher.fetch_page("https://medium.com/...")
        fetcher.close()
    """

    def __init__(self, state_path: str | None = None):
        self._state_path = state_path or _default_state_path()
        self._playwright = None
        self._browser = None

    def _ensure_browser(self):
        """Launch Playwright + Chromium on first use."""
        if self._browser:
            return
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:
            print(f"  [browser] Failed to launch Playwright: {exc}")
            self._playwright = None
            self._browser = None
            raise

    def _new_context(self):
        """Create a new browser context, loading storage state if available."""
        state_file = Path(self._state_path)
        if state_file.exists():
            return self._browser.new_context(storage_state=self._state_path)
        return self._browser.new_context()

    def fetch_page(self, url: str) -> tuple[str, str | None]:
        """
        Fetch a page using Playwright and return rendered HTML.

        Returns:
            Tuple of (html_content, error_or_none)
        """
        try:
            self._ensure_browser()
        except Exception as exc:
            return "", f"browser_launch_failed: {exc}"

        context = None
        try:
            context = self._new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait a bit for JS-rendered content
            page.wait_for_timeout(2000)
            html = page.content()
            return html, None
        except Exception as exc:
            return "", f"browser_fetch_failed: {exc}"
        finally:
            if context:
                context.close()

    def resolve_url(self, url: str) -> tuple[str, str | None]:
        """
        Navigate to a URL and return the final URL after all redirects.

        Returns:
            Tuple of (final_url, error_or_none)
        """
        try:
            self._ensure_browser()
        except Exception as exc:
            return url, f"browser_launch_failed: {exc}"

        context = None
        try:
            context = self._new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return page.url, None
        except Exception as exc:
            return url, f"browser_resolve_failed: {exc}"
        finally:
            if context:
                context.close()

    def close(self):
        """Close browser and Playwright."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None


class BrowserSession:
    """
    Async session manager for Medium OTP login.

    Uses async_playwright + EmailFetcher to:
    1. Navigate to Medium sign-in page
    2. Enter email and submit
    3. Poll inbox for OTP code email
    4. Type OTP code into the browser to complete auth
    5. Save storage state for BrowserFetcher to use

    Usage:
        session = BrowserSession(email_fetcher)
        logged_in = await session.ensure_logged_in()
    """

    def __init__(
        self,
        email_fetcher,
        state_path: str | None = None,
        medium_email: str | None = None,
    ):
        if state_path is None:
            state_path = _default_state_path()
        self._fetcher = email_fetcher
        self.state_path = state_path
        self._medium_email = (
            medium_email
            or os.environ.get("MEDIUM_EMAIL")
            or os.environ.get("MS_GRAPH_USER_EMAIL", "")
        )

    def has_valid_session(self) -> bool:
        """Check if storage state file exists and is less than 7 days old."""
        state_file = Path(self.state_path)
        if not state_file.exists():
            return False
        age = time.time() - state_file.stat().st_mtime
        return age < _SESSION_MAX_AGE

    async def ensure_logged_in(self) -> bool:
        """
        Ensure we have a valid Medium session.

        Returns True if session is valid (existing or newly created).
        Returns False if login failed.
        """
        if self.has_valid_session():
            print("  [browser] Existing Medium session is valid")
            return True

        print("  [browser] No valid session, attempting Medium login...")
        try:
            return await self.login_medium()
        except Exception as exc:
            print(f"  [browser] Medium login failed: {exc}")
            return False

    async def login_medium(self) -> bool:
        """
        Full Medium OTP code login flow.

        1. Open Medium sign-in page
        2. Enter email, submit
        3. Poll inbox for 6-digit OTP code
        4. Type code into the browser
        5. Save storage state
        """
        from playwright.async_api import async_playwright

        sent_after = _iso_now()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                # Navigate to Medium sign-in
                print("  [browser] Navigating to Medium sign-in...")
                await page.goto(
                    "https://medium.com/m/signin",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(2000)

                # Medium shows social login buttons first, then "Sign in with email"
                # Click "Sign in with email" to reveal the email input
                email_button = page.get_by_text("Sign in with email")
                try:
                    await email_button.click(timeout=10000)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass  # May already show email input

                # Look for email input field
                email_input = page.locator(
                    'input[type="email"], input[name="email"]'
                ).first
                try:
                    await email_input.wait_for(state="visible", timeout=5000)
                except Exception:
                    # Fallback: try any visible text input
                    email_input = page.get_by_role("textbox").first
                    try:
                        await email_input.wait_for(state="visible", timeout=3000)
                    except Exception:
                        print("  [browser] Could not find email input on Medium sign-in")
                        print("  [browser] Use --browser-login for manual login")
                        return False

                # Enter email and submit
                print(f"  [browser] Entering email: {self._medium_email}")
                await email_input.fill(self._medium_email)
                await page.wait_for_timeout(500)

                # Find and click submit/continue button
                submit = page.get_by_role("button", name="Continue")
                try:
                    await submit.click(timeout=3000)
                except Exception:
                    await email_input.press("Enter")

                await page.wait_for_timeout(2000)
                print("  [browser] Email submitted, waiting for OTP code...")

                # Poll for OTP code email
                otp_code = await self._poll_for_otp_code(sent_after)
                if not otp_code:
                    print("  [browser] Timed out waiting for OTP code email")
                    return False

                # Find the OTP input field and enter the code
                print(f"  [browser] Entering OTP code: {otp_code}")
                otp_input = page.locator(
                    'input[type="text"], input[type="number"], input[type="tel"]'
                ).first
                try:
                    await otp_input.wait_for(state="visible", timeout=10000)
                except Exception:
                    # Fallback: try any visible textbox
                    otp_input = page.get_by_role("textbox").first
                    try:
                        await otp_input.wait_for(state="visible", timeout=5000)
                    except Exception:
                        print("  [browser] Could not find OTP input field")
                        print("  [browser] Use --browser-login for manual login")
                        return False

                await otp_input.fill(otp_code)
                await page.wait_for_timeout(1000)

                # Submit the code — try button first, then Enter
                verify_btn = page.get_by_role("button", name="Complete sign in")
                try:
                    await verify_btn.click(timeout=3000)
                except Exception:
                    try:
                        verify_btn = page.get_by_role("button", name="Verify")
                        await verify_btn.click(timeout=3000)
                    except Exception:
                        await otp_input.press("Enter")

                # Wait for login to complete (page navigation)
                await page.wait_for_timeout(5000)

                # Save storage state
                await context.storage_state(path=self.state_path)
                print("  [browser] Session saved successfully")
                return True

            finally:
                await browser.close()

    async def _poll_for_otp_code(self, sent_after: str) -> str | None:
        """Poll inbox for Medium OTP code email."""
        elapsed = 0
        while elapsed < _OTP_TIMEOUT:
            await asyncio.sleep(_OTP_POLL_INTERVAL)
            elapsed += _OTP_POLL_INTERVAL
            print(f"  [browser] Polling inbox ({elapsed}s / {_OTP_TIMEOUT}s)...")

            try:
                messages = await self._fetcher.search_inbox(
                    sender_contains="noreply@medium.com",
                    received_after=sent_after,
                    top=5,
                )
            except Exception as exc:
                print(f"  [browser] Inbox poll error: {exc}")
                continue

            for msg in messages:
                code = self._extract_otp_code(msg.get("body_html", ""))
                if code:
                    return code

        return None

    @staticmethod
    def _extract_otp_code(body_html: str) -> str | None:
        """Extract 6-digit OTP code from Medium sign-in email HTML."""
        if not body_html:
            return None
        # Strip HTML tags and look for a standalone 6-digit number
        soup = BeautifulSoup(body_html, "html.parser")
        text = soup.get_text(" ", strip=True)
        match = re.search(r"\b(\d{6})\b", text)
        if match:
            return match.group(1)
        return None


async def manual_login(state_path: str | None = None):
    """
    Open a visible browser for manual Medium login.

    Used as a safety net when the automatic login fails
    (e.g., Medium changes their login page DOM).

    Usage:
        uv run python scripts/run_weekly.py --browser-login
    """
    if state_path is None:
        state_path = _default_state_path()
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://medium.com/m/signin", timeout=30000)

        print("\n" + "=" * 50)
        print("Manual Browser Login")
        print("=" * 50)
        print("A browser window has opened to Medium's sign-in page.")
        print("Please complete the login manually.")
        print("When you are fully logged in, press Enter here to save the session.")
        print("=" * 50)

        # Wait for user input (run in executor to not block event loop)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "\nPress Enter when logged in... ")

        await context.storage_state(path=state_path)
        print(f"Session saved to {state_path}")
        await browser.close()


def _iso_now() -> str:
    """Return current UTC time in ISO format for OData filtering."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
