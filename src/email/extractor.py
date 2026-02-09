"""
Content Extractor for Newsletter Curator.

Parses newsletter email HTML to find article links, resolves tracking
redirects, and extracts article text via trafilatura.
"""

import re
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

        Returns:
            Tuple of (resolved_url, error_or_none)
        """
        try:
            resp = self._client.head(url)
            return str(resp.url), None
        except httpx.HTTPError:
            pass

        # Fallback to GET
        try:
            resp = self._client.get(url)
            return str(resp.url), None
        except httpx.HTTPError as exc:
            # Browser fallback for known domains
            if self._browser and needs_browser(url):
                return self._browser.resolve_url(url)
            return url, f"redirect_failed: {exc}"

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

    def extract_from_email(self, body_html: str) -> list[dict]:
        """
        Full extraction pipeline: parse links -> resolve -> dedupe -> extract.

        Args:
            body_html: The HTML body of a newsletter email.

        Returns:
            List of dicts per link (see module docstring for structure).
        """
        raw_links = self.parse_links(body_html)
        items = []
        seen_resolved = set()

        for link in raw_links:
            source_url = link["url"]
            link_text = link["link_text"]

            # Resolve tracking redirects
            resolved_url, resolve_error = self.resolve_url(source_url)

            # Dedupe on resolved URL
            if resolved_url in seen_resolved:
                continue
            seen_resolved.add(resolved_url)

            if resolve_error:
                items.append({
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
                })
                continue

            # Extract article content
            article = self.extract_article(resolved_url)
            items.append({
                "source_url": source_url,
                "resolved_url": resolved_url,
                "link_text": link_text,
                **article,
            })

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
