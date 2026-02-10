"""
Content Extractor for Newsletter Curator.

Parses newsletter email HTML to find article links, resolves tracking
redirects, and extracts article text via trafilatura.
"""

import concurrent.futures
import re
import threading
from urllib.parse import urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from .browser import needs_browser

# Patterns in URLs that indicate boilerplate (not article links)
_SKIP_URL_PATTERNS = re.compile(
    r"(unsubscribe|manage[-_]?preferences|view[-_]?in[-_]?browser"
    r"|email[-_]?preferences|opt[-_]?out|list[-_]?unsubscribe"
    r"|twitter\.com/intent|x\.com/intent"
    r"|facebook\.com/sharer|linkedin\.com/sharing"
    r"|mailto:|javascript:)",
    re.IGNORECASE,
)

# Default user agent for HTTP requests
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class ContentExtractor:
    """
    Extracts article content from newsletter emails.

    Usage:
        extractor = ContentExtractor()
        items = extractor.extract_from_email(body_html)
        for item in items:
            print(item["title"], item["text_length"])
        extractor.close()
    """

    def __init__(
        self,
        timeout: int = 15,
        max_redirects: int = 10,
        user_agent: str | None = None,
        browser_fetcher=None,
    ):
        ua = user_agent or _DEFAULT_UA
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
            headers={"User-Agent": ua},
        )
        self._browser = browser_fetcher
        self._browser_lock = threading.Lock()

    # ── Link parsing ──────────────────────────────────────────────

    def parse_links(self, body_html: str) -> list[dict]:
        """
        Extract article URLs from newsletter email HTML.

        Filters out boilerplate links (unsubscribe, social share, etc.),
        mailto/javascript links, and anchor-only links.

        Returns:
            List of dicts with keys: url, link_text
        """
        soup = BeautifulSoup(body_html, "html.parser")
        seen_urls = set()
        links = []

        for a_tag in soup.find_all("a", href=True):
            url = a_tag["href"].strip()

            # Skip empty, anchor-only, mailto, javascript
            if not url or url.startswith("#"):
                continue

            # Skip boilerplate patterns
            if _SKIP_URL_PATTERNS.search(url):
                continue

            # Must be http/https
            if not url.startswith(("http://", "https://")):
                continue

            # Get link text, skip image-only or empty anchors
            link_text = a_tag.get_text(strip=True)
            if not link_text:
                continue

            # Dedupe within the same email
            if url in seen_urls:
                continue
            seen_urls.add(url)

            links.append({"url": url, "link_text": link_text})

        return links

    # ── URL resolution ────────────────────────────────────────────

    def resolve_url(self, url: str) -> tuple[str, str | None]:
        """
        Follow tracking redirects to find the real URL.

        Tries HEAD first (faster), falls back to GET if HEAD fails.
        Uses browser for domains that block HTTP clients.

        Returns:
            Tuple of (resolved_url, error_or_none)
        """
        # Try browser first for known blocked domains
        if self._browser and needs_browser(url):
            try:
                with self._browser_lock:
                    return self._browser.resolve_url(url)
            except Exception:
                pass  # Fall through to HTTP

        resolved = None
        try:
            resp = self._client.head(url)
            if resp.status_code < 400:
                resolved = str(resp.url)
        except httpx.HTTPError:
            pass

        if not resolved:
            try:
                resp = self._client.get(url)
                if resp.status_code < 400:
                    resolved = str(resp.url)
            except httpx.HTTPError:
                pass

        if resolved:
            # If resolved URL is still on a tracking domain, try browser
            if self._browser and needs_browser(resolved):
                try:
                    with self._browser_lock:
                        return self._browser.resolve_url(url)
                except Exception:
                    pass
            return resolved, None

        # Everything failed
        if self._browser and needs_browser(url):
            try:
                with self._browser_lock:
                    return self._browser.resolve_url(url)
            except Exception as exc:
                return url, f"redirect_failed: {exc}"
        return url, "redirect_failed: HTTP 403 or blocked"

    # ── Article extraction ────────────────────────────────────────

    def extract_article(self, url: str) -> dict:
        """
        Fetch a URL and extract article text via trafilatura.

        Returns:
            Dict with title, author, date, description, text, sitename,
            hostname, extraction_status, error, text_length.
        """
        base = {
            "title": None,
            "author": None,
            "date": None,
            "description": None,
            "text": None,
            "sitename": None,
            "hostname": urlparse(url).hostname,
        }

        # Fetch the page
        html = None
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            html = resp.text
        except httpx.HTTPError as exc:
            # Browser fallback for known domains
            if self._browser and needs_browser(url):
                with self._browser_lock:
                    html, browser_error = self._browser.fetch_page(url)
                if browser_error or not html:
                    return {
                        **base,
                        "extraction_status": "fetch_failed",
                        "error": browser_error or "browser returned empty page",
                        "text_length": 0,
                    }
            else:
                return {
                    **base,
                    "extraction_status": "fetch_failed",
                    "error": str(exc),
                    "text_length": 0,
                }

        # Extract with trafilatura (2.0+ returns a Document object)
        doc = trafilatura.bare_extraction(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )

        if not doc or not getattr(doc, "text", None):
            return {
                **base,
                "extraction_status": "extraction_empty",
                "error": "trafilatura returned no text",
                "text_length": 0,
            }

        text = doc.text or ""
        return {
            "title": doc.title,
            "author": doc.author,
            "date": doc.date,
            "description": doc.description,
            "text": text,
            "sitename": doc.sitename,
            "hostname": urlparse(url).hostname,
            "extraction_status": "ok",
            "error": None,
            "text_length": len(text),
        }

    # ── Full pipeline ─────────────────────────────────────────────

    def _process_link(self, link: dict) -> dict:
        """
        Process a single link: resolve tracking redirect and extract article.

        Args:
            link: Dict with keys url, link_text.

        Returns:
            Dict with source_url, resolved_url, link_text, and article fields.
        """
        source_url = link["url"]
        link_text = link["link_text"]

        resolved_url, resolve_error = self.resolve_url(source_url)

        if resolve_error:
            return {
                "source_url": source_url,
                "resolved_url": resolved_url,
                "link_text": link_text,
                "title": None,
                "author": None,
                "date": None,
                "description": None,
                "text": None,
                "sitename": None,
                "hostname": urlparse(resolved_url).hostname,
                "extraction_status": "redirect_failed",
                "error": resolve_error,
                "text_length": 0,
            }

        article = self.extract_article(resolved_url)
        return {
            "source_url": source_url,
            "resolved_url": resolved_url,
            "link_text": link_text,
            **article,
        }

    def extract_from_email(self, body_html: str) -> list[dict]:
        """
        Full extraction pipeline: parse links -> resolve + extract in parallel -> dedupe.

        Args:
            body_html: The HTML body of a newsletter email.

        Returns:
            List of dicts per link (see module docstring for structure).
        """
        raw_links = self.parse_links(body_html)

        if not raw_links:
            return []

        # Process all links in parallel (I/O-bound HTTP requests)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._process_link, raw_links))

        # Post-resolution dedup (multiple tracking URLs may resolve to the same article)
        items = []
        seen_resolved = set()
        for result in results:
            resolved_url = result["resolved_url"]
            if resolved_url in seen_resolved:
                continue
            seen_resolved.add(resolved_url)
            items.append(result)

        return items

    # ── Stats ─────────────────────────────────────────────────────

    @staticmethod
    def summary(items: list[dict]) -> dict:
        """
        Compute batch stats from extraction results.

        Returns:
            Dict with total, ok, redirect_failed, fetch_failed,
            extraction_empty counts and avg_text_length.
        """
        counts = {
            "total": len(items),
            "ok": 0,
            "redirect_failed": 0,
            "fetch_failed": 0,
            "extraction_empty": 0,
        }
        total_text_len = 0

        for item in items:
            status = item.get("extraction_status", "")
            if status in counts:
                counts[status] += 1
            total_text_len += item.get("text_length", 0)

        counts["avg_text_length"] = (
            total_text_len // counts["ok"] if counts["ok"] > 0 else 0
        )
        return counts

    # ── Cleanup ───────────────────────────────────────────────────

    def close(self):
        """Close the httpx client session and browser if active."""
        self._client.close()
        if self._browser:
            self._browser.close()
