"""
Digest Database for Newsletter Curator.

SQLite-based storage for processing runs, digest items (routed newsletter
entries waiting for review), and feedback history for learning over time.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    emails_fetched  INTEGER DEFAULT 0,
    items_extracted INTEGER DEFAULT 0,
    items_scored    INTEGER DEFAULT 0,
    items_proposed  INTEGER DEFAULT 0,
    items_skipped   INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES runs(id),
    created_at          TEXT NOT NULL,

    -- Email context
    email_id            TEXT,
    email_subject       TEXT,
    email_sender        TEXT,

    -- Content
    url                 TEXT,
    link_text           TEXT,
    title               TEXT,
    author              TEXT,
    text                TEXT,

    -- Scorer output
    score               INTEGER,
    verdict             TEXT,
    item_type           TEXT,
    description         TEXT,
    reasoning           TEXT,
    signals             TEXT,
    suggested_name      TEXT,
    suggested_category  TEXT,
    tags                TEXT,

    -- Python library extra fields (from scorer)
    pillar              TEXT,
    overlap             TEXT,
    relevance           TEXT,
    usefulness          TEXT,
    usefulness_notes    TEXT,

    -- Router output
    target_database     TEXT,
    dedup_status        TEXT,
    dedup_matches       TEXT,
    action              TEXT,

    -- Listicle explosion
    source_article      TEXT,

    -- Review state
    user_decision       TEXT,
    decided_at          TEXT,
    notion_page_id      TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id),
    created_at      TEXT NOT NULL,
    verdict         TEXT,
    user_decision   TEXT NOT NULL,
    item_type       TEXT,
    target_database TEXT,
    score           INTEGER,
    suggested_name  TEXT,
    url             TEXT,
    reason          TEXT
);
"""


def _now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class DigestStore:
    """
    SQLite store for newsletter processing runs, digest items, and feedback.

    Usage:
        store = DigestStore("digest.db")
        run_id = store.create_run(emails_fetched=5)
        item_id = store.add_item(run_id, decision, email_meta)
        store.finish_run(run_id, stats)
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            data_dir = os.environ.get("DATA_DIR", ".")
            db_path = str(Path(data_dir) / "digest.db")
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # Migrate: add columns if missing (for existing DBs)
        for col in ("source_article", "pillar", "overlap", "relevance",
                     "usefulness", "usefulness_notes"):
            try:
                self._conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
        self._conn.commit()

    # ── Runs ────────────────────────────────────────────────────

    def create_run(self, emails_fetched: int = 0) -> int:
        """Start a new processing run. Returns run_id."""
        cur = self._conn.execute(
            "INSERT INTO runs (started_at, emails_fetched) VALUES (?, ?)",
            (_now(), emails_fetched),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        """Mark a run as completed with final stats."""
        self._conn.execute(
            """UPDATE runs SET
                finished_at = ?,
                items_extracted = ?,
                items_scored = ?,
                items_proposed = ?,
                items_skipped = ?,
                status = ?
            WHERE id = ?""",
            (
                _now(),
                stats.get("items_extracted", 0),
                stats.get("items_scored", 0),
                stats.get("items_proposed", 0),
                stats.get("items_skipped", 0),
                stats.get("status", "completed"),
                run_id,
            ),
        )
        self._conn.commit()

    def get_run(self, run_id: int) -> dict | None:
        """Get run details by ID."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_runs(self) -> list[dict]:
        """List all runs, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Items ───────────────────────────────────────────────────

    def add_item(self, run_id: int, decision: dict, email_meta: dict | None = None) -> int:
        """
        Store one routing decision as a digest item.

        Args:
            run_id: The processing run this item belongs to.
            decision: Routing decision dict from Router.
            email_meta: Optional dict with email_id, email_subject, email_sender.

        Returns:
            item_id of the created row.
        """
        meta = email_meta or {}
        text = (decision.get("text") or "")[:500]

        cur = self._conn.execute(
            """INSERT INTO items (
                run_id, created_at,
                email_id, email_subject, email_sender,
                url, link_text, title, author, text,
                score, verdict, item_type, description, reasoning, signals,
                suggested_name, suggested_category, tags,
                pillar, overlap, relevance, usefulness, usefulness_notes,
                target_database, dedup_status, dedup_matches, action,
                source_article
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                _now(),
                meta.get("email_id"),
                meta.get("email_subject"),
                meta.get("email_sender"),
                decision.get("url"),
                decision.get("link_text"),
                decision.get("title"),
                decision.get("author"),
                text,
                decision.get("score"),
                decision.get("verdict"),
                decision.get("item_type"),
                decision.get("description"),
                decision.get("reasoning"),
                json.dumps(decision.get("signals", [])),
                decision.get("suggested_name"),
                decision.get("suggested_category"),
                json.dumps(decision.get("tags", [])),
                decision.get("pillar"),
                decision.get("overlap"),
                decision.get("relevance"),
                decision.get("usefulness"),
                decision.get("usefulness_notes"),
                decision.get("target_database"),
                decision.get("dedup_status"),
                json.dumps(decision.get("dedup_matches", [])),
                decision.get("action"),
                decision.get("source_article"),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def add_batch(self, run_id: int, decisions: list[dict], email_meta: dict | None = None) -> list[int]:
        """
        Store a list of routing decisions from one email.

        Args:
            run_id: The processing run.
            decisions: List of routing decision dicts.
            email_meta: Optional email context shared by all items.

        Returns:
            List of item_ids.
        """
        return [self.add_item(run_id, d, email_meta) for d in decisions]

    def get_items(self, run_id: int, action_filter: str | None = None) -> list[dict]:
        """
        Get items for a run, optionally filtered by action.

        Args:
            run_id: The processing run.
            action_filter: If set, only return items with this action (e.g. "propose").

        Returns:
            List of item dicts with JSON fields decoded.
        """
        if action_filter:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE run_id = ? AND action = ? ORDER BY id",
                (run_id, action_filter),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [self._decode_item(r) for r in rows]

    def get_item(self, item_id: int) -> dict | None:
        """Get a single item with full details."""
        row = self._conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return self._decode_item(row) if row else None

    # ── Decisions / feedback ────────────────────────────────────

    def set_decision(self, item_id: int, decision: str, reason: str | None = None) -> None:
        """
        Record a user accept/reject decision and log it to the feedback table.

        Args:
            item_id: The item being reviewed.
            decision: "accepted", "rejected", or "edited".
            reason: Optional explanation of why the user overrode.
        """
        now = _now()

        # Get the item so we can copy fields into the feedback row
        item = self.get_item(item_id)
        if item is None:
            raise ValueError(f"Item {item_id} not found")

        # Update the item row
        self._conn.execute(
            "UPDATE items SET user_decision = ?, decided_at = ? WHERE id = ?",
            (decision, now, item_id),
        )

        # Insert feedback row
        self._conn.execute(
            """INSERT INTO feedback (
                item_id, created_at, verdict, user_decision,
                item_type, target_database, score,
                suggested_name, url, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                now,
                item.get("verdict"),
                decision,
                item.get("item_type"),
                item.get("target_database"),
                item.get("score"),
                item.get("suggested_name"),
                item.get("url"),
                reason,
            ),
        )

        self._conn.commit()

    def update_item_fields(self, item_id: int, fields: dict) -> None:
        """
        Update editable fields on an item before accepting.

        Allowed fields: suggested_name, suggested_category, target_database, tags.

        Args:
            item_id: The item to update.
            fields: Dict of field_name -> new_value. Only allowed fields are applied.
        """
        allowed = {"suggested_name", "suggested_category", "target_database", "tags"}
        to_update = {k: v for k, v in fields.items() if k in allowed}
        if not to_update:
            return

        # Encode tags as JSON if present
        if "tags" in to_update:
            to_update["tags"] = json.dumps(to_update["tags"])

        set_clause = ", ".join(f"{k} = ?" for k in to_update)
        values = list(to_update.values()) + [item_id]

        self._conn.execute(
            f"UPDATE items SET {set_clause} WHERE id = ?",
            values,
        )
        self._conn.commit()

    def get_pending_count(self, run_id: int) -> int:
        """Count items not yet reviewed in a run (action='propose' and no user_decision)."""
        row = self._conn.execute(
            """SELECT COUNT(*) FROM items
               WHERE run_id = ? AND action = 'propose' AND user_decision IS NULL""",
            (run_id,),
        ).fetchone()
        return row[0]

    def get_accepted_items(self, run_id: int) -> list[dict]:
        """Get accepted items not yet written to Notion."""
        rows = self._conn.execute(
            """SELECT * FROM items
               WHERE run_id = ? AND user_decision = 'accepted' AND notion_page_id IS NULL
               ORDER BY id""",
            (run_id,),
        ).fetchall()
        return [self._decode_item(r) for r in rows]

    def set_notion_page_id(self, item_id: int, page_id: str) -> None:
        """Record the Notion page ID after successful creation."""
        self._conn.execute(
            "UPDATE items SET notion_page_id = ? WHERE id = ?",
            (page_id, item_id),
        )
        self._conn.commit()

    def dismiss_undecided(self, run_id: int) -> int:
        """
        Bulk-dismiss all undecided items in a run by marking them as rejected.

        No feedback rows are created — these are bulk dismissals, not individual
        review decisions, so they shouldn't influence the scorer learning loop.

        Returns:
            Number of items dismissed.
        """
        now = _now()
        cur = self._conn.execute(
            """UPDATE items SET user_decision = 'rejected', decided_at = ?
               WHERE run_id = ? AND user_decision IS NULL""",
            (now, run_id),
        )
        self._conn.commit()
        return cur.rowcount

    def cleanup_old_items(self, days: int = 30) -> int:
        """
        Delete old rejected/skipped items and their feedback rows.

        Removes items where:
        - user_decision = 'rejected' AND decided_at < cutoff, OR
        - action = 'skip' AND created_at < cutoff

        Feedback rows are deleted first (FK constraint).

        Returns:
            Number of items deleted.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Find item IDs to delete
        rows = self._conn.execute(
            """SELECT id FROM items
               WHERE (user_decision = 'rejected' AND decided_at < ?)
                  OR (action = 'skip' AND created_at < ?)""",
            (cutoff, cutoff),
        ).fetchall()

        if not rows:
            return 0

        item_ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(item_ids))

        # Delete feedback first (FK constraint)
        self._conn.execute(
            f"DELETE FROM feedback WHERE item_id IN ({placeholders})",
            item_ids,
        )

        # Delete items
        self._conn.execute(
            f"DELETE FROM items WHERE id IN ({placeholders})",
            item_ids,
        )

        self._conn.commit()
        return len(item_ids)

    def get_feedback(self, limit: int = 50) -> list[dict]:
        """Recent feedback entries, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ───────────────────────────────────────────────────

    def stats(self) -> dict:
        """Summary stats across all runs."""
        runs = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        items = self._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        proposed = self._conn.execute(
            "SELECT COUNT(*) FROM items WHERE action = 'propose'"
        ).fetchone()[0]
        skipped = self._conn.execute(
            "SELECT COUNT(*) FROM items WHERE action = 'skip'"
        ).fetchone()[0]
        reviewed = self._conn.execute(
            "SELECT COUNT(*) FROM items WHERE user_decision IS NOT NULL"
        ).fetchone()[0]
        accepted = self._conn.execute(
            "SELECT COUNT(*) FROM items WHERE user_decision = 'accepted'"
        ).fetchone()[0]
        rejected = self._conn.execute(
            "SELECT COUNT(*) FROM items WHERE user_decision = 'rejected'"
        ).fetchone()[0]
        feedback_count = self._conn.execute(
            "SELECT COUNT(*) FROM feedback"
        ).fetchone()[0]

        return {
            "total_runs": runs,
            "total_items": items,
            "proposed": proposed,
            "skipped": skipped,
            "reviewed": reviewed,
            "accepted": accepted,
            "rejected": rejected,
            "feedback_entries": feedback_count,
        }

    # ── Internal ────────────────────────────────────────────────

    @staticmethod
    def _decode_item(row: sqlite3.Row) -> dict:
        """Convert a Row to dict and decode JSON string fields."""
        d = dict(row)
        for key in ("signals", "tags", "dedup_matches"):
            val = d.get(key)
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except json.JSONDecodeError:
                    d[key] = []
        return d
