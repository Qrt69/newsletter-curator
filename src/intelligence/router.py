"""
Router for Newsletter Curator.

Maps scored items to the correct Notion database, checks the DedupIndex
for duplicates, and produces routing decisions for downstream modules.
"""

from ..notion.dedup import DedupIndex

# 13 item_types -> 13 Notion databases (keys match DATABASES in client.py)
# Notes & Insights is excluded (personal/manual only)
ROUTING_TABLE = {
    "python_library": "Python Libraries",
    "duckdb_extension": "DuckDB Extensions",
    "ai_tool": "TAAFT",
    "agent_workflow": "Overview",
    "model_release": "Model information",
    "platform_infra": "Platforms & Infrastructure",
    "concept_pattern": "Topics & Concepts",
    "article": "Articles & Reads",
    "book_paper": "Books & Papers",
    "coding_tool": "AI Agents & Coding Tools",
    "vibe_coding_tool": "Vibe Coding Tools",
    "ai_architecture": "AI Architecture Topics",
    "infra_reference": "Infrastructure Knowledge Base",
}


class Router:
    """
    Routes scored items to the correct Notion database with dedup checking.

    Usage:
        dedup = DedupIndex(notion_client)
        dedup.load()
        router = Router(dedup)
        decision = router.route_item(scored_item)
    """

    def __init__(self, dedup_index: DedupIndex):
        self._dedup = dedup_index

    def route_item(self, scored_item: dict) -> dict:
        """
        Route one scored item: map to target database + dedup check.

        Args:
            scored_item: Dict from Scorer (has item_type, verdict, score, etc.)

        Returns:
            Routing decision dict with target_database, dedup_status, action, etc.
        """
        item_type = scored_item.get("item_type", "article")
        target_db = ROUTING_TABLE.get(item_type, "Articles & Reads")

        # Build the base decision with scorer fields passed through
        decision = {
            "score": scored_item.get("score", 0),
            "verdict": scored_item.get("verdict", ""),
            "item_type": item_type,
            "description": scored_item.get("description", ""),
            "reasoning": scored_item.get("reasoning", ""),
            "signals": scored_item.get("signals", []),
            "suggested_name": scored_item.get("suggested_name", ""),
            "suggested_category": scored_item.get("suggested_category", ""),
            "tags": scored_item.get("tags", []),
            "url": scored_item.get("url", ""),
            "link_text": scored_item.get("link_text", ""),
            "source_article": scored_item.get("source_article"),
            "pillar": scored_item.get("pillar", ""),
            "overlap": scored_item.get("overlap", ""),
            "relevance": scored_item.get("relevance", ""),
            "usefulness": scored_item.get("usefulness", ""),
            "usefulness_notes": scored_item.get("usefulness_notes", ""),
            "target_database": target_db,
            "dedup_status": "new",
            "dedup_matches": [],
            "action": "propose",
        }

        # Skip rejected/errored items immediately
        if decision["verdict"] in ("reject", "error"):
            decision["action"] = "skip"
            return decision

        # Dedup check: search by both name and URL
        matches = self._dedup.search(
            name=decision["suggested_name"] or None,
            url=decision["url"] or None,
        )
        decision["dedup_matches"] = matches

        if matches:
            decision["dedup_status"] = "duplicate"
            decision["action"] = "skip"

        return decision

    def route_batch(self, scored_items: list[dict]) -> list[dict]:
        """
        Route a list of scored items with within-batch URL dedup + progress.

        Args:
            scored_items: List of dicts from Scorer.

        Returns:
            List of routing decision dicts.
        """
        decisions = []
        seen_urls: set[str] = set()
        total = len(scored_items)

        for i, item in enumerate(scored_items, 1):
            name = (item.get("suggested_name") or item.get("link_text", "?"))[:40]
            name = name.encode("ascii", errors="replace").decode("ascii")
            print(f"  [{i}/{total}] Routing: {name}")

            decision = self.route_item(item)

            # Within-batch URL dedup
            url = decision["url"]
            if url and url in seen_urls and decision["action"] == "propose" and not decision.get("source_article"):
                decision["dedup_status"] = "duplicate"
                decision["action"] = "skip"
            elif url:
                seen_urls.add(url)

            print(f"           -> {decision['action']} ({decision['target_database']})")
            decisions.append(decision)

        return decisions

    @staticmethod
    def summary(decisions: list[dict]) -> dict:
        """
        Compute stats from a list of routing decisions.

        Args:
            decisions: List of routing decision dicts.

        Returns:
            Summary dict with counts by action, by database, and by dedup_status.
        """
        by_action: dict[str, int] = {}
        by_database: dict[str, int] = {}
        by_dedup: dict[str, int] = {}

        for d in decisions:
            action = d.get("action", "unknown")
            by_action[action] = by_action.get(action, 0) + 1

            db = d.get("target_database", "unknown")
            by_database[db] = by_database.get(db, 0) + 1

            dedup = d.get("dedup_status", "unknown")
            by_dedup[dedup] = by_dedup.get(dedup, 0) + 1

        return {
            "total": len(decisions),
            "by_action": by_action,
            "by_database": by_database,
            "by_dedup_status": by_dedup,
        }
