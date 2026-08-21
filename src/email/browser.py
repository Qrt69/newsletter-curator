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
import concurrent.futures
import contextlib
import logging
import os
import queue
import re
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Child of the "pipeline" logger, so messages land in the pipeline log file.
logger = logging.getLogger("pipeline.browser")

# Domains that need browser-based fetching
BROWSER_DOMAINS = {"medium.com", "beehiiv.com"}

# Deadlines for a whole Playwright call, enforced from the calling thread.
# Only page.goto() carries a timeout of its own (30s); new_context(), new_page(),
# page.content() and context.close() have none, so a Chromium that can no longer
# fork a renderer parks the call — and with it the run — forever. These are
# ceilings on the worst *legitimate* case, not the expected duration.
_RESOLVE_TIMEOUT = 90.0
_CLOSE_TIMEOUT = 60.0


def _fetch_timeout(retries: int, retry_delay: float) -> float:
    """Ceiling for one fetch_page: every attempt's goto plus its retry sleep."""
    return (retries + 1) * 45.0 + retries * retry_delay + 15.0


class BrowserTimeout(Exception):
    """A Playwright call blew its deadline; the fetcher that ran it is dead."""


def _default_state_path() -> str:
    data_dir = os.environ.get("DATA_DIR", ".")
    return str(Path(data_dir) / ".browser_state.json")

_SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds
_OTP_TIMEOUT = 120  # seconds to wait for OTP code email
_OTP_POLL_INTERVAL = 5  # seconds between inbox polls

# Stealth: Chromium launch args to avoid bot detection (Cloudflare, etc.)
_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
]

# Stealth: realistic browser context settings
_CONTEXT_OPTIONS = {
    "user_agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "viewport": {"width": 1920, "height": 1080},
    "locale": "en-US",
}


def _descendant_pids(root: int) -> set[int]:
    """
    PIDs descending from `root`, read straight from /proc (Linux only).

    Used to remember which processes a Chromium launch spawned, so they can be
    killed if the graceful close ever fails. Returns an empty set off Linux.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return set()

    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            # Format: "pid (comm) state ppid ..." — comm may contain spaces and
            # parens, so split after the *last* closing paren.
            fields = stat[stat.rindex(")") + 2:].split()
            ppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))

    found: set[int] = set()
    stack = [root]
    while stack:
        for pid in children.get(stack.pop(), []):
            if pid not in found:
                found.add(pid)
                stack.append(pid)
    return found


def _driver_pid(playwright) -> int | None:
    """
    PID of the node driver behind a started Playwright, or None.

    Everything Chromium spawns descends from it, so it is the one handle that
    identifies *this* fetcher's processes. Diffing /proc before and after a
    launch cannot: concurrent launches in a pool see each other's children and
    every fetcher ends up claiming all of them.
    """
    try:
        return playwright._impl_obj._connection._transport._proc.pid
    except Exception:
        logger.warning("[browser] could not determine driver pid", exc_info=True)
        return None


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

    Every Playwright call runs on one dedicated, long-lived owner thread.
    Playwright's sync API is thread-affine: whichever thread started the driver
    must also use and close it. Callers arrive from the extractor's per-email
    ThreadPoolExecutor, whose threads die when that pool is torn down after each
    email — so launching there and closing from the main thread raised
    "cannot switch to a different thread (which happens to have exited)" and
    leaked a whole Chromium tree per run.

    Usage:
        fetcher = BrowserFetcher()
        html, error = fetcher.fetch_page("https://medium.com/...")
        fetcher.close()
    """

    def __init__(self, state_path: str | None = None):
        self._state_path = state_path or _default_state_path()
        self._playwright = None
        self._browser = None
        self._owner: concurrent.futures.ThreadPoolExecutor | None = None
        self._owner_lock = threading.Lock()
        self._closed = False
        self._wedged = False
        # Processes spawned by the launch, killed if close() fails or a call
        # blows its deadline. _driver_pid is the root of that tree.
        self._driver_pid: int | None = None
        self._child_pids: list[int] = []

    # ── Owner thread ──────────────────────────────────────────────

    def _run(self, fn, *args, timeout: float):
        """
        Run a Playwright call on the owner thread and return its result.

        `timeout` is a hard deadline: the sync API offers no way to interrupt a
        call, so a hang here is only escapable by killing the browser processes.
        That is what _wedge() does, and why a timeout retires the fetcher rather
        than freeing it for the next caller — its owner thread stays parked
        inside the dead call until the kill lands.
        """
        with self._owner_lock:
            if self._closed or self._wedged:
                raise RuntimeError("BrowserFetcher is closed")
            if self._owner is None:
                self._owner = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="playwright-owner",
                )
            owner = self._owner
        try:
            return owner.submit(fn, *args).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            self._wedge(f"{getattr(fn, '__name__', fn)} exceeded {timeout:.0f}s")
            raise BrowserTimeout(
                f"Playwright call did not return within {timeout:.0f}s"
            ) from None

    def _wedge(self, detail: str):
        """
        Give up on this fetcher: kill its browser tree and retire it.

        The kill is not cleanup for later — it is what unblocks the owner
        thread. Once the driver is gone the parked call raises, the thread
        unwinds, and the executor (already shut down here) lets it exit.
        """
        with self._owner_lock:
            if self._wedged:
                return
            self._wedged = True
            self._closed = True  # close() must not queue behind the dead call
            owner, self._owner = self._owner, None

        logger.error("[browser] wedged: %s — killing browser processes", detail)
        print(f"  [browser] call hung ({detail}), killing browser processes")
        self._kill_tracked_pids()
        # Both objects live on a thread we no longer control; dropping the
        # references keeps a later close() from touching them.
        self._browser = None
        self._playwright = None
        if owner is not None:
            owner.shutdown(wait=False)

    def _kill_tracked_pids(self):
        """SIGKILL the processes we spawned, including any added since launch."""
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        pids = set(self._child_pids)
        if self._driver_pid:
            # Renderers and utility processes appear long after the launch.
            pids |= _descendant_pids(self._driver_pid) | {self._driver_pid}
        # Lowest pid first: driver, then browser, then the children it
        # would otherwise restart on their way out.
        for pid in sorted(pids):
            try:
                os.kill(pid, sig)
                logger.warning("[browser] killed leaked process %d", pid)
            except OSError:
                pass  # Already gone, or not ours to kill
        self._child_pids = []
        self._driver_pid = None

    # ── Playwright (owner thread only) ────────────────────────────

    def _ensure_browser(self):
        """Launch Playwright + Chromium on first use with stealth args."""
        if self._browser:
            return
        before = _descendant_pids(os.getpid())
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True, args=_BROWSER_ARGS,
            )
        except Exception as exc:
            print(f"  [browser] Failed to launch Playwright: {exc}")
            logger.exception("[browser] Playwright launch failed")
            self._playwright = None
            self._browser = None
            raise
        self._driver_pid = _driver_pid(self._playwright)
        if self._driver_pid:
            self._child_pids = sorted(
                {self._driver_pid} | _descendant_pids(self._driver_pid)
            )
        else:
            # Fallback: racy in a pool, but better than no handle at all.
            self._child_pids = sorted(_descendant_pids(os.getpid()) - before)
        logger.info(
            "[browser] launched on thread '%s' (driver %s, pids: %s)",
            threading.current_thread().name,
            self._driver_pid or "unknown",
            self._child_pids or "unknown",
        )

    def _new_context(self):
        """Create a new browser context with stealth settings and stored session."""
        opts = dict(_CONTEXT_OPTIONS)
        state_file = Path(self._state_path)
        if state_file.exists():
            opts["storage_state"] = self._state_path
        return self._browser.new_context(**opts)

    def _fetch_page(
        self, url: str, retries: int = 2, retry_delay: float = 5.0,
    ) -> tuple[str, str | None]:
        try:
            self._ensure_browser()
        except Exception as exc:
            return "", f"browser_launch_failed: {exc}"

        last_error = None
        for attempt in range(retries + 1):
            context = None
            try:
                context = self._new_context()
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Retry on server errors (5xx)
                if response and response.status >= 500:
                    last_error = f"HTTP {response.status}"
                    if attempt < retries:
                        print(f"  [browser] {last_error}, retrying ({attempt + 1}/{retries})...")
                        time.sleep(retry_delay)
                        continue
                    return "", f"browser_fetch_failed: {last_error} after {retries + 1} attempts"

                # Wait a bit for JS-rendered content
                page.wait_for_timeout(2000)
                html = page.content()
                return html, None
            except Exception as exc:
                last_error = str(exc)
                if attempt < retries:
                    time.sleep(retry_delay)
                    continue
                return "", f"browser_fetch_failed: {exc}"
            finally:
                if context:
                    context.close()

        return "", f"browser_fetch_failed: {last_error}"

    def _resolve_url(self, url: str) -> tuple[str, str | None]:
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

    def _close(self):
        """Close browser and Playwright (owner thread)."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    # ── Public API (any thread) ───────────────────────────────────

    @property
    def is_wedged(self) -> bool:
        """Whether a call hung here; such a fetcher can never be used again."""
        return self._wedged

    def fetch_page(
        self, url: str, retries: int = 2, retry_delay: float = 5.0,
    ) -> tuple[str, str | None]:
        """
        Fetch a page using Playwright and return rendered HTML.

        Retries on 5xx server errors (common with Medium transient failures).

        Returns:
            Tuple of (html_content, error_or_none)
        """
        try:
            return self._run(
                self._fetch_page, url, retries, retry_delay,
                timeout=_fetch_timeout(retries, retry_delay),
            )
        except BrowserTimeout as exc:
            return "", f"browser_timeout: {exc}"
        except RuntimeError as exc:
            return "", f"browser_unavailable: {exc}"

    def resolve_url(self, url: str) -> tuple[str, str | None]:
        """
        Navigate to a URL and return the final URL after all redirects.

        Returns:
            Tuple of (final_url, error_or_none)
        """
        try:
            return self._run(self._resolve_url, url, timeout=_RESOLVE_TIMEOUT)
        except BrowserTimeout as exc:
            return url, f"browser_timeout: {exc}"
        except RuntimeError as exc:
            return url, f"browser_unavailable: {exc}"

    def close(self):
        """
        Close browser and Playwright, then retire the owner thread.

        The close itself runs on the owner thread — the only thread allowed to
        touch the driver. If it still fails or hangs, the processes recorded at
        launch are killed so nothing survives the run.
        """
        with self._owner_lock:
            if self._closed:
                return
            self._closed = True
            owner, self._owner = self._owner, None

        if owner is None:
            return  # Browser was never launched

        try:
            owner.submit(self._close).result(timeout=_CLOSE_TIMEOUT)
        except Exception:
            logger.exception("[browser] close failed; killing browser processes")
            print("  [browser] close failed, killing browser processes")
            self._kill_tracked_pids()
        else:
            self._child_pids = []
        finally:
            owner.shutdown(wait=False)


class BrowserPool:
    """
    A fixed set of BrowserFetchers, leased out one caller at a time.

    Drop-in for a single BrowserFetcher — same resolve_url / fetch_page / close
    surface — so ContentExtractor does not care which one it was handed.

    Why a pool of fetchers instead of one fetcher plus a lock: Playwright's sync
    API is thread-affine, so a fetcher must be launched, used and closed on its
    own owner thread. That constraint is per instance, not global. K instances
    means K owner threads and K Chromiums, each still closed by the thread that
    launched it — no orphans — while K calls run at once.

    Running every call through one instance is what made extraction take 16
    minutes for 30 emails: almost every newsletter link is a beehiiv tracking
    URL that httpx cannot follow (403), so resolving it costs a full browser
    navigation. Measured on the VPS: 1.2s per link serial, 0.4s with three
    fetchers (3.0x), ~115MB RSS per launched instance, released on close.

    Usage:
        pool = BrowserPool(size=4)
        html, error = pool.fetch_page("https://medium.com/...")
        pool.close()
    """

    def __init__(self, size: int = 4, state_path: str | None = None):
        if size < 1:
            raise ValueError(f"BrowserPool size must be at least 1, got {size}")
        self._state_path = state_path
        self._fetchers = [BrowserFetcher(state_path=state_path) for _ in range(size)]
        self._fetchers_lock = threading.Lock()
        # LIFO, so a run with only a handful of browser links keeps reusing the
        # same warm fetcher and the other Chromiums are never launched at all
        # (BrowserFetcher launches lazily, on first use).
        self._idle: queue.LifoQueue = queue.LifoQueue()
        for fetcher in self._fetchers:
            self._idle.put(fetcher)
        self._closed = False

    @property
    def size(self) -> int:
        """Number of fetchers in the pool (launched or not)."""
        return len(self._fetchers)

    @contextlib.contextmanager
    def _lease(self):
        """
        Borrow a fetcher, blocking while all of them are busy.

        A fetcher that wedged is not handed back out — its owner thread is
        parked in a dead Playwright call — but the pool must not shrink either,
        or a run that hits four hangs would be left with no browser at all. So
        it is swapped for a fresh, unlaunched one, which costs a Chromium
        launch on first use and nothing until then.
        """
        fetcher = self._idle.get()
        try:
            yield fetcher
        finally:
            if fetcher.is_wedged and not self._closed:
                fetcher = self._replace(fetcher)
            self._idle.put(fetcher)

    def _replace(self, wedged: BrowserFetcher) -> BrowserFetcher:
        """Swap a wedged fetcher for a fresh one, so close() still covers it."""
        fresh = BrowserFetcher(state_path=self._state_path)
        with self._fetchers_lock:
            try:
                self._fetchers[self._fetchers.index(wedged)] = fresh
            except ValueError:
                self._fetchers.append(fresh)  # Already swapped out; keep size
        logger.warning("[pool] replaced a wedged fetcher with a fresh one")
        return fresh

    def fetch_page(
        self, url: str, retries: int = 2, retry_delay: float = 5.0,
    ) -> tuple[str, str | None]:
        """Fetch a page on any free fetcher. See BrowserFetcher.fetch_page."""
        with self._lease() as fetcher:
            return fetcher.fetch_page(url, retries=retries, retry_delay=retry_delay)

    def resolve_url(self, url: str) -> tuple[str, str | None]:
        """Resolve redirects on any free fetcher. See BrowserFetcher.resolve_url."""
        with self._lease() as fetcher:
            return fetcher.resolve_url(url)

    def close(self):
        """
        Close every fetcher, each on its own owner thread.

        Closes run concurrently: a single close can hang for up to 60s before it
        falls back to killing the tracked PIDs, and paying that serially for
        every fetcher would stall the end of a run.
        """
        if self._closed:
            return
        self._closed = True

        with self._fetchers_lock:
            fetchers = list(self._fetchers)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(fetchers), thread_name_prefix="browser-pool-close",
        ) as pool:
            for future in [pool.submit(f.close) for f in fetchers]:
                try:
                    future.result()
                except Exception:
                    # BrowserFetcher.close() already kills its own processes on
                    # failure; never let one bad fetcher skip the others.
                    logger.exception("[pool] fetcher close failed")


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
            browser = await p.chromium.launch(
                headless=True, args=_BROWSER_ARGS,
            )
            context = await browser.new_context(**_CONTEXT_OPTIONS)
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
        browser = await p.chromium.launch(headless=False, args=_BROWSER_ARGS)
        context = await browser.new_context(**_CONTEXT_OPTIONS)
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
