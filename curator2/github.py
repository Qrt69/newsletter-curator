"""Small, polite look-ups of a repo's vital signs on GitHub (no login needed)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("curator2.github")


class GitHub:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "newsletter-curator"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(
            base_url="https://api.github.com", headers=headers, timeout=10.0, transport=transport
        )
        self.blocked = False

    def close(self):
        self.client.close()

    def repo(self, owner_repo: str) -> dict | None:
        """{'stars', 'days_idle', 'archived', 'license', 'description'} or None if unknown."""
        if self.blocked:
            return None
        try:
            r = self.client.get(f"/repos/{owner_repo}")
        except httpx.HTTPError as exc:
            log.info("GitHub lookup failed for %s: %s", owner_repo, exc)
            return None
        if r.status_code in (403, 429):
            self.blocked = True  # rate limit: stop asking for the rest of the run
            return None
        if r.status_code != 200:
            return None
        d = r.json()
        pushed = d.get("pushed_at")
        idle = 0
        if pushed:
            try:
                dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                idle = (datetime.now(timezone.utc) - dt).days
            except ValueError:
                pass
        return {
            "stars": int(d.get("stargazers_count") or 0),
            "days_idle": idle,
            "archived": bool(d.get("archived")),
            "license": ((d.get("license") or {}).get("spdx_id") or ""),
            "description": d.get("description") or "",
        }
