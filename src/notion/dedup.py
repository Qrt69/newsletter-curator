"""
Dedup index for Newsletter Curator.

Loads all entries from all 14 Notion databases into a searchable
in-memory index for fast duplicate detection.
"""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from rapidfuzz import fuzz

from .client import DATABASES, NotionClient

_CACHE_MAX_AGE = 24 * 60 * 60  # 24 hours


def _cache_file() -> Path:
    data_dir = os.environ.get("DATA_DIR", ".")
    return Path(data_dir) / ".dedup_cache.json"


def _normalize_url(raw_url: str | None) -> str | None:
    """
    Normalize a URL for comparison.

    Strips protocol, www., trailing slashes, query params, and fragments.
    Example: "https://www.github.com/foo/bar?ref=abc" → "github.com/foo/bar"
    """
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}" if host else None


class DedupIndex:
    """
    In-memory index of all Notion database entries for duplicate detection.

    Usage:
        nc = NotionClient()
        index = DedupIndex(nc)
        index.load()                    # from cache, or builds fresh
        results = index.search_by_name("Marimo", threshold=80)
        exists = index.exists("Marimo")
    """

    def __init__(self, client: NotionClient):
        self._client = client
        self._entries: list[dict] = []
        self._url_map: dict[str, list[int]] = {}  # normalized_url → entry indices

    def build(self) -> None:
        """Fetch all entries from all 14 databases and build the index."""
        self._entries = []
        self._url_map = {}

        db_names = list(DATABASES.keys())
        total = len(db_names)
        print("Building dedup index...")

        for i, db_name in enumerate(db_names, 1):
            # Discover the title and url property names from the schema
            schema = self._client.get_database_schema(db_name)
            title_prop = None
            url_prop = None
            for prop_name, prop_type in schema.items():
                if prop_type == "title":
                    title_prop = prop_name
                if prop_type == "url" and url_prop is None:
                    url_prop = prop_name

            if not title_prop:
                print(f"  [{i}/{total}] {db_name}: skipped (no title property)")
                continue

            pages = self._client.query_database(db_name)
            count = 0
            for page in pages:
                name = page.get(title_prop, "")
                if not name:
                    continue
                raw_url = page.get(url_prop) if url_prop else None
                normalized = _normalize_url(raw_url)

                entry = {
                    "id": page["id"],
                    "name": name,
                    "name_lower": name.lower(),
                    "url": raw_url,
                    "url_normalized": normalized,
                    "database": db_name,
                }
                idx = len(self._entries)
                self._entries.append(entry)

                if normalized:
                    self._url_map.setdefault(normalized, []).append(idx)

                count += 1

            print(f"  [{i}/{total}] {db_name}: {count} entries")

        print(f"Index built: {len(self._entries)} entries from {total} databases.")
        self._save_cache()

    def load(self) -> None:
        """Load index from cache file, or build fresh if cache is missing or stale."""
        cache_file = _cache_file()
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < _CACHE_MAX_AGE:
                print(f"Loading dedup index from {cache_file}...")
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._entries = data["entries"]
                self._rebuild_url_map()
                print(f"Loaded {len(self._entries)} entries from cache "
                      f"(built {data.get('timestamp', 'unknown')}).")
            else:
                print(f"Cache expired ({age / 3600:.1f}h old), rebuilding...")
                self.build()
        else:
            self.build()

    def search_by_name(self, name: str, threshold: int = 80) -> list[dict]:
        """
        Fuzzy search for entries by name.

        Args:
            name: The name to search for.
            threshold: Minimum match score (0-100). Default 80.

        Returns:
            List of matching entries with a "score" field, sorted best-first.
        """
        results = []
        for entry in self._entries:
            score = fuzz.token_sort_ratio(name.lower(), entry["name_lower"])
            if score >= threshold:
                results.append({
                    "name": entry["name"],
                    "database": entry["database"],
                    "id": entry["id"],
                    "score": score,
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    def search_by_url(self, url: str) -> list[dict]:
        """
        Exact search for entries by normalized URL.

        Args:
            url: The URL to search for.

        Returns:
            List of matching entries.
        """
        normalized = _normalize_url(url)
        if not normalized:
            return []
        indices = self._url_map.get(normalized, [])
        return [
            {
                "name": self._entries[i]["name"],
                "database": self._entries[i]["database"],
                "id": self._entries[i]["id"],
                "url": self._entries[i]["url"],
            }
            for i in indices
        ]

    def exists(self, name: str, threshold: int = 80) -> bool:
        """Check if an entry with a similar name already exists."""
        return len(self.search_by_name(name, threshold)) > 0

    def search(
        self,
        name: str | None = None,
        url: str | None = None,
        threshold: int = 80,
    ) -> list[dict]:
        """
        Combined search by name and/or URL.

        If both are provided, URL matches take priority (exact match),
        then name matches are appended (excluding duplicates).
        """
        seen_ids: set[str] = set()
        results: list[dict] = []

        if url:
            for match in self.search_by_url(url):
                if match["id"] not in seen_ids:
                    seen_ids.add(match["id"])
                    results.append(match)

        if name:
            for match in self.search_by_name(name, threshold):
                if match["id"] not in seen_ids:
                    seen_ids.add(match["id"])
                    results.append(match)

        return results

    def stats(self) -> dict:
        """Return summary statistics about the index."""
        by_database: dict[str, int] = {}
        for entry in self._entries:
            db = entry["database"]
            by_database[db] = by_database.get(db, 0) + 1
        return {
            "total": len(self._entries),
            "by_database": by_database,
        }

    # ── Private helpers ──────────────────────────────────────────────

    def _save_cache(self) -> None:
        """Save the current index to the cache file."""
        cache_file = _cache_file()
        data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": self._entries,
        }
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Cache saved to {cache_file}")

    def _rebuild_url_map(self) -> None:
        """Rebuild the URL lookup map from the entries list."""
        self._url_map = {}
        for i, entry in enumerate(self._entries):
            normalized = entry.get("url_normalized")
            if normalized:
                self._url_map.setdefault(normalized, []).append(i)
