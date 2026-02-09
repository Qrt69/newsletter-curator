"""
Notion client wrapper for Newsletter Curator.

Wraps the notion-client SDK into clean, reusable methods.
Handles pagination, property extraction, and entry management.
"""

import os

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

# All 14 Notion databases used by the curator.
# Keys are human-readable names, values are Notion database IDs.
DATABASES = {
    "Articles & Reads": "2cc1d067-a128-80e8-bdb1-d81fff250a54",
    "Infrastructure Knowledge Base": "2c81d067-a128-80fa-8762-de1c655c431f",
    "Notes & Insights": "2c21d067-a128-8085-92a6-da1115fdc2f2",
    "Topics & Concepts": "2bc1d067-a128-8062-a981-c105b6dee624",
    "Books & Papers": "2bc1d067-a128-80ac-a9e4-c2c1943657cf",
    "Platforms & Infrastructure": "94c3611a-2f3c-41ac-b4df-248013160107",
    "AI Agents & Coding Tools": "5dec10bd-ae78-44a7-81be-4b9b1bd85da4",
    "Model information": "2c81d067-a128-80eb-9bef-f3492a68c4c2",
    "Vibe Coding Tools": "b63aa8e4-9c70-4688-b30b-3c581777744c",
    "AI Architecture Topics": "3a87061e-957d-4bf9-9133-f49932edbbdb",
    "Overview": "2c81d067-a128-80d0-aa60-caaab0e81ea5",
    "TAAFT": "2c81d067-a128-809b-9031-d607131ea7c0",
    "Python Libraries": "2c61d067-a128-80e0-8841-dbe01e199e03",
    "DuckDB Extensions": "2ce1d067-a128-8091-95a8-e1a82bbd872f",
}


class NotionClient:
    """
    High-level wrapper around the Notion API.

    Usage:
        nc = NotionClient()                       # uses NOTION_API_KEY from .env
        entries = nc.query_database("Books & Papers")
        for entry in entries:
            print(entry["Name"])                  # clean, simple values
    """

    def __init__(self, api_key: str | None = None):
        """
        Initialize the Notion client.

        Args:
            api_key: Notion integration token. If not provided,
                     reads from NOTION_API_KEY environment variable.
        """
        token = api_key or os.environ.get("NOTION_API_KEY")
        if not token:
            raise ValueError(
                "No Notion API key found. Set NOTION_API_KEY in .env "
                "or pass api_key to NotionClient()."
            )
        # Pin to 2022-06-28 — this version returns full property schemas
        # from databases.retrieve(). Newer versions omit them.
        self._client = Client(auth=token, notion_version="2022-06-28")

    # ── Querying ──────────────────────────────────────────────────────

    def get_database_id(self, name: str) -> str:
        """
        Look up a database ID by its human-readable name.

        Args:
            name: Database name (e.g. "Books & Papers")

        Returns:
            The Notion database ID string.

        Raises:
            KeyError: If the database name isn't recognized.
        """
        if name not in DATABASES:
            raise KeyError(
                f"Unknown database '{name}'. "
                f"Known databases: {', '.join(DATABASES.keys())}"
            )
        return DATABASES[name]

    def get_database_schema(self, database: str) -> dict:
        """
        Get the property schema for a database.

        Args:
            database: Database name (e.g. "Books & Papers") or database ID.

        Returns:
            Dict mapping property names to their types.
            Example: {"Name": "title", "Author": "rich_text", "Rating": "number"}
        """
        db_id = DATABASES.get(database, database)
        db_meta = self._client.databases.retrieve(database_id=db_id)
        return {
            name: prop["type"]
            for name, prop in db_meta["properties"].items()
        }

    def query_database(
        self,
        database: str,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
    ) -> list[dict]:
        """
        Query all entries from a Notion database with automatic pagination.

        Returns entries with clean, extracted property values — no nested
        Notion structures. Each entry also includes its page "id".

        Args:
            database: Database name (e.g. "Books & Papers") or database ID.
            filter: Optional Notion filter object.
                    Example: {"property": "Status", "select": {"equals": "Reading"}}
            sorts: Optional list of sort objects.
                   Example: [{"property": "Name", "direction": "ascending"}]

        Returns:
            List of dicts, one per entry. Each dict has:
              - "id": the Notion page ID
              - One key per property with its extracted value
        """
        db_id = DATABASES.get(database, database)

        # Paginate through all results using the raw request API
        # (the SDK's databases endpoint doesn't expose a query method)
        raw_pages = []
        cursor = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts

            response = self._client.request(
                path=f"databases/{db_id}/query",
                method="POST",
                body=body,
            )
            raw_pages.extend(response["results"])

            if not response.get("has_more"):
                break
            cursor = response["next_cursor"]

        # Convert each page's properties to clean Python values
        return [self._extract_page(page) for page in raw_pages]

    # ── Creating ──────────────────────────────────────────────────────

    def create_entry(self, database: str, properties: dict) -> dict:
        """
        Create a new entry (page) in a Notion database.

        You provide properties as simple Python values. This method converts
        them into the nested format Notion expects.

        Args:
            database: Database name (e.g. "Books & Papers") or database ID.
            properties: Dict of property values to set.
                        Use the helpers below to build property values:
                        - title("My Book")
                        - rich_text("Some description")
                        - number(42)
                        - select("Fiction")
                        - multi_select(["AI", "Python"])
                        - url("https://example.com")
                        - checkbox(True)
                        - relation(["page-id-1", "page-id-2"])

        Returns:
            The created page as a clean dict (same format as query results).
        """
        db_id = DATABASES.get(database, database)

        response = self._client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        return self._extract_page(response)

    # ── Updating ──────────────────────────────────────────────────────

    def update_entry(self, page_id: str, properties: dict) -> dict:
        """
        Update properties on an existing Notion page.

        Args:
            page_id: The Notion page ID to update.
            properties: Dict of Notion-formatted property values to change.
                        (Same format as create_entry.)

        Returns:
            The updated page as a clean dict.
        """
        response = self._client.pages.update(
            page_id=page_id,
            properties=properties,
        )
        return self._extract_page(response)

    def add_relation(
        self, page_id: str, property_name: str, related_page_ids: list[str]
    ) -> dict:
        """
        Add relation links from one page to other pages.

        This APPENDS to existing relations (doesn't replace them).

        Args:
            page_id: The page to add relations to.
            property_name: The relation property name (e.g. "Related Articles").
            related_page_ids: List of page IDs to link to.

        Returns:
            The updated page as a clean dict.
        """
        # First, get existing relations so we don't overwrite them
        page = self._client.pages.retrieve(page_id=page_id)
        existing = page["properties"].get(property_name, {})
        existing_ids = [r["id"] for r in existing.get("relation", [])]

        # Merge: existing + new (no duplicates)
        all_ids = list(dict.fromkeys(existing_ids + related_page_ids))

        return self.update_entry(
            page_id,
            {property_name: relation(all_ids)},
        )

    # ── Property extraction ───────────────────────────────────────────

    def _extract_page(self, page: dict) -> dict:
        """
        Convert a raw Notion page into a clean dict with simple Python values.

        Turns Notion's deeply nested property structures into flat values:
          {"title": [{"plain_text": "My Book"}]}  →  "My Book"
          {"select": {"name": "Fiction"}}          →  "Fiction"
          {"number": 42}                           →  42
        """
        result = {"id": page["id"]}
        for prop_name, prop_data in page["properties"].items():
            result[prop_name] = self._extract_property_value(prop_data)
        return result

    @staticmethod
    def _extract_property_value(prop_data: dict):
        """
        Extract a single property value from Notion's nested format.

        Handles all common Notion property types and returns simple
        Python values (str, int, float, bool, list, or None).
        """
        prop_type = prop_data["type"]

        if prop_type == "title":
            return "".join(t["plain_text"] for t in prop_data.get("title", []))

        elif prop_type == "rich_text":
            return "".join(t["plain_text"] for t in prop_data.get("rich_text", []))

        elif prop_type == "number":
            return prop_data.get("number")

        elif prop_type == "select":
            sel = prop_data.get("select")
            return sel["name"] if sel else None

        elif prop_type == "multi_select":
            return [s["name"] for s in prop_data.get("multi_select", [])]

        elif prop_type == "date":
            d = prop_data.get("date")
            if not d:
                return None
            start = d.get("start", "")
            end = d.get("end")
            return f"{start} → {end}" if end else start

        elif prop_type == "checkbox":
            return prop_data.get("checkbox", False)

        elif prop_type == "url":
            return prop_data.get("url")

        elif prop_type == "email":
            return prop_data.get("email")

        elif prop_type == "phone_number":
            return prop_data.get("phone_number")

        elif prop_type == "status":
            st = prop_data.get("status")
            return st["name"] if st else None

        elif prop_type == "people":
            return [p.get("name", p["id"]) for p in prop_data.get("people", [])]

        elif prop_type == "relation":
            return [r["id"] for r in prop_data.get("relation", [])]

        elif prop_type == "formula":
            f = prop_data.get("formula", {})
            return f.get(f.get("type", ""), "")

        elif prop_type == "rollup":
            r = prop_data.get("rollup", {})
            return f"[rollup: {r.get('type', '')}]"

        elif prop_type == "files":
            return [
                f.get("external", {}).get("url", "")
                or f.get("file", {}).get("url", "")
                for f in prop_data.get("files", [])
            ]

        elif prop_type == "created_time":
            return prop_data.get("created_time")

        elif prop_type == "last_edited_time":
            return prop_data.get("last_edited_time")

        elif prop_type == "created_by":
            cb = prop_data.get("created_by", {})
            return cb.get("name", cb.get("id"))

        elif prop_type == "last_edited_by":
            eb = prop_data.get("last_edited_by", {})
            return eb.get("name", eb.get("id"))

        else:
            return f"[unsupported: {prop_type}]"


# ── Property builder helpers ──────────────────────────────────────────
#
# These functions create the nested dicts that Notion's API expects
# when creating or updating entries. Use them like:
#
#   nc.create_entry("Books & Papers", {
#       "Name": title("Deep Learning"),
#       "Author": rich_text("Ian Goodfellow"),
#       "Rating": number(5),
#       "Type": select("Book"),
#       "Tags": multi_select(["AI", "ML"]),
#   })


def title(text: str) -> dict:
    """Build a title property value."""
    return {"title": [{"text": {"content": text}}]}


def rich_text(text: str) -> dict:
    """Build a rich_text property value."""
    return {"rich_text": [{"text": {"content": text}}]}


def number(value: int | float) -> dict:
    """Build a number property value."""
    return {"number": value}


def select(name: str) -> dict:
    """Build a select property value."""
    return {"select": {"name": name}}


def multi_select(names: list[str]) -> dict:
    """Build a multi_select property value."""
    return {"multi_select": [{"name": n} for n in names]}


def url(link: str) -> dict:
    """Build a URL property value."""
    return {"url": link}


def checkbox(checked: bool) -> dict:
    """Build a checkbox property value."""
    return {"checkbox": checked}


def date(start: str, end: str | None = None) -> dict:
    """Build a date property value. Dates should be ISO 8601 strings."""
    d = {"start": start}
    if end:
        d["end"] = end
    return {"date": d}


def relation(page_ids: list[str]) -> dict:
    """Build a relation property value."""
    return {"relation": [{"id": pid} for pid in page_ids]}
