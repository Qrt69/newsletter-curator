"""
Integration test for the DedupIndex.

Tests against live Notion data — requires NOTION_API_KEY in .env.
Run: uv run python tests/test_dedup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.notion.client import NotionClient
from src.notion.dedup import DedupIndex

def main():
    nc = NotionClient()
    index = DedupIndex(nc)

    # ── 1. Build the index from all databases ─────────────────────
    print("=" * 60)
    print("TEST 1: Build index from Notion")
    print("=" * 60)
    index.build()
    print()

    # ── 2. Print stats ────────────────────────────────────────────
    print("=" * 60)
    print("TEST 2: Stats")
    print("=" * 60)
    s = index.stats()
    print(f"Total entries: {s['total']}")
    for db_name, count in sorted(s["by_database"].items()):
        print(f"  {db_name}: {count}")
    assert s["total"] > 0, "Index should have entries"
    print("PASS\n")

    # ── 3. Search by exact name ───────────────────────────────────
    print("=" * 60)
    print("TEST 3: Search by name — 'Marimo'")
    print("=" * 60)
    results = index.search_by_name("Marimo")
    for r in results:
        print(f"  {r['name']} ({r['database']}) — score {r['score']}")
    assert len(results) > 0, "Should find at least one match for 'Marimo'"
    assert results[0]["score"] >= 80, "Best match should score >= 80"
    print("PASS\n")

    # ── 4. Fuzzy name search ──────────────────────────────────────
    print("=" * 60)
    print("TEST 4: Fuzzy search — 'marimo notebook'")
    print("=" * 60)
    results = index.search_by_name("marimo notebook", threshold=50)
    for r in results[:5]:
        print(f"  {r['name']} ({r['database']}) — score {r['score']}")
    # Marimo should appear somewhere in results
    names_lower = [r["name"].lower() for r in results]
    assert any("marimo" in n for n in names_lower), \
        "Fuzzy search for 'marimo notebook' should find Marimo"
    print("PASS\n")

    # ── 5. exists() check ─────────────────────────────────────────
    print("=" * 60)
    print("TEST 5: exists()")
    print("=" * 60)
    assert index.exists("Marimo"), "Marimo should exist"
    print("  exists('Marimo') = True  OK")
    assert not index.exists("zzz_nonexistent_tool_12345"), \
        "Nonsense name should not exist"
    print("  exists('zzz_nonexistent_tool_12345') = False  OK")
    print("PASS\n")

    # ── 6. Cache round-trip ───────────────────────────────────────
    print("=" * 60)
    print("TEST 6: Cache round-trip")
    print("=" * 60)
    original_stats = index.stats()

    index2 = DedupIndex(nc)
    index2.load()  # should load from cache
    cached_stats = index2.stats()

    assert original_stats["total"] == cached_stats["total"], \
        f"Cache mismatch: {original_stats['total']} vs {cached_stats['total']}"
    print(f"  Original: {original_stats['total']} entries")
    print(f"  Cached:   {cached_stats['total']} entries")

    # Verify search works on cached index too
    cached_results = index2.search_by_name("Marimo")
    assert len(cached_results) > 0, "Cached index should also find Marimo"
    print("  Search on cached index works  OK")
    print("PASS\n")

    # ── 7. Combined search ────────────────────────────────────────
    print("=" * 60)
    print("TEST 7: Combined search")
    print("=" * 60)
    results = index.search(name="Marimo", url="https://github.com/marimo-team/marimo")
    for r in results:
        print(f"  {r['name']} ({r['database']}) — id {r['id'][:8]}...")
    assert len(results) > 0, "Combined search should find at least one match"
    print("PASS\n")

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
