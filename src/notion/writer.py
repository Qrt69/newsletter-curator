"""
Notion Writer for Newsletter Curator.

Creates or updates Notion pages for accepted digest items,
then records the page ID back in the digest database.
"""

from datetime import date as date_today

from .client import NotionClient, title, rich_text, select, multi_select, url, date


def _base(item, name_field="Name"):
    """Build a dict with just the title property."""
    return {name_field: title(item["suggested_name"])}


def _add(props, key, value):
    """Add a property only if value is truthy. Returns props for chaining."""
    if value:
        props[key] = value
    return props


def _learning_priority(score: int) -> str:
    """Derive Learning Priority from score: 5+=High, 3-4=Medium, else Low."""
    if score >= 5:
        return "High"
    elif score >= 3:
        return "Medium"
    return "Low"


def _build_python_libraries(item):
    props = _base(item)
    _add(props, "Category", rich_text(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Short Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Primary Use", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    _add(props, "Pillar", rich_text(item["pillar"]) if item.get("pillar") else None)
    _add(props, "Overlaps / Alternatives", rich_text(item["overlap"]) if item.get("overlap") else None)
    _add(props, "Relevance", rich_text(item["relevance"]) if item.get("relevance") else None)
    _add(props, "Reason", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    _add(props, "Learning Priority", select(_learning_priority(item.get("score", 0))))
    _add(props, "Usefulness (High/Medium/Low)", rich_text(item["usefulness"]) if item.get("usefulness") else None)
    _add(props, "Usefulness Notes", rich_text(item["usefulness_notes"]) if item.get("usefulness_notes") else None)
    return props


def _build_duckdb_extensions(item):
    props = _base(item, "Extension Name")
    _add(props, "Category", select(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Description", rich_text(item["description"]) if item.get("description") else None)
    return props


def _build_taaft(item):
    props = _base(item)
    _add(props, "Category", rich_text(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Type", rich_text(item["item_type"]) if item.get("item_type") else None)
    _add(props, "Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Source URL", url(item["url"]) if item.get("url") else None)
    return props


def _build_overview(item):
    props = _base(item)
    _add(props, "Type", rich_text(item["item_type"]) if item.get("item_type") else None)
    _add(props, "Category", rich_text(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Core Idea", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Description", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    _add(props, "Source URL", url(item["url"]) if item.get("url") else None)
    _add(props, "Date Added", date(date_today.today().isoformat()))
    return props


def _build_model_information(item):
    props = _base(item)
    _add(props, "Category", rich_text(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Type", rich_text(item["item_type"]) if item.get("item_type") else None)
    _add(props, "Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Why It Matters", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    _add(props, "Source URL", url(item["url"]) if item.get("url") else None)
    _add(props, "Tags", rich_text(", ".join(item["tags"])) if item.get("tags") else None)
    return props


def _build_platforms_infrastructure(item):
    props = _base(item, "Platform Name")
    _add(props, "Category", select(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Website", url(item["url"]) if item.get("url") else None)
    return props


def _build_topics_concepts(item):
    props = _base(item)
    _add(props, "Type", select(item["item_type"]) if item.get("item_type") else None)
    _add(props, "Category", multi_select([item["suggested_category"]]) if item.get("suggested_category") else None)
    _add(props, "Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Tags", multi_select(item["tags"]) if item.get("tags") else None)
    _add(props, "Summary", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    return props


def _build_articles_reads(item):
    props = _base(item)
    _add(props, "URL", url(item["url"]) if item.get("url") else None)
    _add(props, "Tags", multi_select(item["tags"]) if item.get("tags") else None)
    _add(props, "Source", select(item["email_sender"]) if item.get("email_sender") else None)
    _add(props, "Short Summary", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Why it matters", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    _add(props, "Date found", date(date_today.today().isoformat()))
    return props


def _build_books_papers(item):
    props = _base(item)
    _add(props, "Type", select(item["item_type"]) if item.get("item_type") else None)
    _add(props, "Author", rich_text(item["author"]) if item.get("author") else None)
    _add(props, "URL", url(item["url"]) if item.get("url") else None)
    _add(props, "Tags", multi_select(item["tags"]) if item.get("tags") else None)
    return props


def _build_ai_agents_coding_tools(item):
    props = _base(item)
    _add(props, "Category", rich_text(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Short Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Primary Use", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    return props


def _build_vibe_coding_tools(item):
    props = _base(item)
    _add(props, "Category", select(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Short Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Primary Use", rich_text(item["reasoning"]) if item.get("reasoning") else None)
    return props


def _build_ai_architecture_topics(item):
    props = _base(item)
    _add(props, "Type", select(item["item_type"]) if item.get("item_type") else None)
    _add(props, "Summary", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Main Link", url(item["url"]) if item.get("url") else None)
    return props


def _build_infra_knowledge_base(item):
    props = _base(item, "Title")
    _add(props, "Category", select(item["suggested_category"]) if item.get("suggested_category") else None)
    _add(props, "Description", rich_text(item["description"]) if item.get("description") else None)
    _add(props, "Tags", multi_select(item["tags"]) if item.get("tags") else None)
    return props


# Per-database property builders.
# Each entry is a callable(item) -> dict of Notion properties.
# Property names match the actual Notion database schemas.
PROPERTY_MAP = {
    "Python Libraries": _build_python_libraries,
    "DuckDB Extensions": _build_duckdb_extensions,
    "TAAFT": _build_taaft,
    "Overview": _build_overview,
    "Model information": _build_model_information,
    "Platforms & Infrastructure": _build_platforms_infrastructure,
    "Topics & Concepts": _build_topics_concepts,
    "Articles & Reads": _build_articles_reads,
    "Books & Papers": _build_books_papers,
    "AI Agents & Coding Tools": _build_ai_agents_coding_tools,
    "Vibe Coding Tools": _build_vibe_coding_tools,
    "AI Architecture Topics": _build_ai_architecture_topics,
    "Infrastructure Knowledge Base": _build_infra_knowledge_base,
}


# Relation config: database -> list of (relation_property, target_database)
RELATION_MAP = {
    "Articles & Reads": [("Related Concepts", "Topics & Concepts")],
    "Books & Papers": [("Related Topics", "Topics & Concepts")],
    "Topics & Concepts": [("Related Books & Papers", "Books & Papers")],
}


class NotionWriter:
    """
    Writes accepted digest items to Notion as pages.

    Usage:
        writer = NotionWriter(notion_client, digest_store)
        summary = writer.write_batch(run_id)
        print(summary)  # {created: N, updated: N, failed: N, errors: [...]}
    """

    def __init__(self, notion_client: NotionClient, digest_store, dedup_index=None):
        self._notion = notion_client
        self._store = digest_store
        self._dedup = dedup_index

    def write_item(self, item: dict) -> str:
        """
        Create or update a Notion page for one accepted item.

        Args:
            item: Dict from DigestStore (has target_database, dedup_status, etc.)

        Returns:
            The Notion page ID of the created/updated page.
        """
        target_db = item["target_database"]
        builder = PROPERTY_MAP.get(target_db)
        if not builder:
            raise ValueError(f"No property map for database: {target_db}")

        properties = builder(item)

        # Decide create vs update
        if (item.get("dedup_status") == "update_candidate"
                and item.get("dedup_matches")):
            # Find the first match with a page ID
            existing_page_id = None
            for match in item["dedup_matches"]:
                if isinstance(match, dict) and match.get("page_id"):
                    existing_page_id = match["page_id"]
                    break

            if existing_page_id:
                page = self._notion.update_entry(existing_page_id, properties)
                page_id = page["id"]
            else:
                page = self._notion.create_entry(target_db, properties)
                page_id = page["id"]
        else:
            page = self._notion.create_entry(target_db, properties)
            page_id = page["id"]

        # Record page ID in digest DB
        self._store.set_notion_page_id(item["id"], page_id)

        # Link relations to entries in related databases
        self._link_relations(page_id, item, target_db)

        return page_id

    def _link_relations(self, page_id: str, item: dict, target_db: str) -> None:
        """
        Search the dedup index for related entries and link them via
        Notion relation properties.

        No-op if dedup_index was not provided or target_db has no relations.
        """
        if not self._dedup:
            return

        relations = RELATION_MAP.get(target_db)
        if not relations:
            return

        # Collect search terms from tags + suggested_category
        terms: list[str] = []
        if item.get("tags"):
            terms.extend(item["tags"])
        if item.get("suggested_category"):
            terms.append(item["suggested_category"])

        if not terms:
            return

        for relation_property, relation_target_db in relations:
            related_page_ids: list[str] = []
            seen: set[str] = set()

            for term in terms:
                matches = self._dedup.search_by_name(term, threshold=85)
                for match in matches:
                    if match["database"] == relation_target_db and match["id"] not in seen:
                        seen.add(match["id"])
                        related_page_ids.append(match["id"])

            if related_page_ids:
                try:
                    self._notion.add_relation(page_id, relation_property, related_page_ids)
                    print(f"           -> linked {len(related_page_ids)} {relation_target_db} "
                          f"entries via '{relation_property}'")
                except Exception as exc:
                    print(f"           -> relation link failed ({relation_property}): {exc}")

    def write_batch(self, run_id: int) -> dict:
        """
        Write all accepted items for a run to Notion.

        Args:
            run_id: The processing run ID.

        Returns:
            Summary dict: {created, updated, failed, errors}
        """
        items = self._store.get_accepted_items(run_id)
        created = 0
        updated = 0
        failed = 0
        errors = []

        total = len(items)
        for i, item in enumerate(items, 1):
            name = (item.get("suggested_name") or "?")[:40]
            name = name.encode("ascii", errors="replace").decode("ascii")
            print(f"  [{i}/{total}] Writing: {name}")

            try:
                page_id = self.write_item(item)

                is_update = (
                    item.get("dedup_status") == "update_candidate"
                    and item.get("dedup_matches")
                )
                if is_update:
                    updated += 1
                    print(f"           -> updated ({item['target_database']})")
                else:
                    created += 1
                    print(f"           -> created ({item['target_database']})")

            except Exception as exc:
                failed += 1
                error_msg = f"{name}: {exc}"
                errors.append(error_msg)
                print(f"           -> FAILED: {exc}")

        return {
            "created": created,
            "updated": updated,
            "failed": failed,
            "errors": errors,
        }
