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
    r"|mailto:|javascript:"
    r"|apps\.apple\.com/|play\.google\.com/store"
    r"|medium\.com/m/|medium\.com/tag/"
    # Newsletter platforms
    r"|substack\.com/$|substack\.com/\?"
    r"|convertkit\.com|mailchimp\.com|campaign-archive"
    # Sponsor/referral tracking
    r"|sparkloop\.app|swapstack\.co|refind\.com"
    # Community/chat invites
    r"|discord\.gg/|discord\.com/invite/"
    r"|t\.me/|slack\.com/|chat\.whatsapp\.com"
    # Event platforms
    r"|meetup\.com|eventbrite\.com|lu\.ma/|tito\.io"
    # Help/support pages
    r"|help\.medium\.com|help\.substack\.com|/help[-_]?center|/faq\b|/support\b|zendesk\.com"
    # Feeds
    r"|/feed$|/rss$|/atom\.xml)",
    re.IGNORECASE,
)

# Default user agent for HTTP requests
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Boilerplate link text patterns (case-insensitive)
_BOILERPLATE_TEXT = re.compile(
    r"^(read more|continue reading|read the full"
    r"|follow|subscribe|sign up|sign in|log in"
    r"|view in browser|open in app|view online"
    r"|learn more|click here|download app|get the app"
    r"|manage preferences|update preferences"
    r"|share|tweet|post"
    # Navigation
    r"|home|about|contact|archive|past issues|all posts|explore"
    # Account/onboarding
    r"|login|register|sign up free|start writing|get started"
    # Sponsor/ads
    r"|advertise|sponsor|become a sponsor|promoted"
    # Social platform names
    r"|twitter|linkedin|youtube|discord|github"
    # Legal/misc
    r"|privacy|privacy policy|terms|terms of service|rss|podcast"
    # Newsletter actions
    r"|unsubscribe|view online|refer a friend|share this"
    # Footer/generic
    r"|here|click|powered by.*|beehiiv|submit"
    # Help/support
    r"|help center|help centre|faq|support|knowledge base)$",
    re.IGNORECASE,
)


def _is_boilerplate_text(text: str) -> bool:
    """Return True if the link text is a known boilerplate phrase or too short."""
    stripped = text.strip()
    if len(stripped) <= 2:
        return True
    if _BOILERPLATE_TEXT.match(stripped):
        return True
    # Link text that is just a URL
    if stripped.startswith(("http://", "https://")):
        return True
    # Event/meetup links (partial match)
    if _EVENT_TEXT.search(stripped):
        return True
    # Cross-promo newsletter links ("Programmer Weekly", "Founder Weekly", etc.)
    if _NEWSLETTER_PROMO_TEXT.match(stripped):
        return True
    return False


# Event/meetup link text (partial match — anywhere in the text)
_EVENT_TEXT = re.compile(
    r"\b(meetup|user group|hackathon)\b",
    re.IGNORECASE,
)

# Cross-promotion newsletter names ("X Weekly", "X Digest", "X Newsletter")
_NEWSLETTER_PROMO_TEXT = re.compile(
    r"^.+\s+(weekly|digest|newsletter|bulletin)$",
    re.IGNORECASE,
)


# Patterns that indicate a site error page rather than real content
_ERROR_PAGE_PATTERNS = re.compile(
    r"(500 Apolog|something went wrong on our end"
    r"|page is unavailable|502 Bad Gateway"
    r"|503 Service Unavailable|504 Gateway)",
    re.IGNORECASE,
)


def _is_error_page(text: str) -> bool:
    """Return True if extracted text looks like a site error page, not a real article."""
    if not text or len(text) > 500:
        return False  # Real articles are longer than 500 chars
    return bool(_ERROR_PAGE_PATTERNS.search(text))


# Generic non-article path suffixes (lowercase, no leading slash)
_NON_ARTICLE_PATHS = {
    "about", "contact", "privacy", "privacy-policy", "terms",
    "terms-of-service", "pricing", "login", "signin", "sign-in",
    "signup", "sign-up", "search", "archive", "archives",
    "careers", "jobs", "settings", "account", "profile",
    "help", "faq", "support", "docs", "documentation",
    "sponsor", "advertise", "newsletter", "subscribe",
}


def _is_non_article_url(url: str) -> bool:
    """
    Return True if the URL structurally looks like a non-article page.

    Catches author profiles, publication homepages, tag pages,
    social media profiles, app store links, domain roots,
    and common non-article paths.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    segments = [s for s in path.split("/") if s]

    # Domain roots — bare homepage with no meaningful path
    if not segments:
        return True

    # Medium: skip profiles (/@user), publication homepages (/pub-name),
    # tag pages (/tag/*), and internal pages (/m/*)
    if "medium.com" in hostname:
        if len(segments) == 1:
            # Single segment = profile (@user) or publication page
            return True
        if segments[0] == "tag":
            return True
        if segments[0] == "m":
            return True
        return False

    # Substack: only allow /p/ (actual posts)
    if "substack.com" in hostname:
        if segments and segments[0] == "p":
            return False
        return True

    # Beehiiv: tracking subdomains (link.mail.beehiiv.com) are redirects — let them through.
    # Only filter actual publication pages (keep /p/ article paths).
    if "beehiiv.com" in hostname:
        if hostname.startswith(("link.", "mail.")):
            return False  # tracking redirect, will be resolved later
        if segments and segments[0] == "p":
            return False
        return True

    # Twitter/X: skip profile pages (no /status/ in path)
    if hostname in ("twitter.com", "www.twitter.com", "x.com", "www.x.com"):
        if "/status/" not in path:
            return True
        return False

    # LinkedIn: skip /in/ (profiles) and /company/ pages
    if "linkedin.com" in hostname:
        if segments and segments[0] in ("in", "company"):
            return True
        return False

    # GitHub: skip user/org pages (1 segment, no repo)
    if hostname in ("github.com", "www.github.com"):
        if len(segments) <= 1:
            return True
        return False

    # YouTube: skip channels/playlists, allow only /watch (videos)
    if "youtube.com" in hostname:
        if segments and segments[0] in ("channel", "c", "playlist", "user"):
            return True
        if segments and segments[0].startswith("@"):
            return True
        return False

    # Reddit: skip bare subreddit pages, allow full post URLs
    if "reddit.com" in hostname:
        # /r/subreddit/comments/... is a post — allow it
        if len(segments) >= 4 and segments[0] == "r" and segments[2] == "comments":
            return False
        # /r/subreddit with no post = subreddit listing — skip
        if len(segments) <= 2 and segments[0] == "r":
            return True
        return False

    # App stores (belt-and-suspenders with _SKIP_URL_PATTERNS)
    if "apps.apple.com" in hostname or "play.google.com" in hostname:
        return True

    # Generic non-article paths (e.g. /about, /pricing, /careers)
    if len(segments) == 1 and segments[0].lower() in _NON_ARTICLE_PATHS:
        return True

    return False


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

        all_a_tags = soup.find_all("a", href=True)
        skipped = {"no_text": 0, "boilerplate_url": 0, "boilerplate_text": 0,
                    "non_article": 0, "non_http": 0, "empty": 0, "dupe": 0}

        for a_tag in all_a_tags:
            url = a_tag["href"].strip()

            # Skip empty, anchor-only, mailto, javascript
            if not url or url.startswith("#"):
                skipped["empty"] += 1
                continue

            # Skip boilerplate patterns
            if _SKIP_URL_PATTERNS.search(url):
                skipped["boilerplate_url"] += 1
                continue

            # Must be http/https
            if not url.startswith(("http://", "https://")):
                skipped["non_http"] += 1
                continue

            # Get link text, skip image-only or empty anchors
            link_text = a_tag.get_text(strip=True)
            if not link_text:
                skipped["no_text"] += 1
                continue

            # Skip boilerplate link text ("Read more", "Follow", etc.)
            if _is_boilerplate_text(link_text):
                skipped["boilerplate_text"] += 1
                continue

            # Skip non-article URLs (profiles, tag pages, etc.)
            if _is_non_article_url(url):
                skipped["non_article"] += 1
                continue

            # Dedupe within the same email
            if url in seen_urls:
                skipped["dupe"] += 1
                continue
            seen_urls.add(url)

            links.append({"url": url, "link_text": link_text})

        # Diagnostic logging when no links survive filtering
        if not links and all_a_tags:
            total = len(all_a_tags)
            print(f"    [debug] {total} <a> tags found, all filtered out: {skipped}")
            # Show first 5 filtered URLs for diagnosis
            shown = 0
            for a_tag in all_a_tags:
                href = a_tag.get("href", "").strip()
                text = a_tag.get_text(strip=True)[:60]
                if href and href.startswith("http"):
                    print(f"    [debug]   {href[:100]} | text='{text}'")
                    shown += 1
                    if shown >= 5:
                        break
        elif not all_a_tags:
            body_len = len(body_html) if body_html else 0
            print(f"    [debug] No <a> tags found in email body ({body_len} chars)")

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

        # Detect error pages masquerading as content (e.g. Medium soft-500)
        if _is_error_page(text):
            return {
                **base,
                "extraction_status": "error_page",
                "error": "site returned an error page instead of article content",
                "text_length": 0,
            }

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

        # Post-resolution filter: tracking URL may resolve to a non-article page
        if not resolve_error and _is_non_article_url(resolved_url):
            return None

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
            if result is None:
                continue  # Filtered by post-resolution URL check
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
            try:
                self._browser.close()
            except Exception:
                pass  # Playwright greenlet may be on a dead thread
