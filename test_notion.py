import json
import os

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

notion = Client(auth=os.environ["NOTION_API_KEY"], notion_version="2022-06-28")

DATABASE_ID = "2bc1d067-a128-80ac-a9e4-c2c1943657cf"

# Fetch database metadata to learn the schema
db_meta = notion.databases.retrieve(database_id=DATABASE_ID)
title_parts = db_meta.get("title", [])
db_title = "".join(part["plain_text"] for part in title_parts) or "(Untitled)"
print(f"Database: {db_title}")
properties = db_meta.get("properties", {})
if properties:
    print(f"Properties: {', '.join(properties.keys())}")
print("-" * 60)

# Query all entries (paginated)
entries = []
cursor = None
while True:
    kwargs = {"database_id": DATABASE_ID, "page_size": 100}
    if cursor:
        kwargs["start_cursor"] = cursor
    body = {"page_size": kwargs["page_size"]}
    if "start_cursor" in kwargs:
        body["start_cursor"] = kwargs["start_cursor"]
    response = notion.request(
        path=f"databases/{DATABASE_ID}/query",
        method="POST",
        body=body,
    )
    entries.extend(response["results"])
    if not response.get("has_more"):
        break
    cursor = response["next_cursor"]

print(f"\nFound {len(entries)} entries:\n")

for i, page in enumerate(entries, 1):
    print(f"--- Entry {i} ---")
    for prop_name, prop_data in page["properties"].items():
        prop_type = prop_data["type"]
        value = ""

        if prop_type == "title":
            value = "".join(t["plain_text"] for t in prop_data.get("title", []))
        elif prop_type == "rich_text":
            value = "".join(t["plain_text"] for t in prop_data.get("rich_text", []))
        elif prop_type == "number":
            value = prop_data.get("number")
        elif prop_type == "select":
            sel = prop_data.get("select")
            value = sel["name"] if sel else ""
        elif prop_type == "multi_select":
            value = ", ".join(s["name"] for s in prop_data.get("multi_select", []))
        elif prop_type == "date":
            d = prop_data.get("date")
            if d:
                value = d.get("start", "")
                if d.get("end"):
                    value += f" → {d['end']}"
        elif prop_type == "checkbox":
            value = prop_data.get("checkbox")
        elif prop_type == "url":
            value = prop_data.get("url")
        elif prop_type == "email":
            value = prop_data.get("email")
        elif prop_type == "phone_number":
            value = prop_data.get("phone_number")
        elif prop_type == "status":
            st = prop_data.get("status")
            value = st["name"] if st else ""
        elif prop_type == "people":
            value = ", ".join(p.get("name", p["id"]) for p in prop_data.get("people", []))
        elif prop_type == "relation":
            value = ", ".join(r["id"] for r in prop_data.get("relation", []))
        elif prop_type == "formula":
            f = prop_data.get("formula", {})
            value = f.get(f.get("type", ""), "")
        elif prop_type == "rollup":
            r = prop_data.get("rollup", {})
            value = f"[rollup: {r.get('type', '')}]"
        elif prop_type == "files":
            value = ", ".join(
                f.get("external", {}).get("url", "") or f.get("file", {}).get("url", "")
                for f in prop_data.get("files", [])
            )
        elif prop_type == "created_time":
            value = prop_data.get("created_time")
        elif prop_type == "last_edited_time":
            value = prop_data.get("last_edited_time")
        else:
            value = f"[{prop_type}]"

        if value is None:
            value = ""
        print(f"  {prop_name}: {value}")
    print()
