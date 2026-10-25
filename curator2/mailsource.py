"""
Where the newsletters come from. OutlookSource reuses the parts of the old
curator that work well (Microsoft Graph for the mailbox, the browser pool for
beehiiv/Medium links). FakeSource feeds tests.
"""

from __future__ import annotations

import logging

log = logging.getLogger("curator2.mail")


class OutlookSource:
    def __init__(self, browser_pool_size: int = 4):
        from src.email.fetcher import EmailFetcher  # imported here so tests need no Graph SDK

        self.fetcher = EmailFetcher()
        self.pool_size = browser_pool_size
        self._extractor = None

    async def fetch(self) -> list[dict]:
        emails = await self.fetcher.fetch_emails()
        await self._start_extractor(emails)
        return emails

    async def _start_extractor(self, emails: list[dict]):
        from src.email.browser import BrowserPool, BrowserSession, needs_browser
        from src.email.extractor import ContentExtractor

        needs = False
        scanner = ContentExtractor()
        try:
            for e in emails:
                if any(needs_browser(l["url"]) for l in scanner.parse_links(e["body_html"])):
                    needs = True
                    break
        finally:
            scanner.close()
        state_path = None
        if needs:
            try:
                session = BrowserSession(self.fetcher)
                if await session.ensure_logged_in():
                    state_path = session.state_path
            except Exception as exc:  # a failed Medium login must not stop the run
                log.warning("Browser login skipped: %s", exc)
        pool = BrowserPool(size=self.pool_size, state_path=state_path) if state_path else BrowserPool(size=self.pool_size)
        self._extractor = ContentExtractor(browser_fetcher=pool)

    def extract(self, email: dict) -> list[dict]:
        return self._extractor.extract_from_email(email["body_html"])

    async def move(self, email_id: str):
        await self.fetcher.move_to_processed(email_id)

    def close(self):
        if self._extractor:
            self._extractor.close()


class FakeSource:
    """emails: [{'id', 'subject', 'sender', 'items': [extractor-like dicts]}]"""

    def __init__(self, emails: list[dict], fail_extract: set[str] | None = None):
        self.emails = emails
        self.moved: list[str] = []
        self.extract_calls: list[str] = []
        self.fail_extract = fail_extract or set()

    async def fetch(self) -> list[dict]:
        return [dict(e, body_html="") for e in self.emails]

    def extract(self, email: dict) -> list[dict]:
        self.extract_calls.append(email["id"])
        if email["id"] in self.fail_extract:
            raise RuntimeError("extraction failed")
        return [dict(i) for i in email["items"]]

    async def move(self, email_id: str):
        self.moved.append(email_id)

    def close(self):
        pass
