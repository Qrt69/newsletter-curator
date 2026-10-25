"""
Everything the curator must remember between runs, in one SQLite file:

- seen:       every link key ever handled (kept or rejected) -> real duplicates
- topics:     what kept items said per topic -> "what does this article add?"
- extracted:  per email, the items pulled out of it (cache for an interrupted run)
- judged:     per link key, the model's answers (cache for an interrupted run)
- rejected:   rejected items with their data, so a ticked box can upgrade them
- upgrades:   which rejected items Kurt upgraded (for the pattern hint)
- runs:       one row per run, open until the run has finished
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY, first_seen REAL, outcome TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS topics (
    topic TEXT, key TEXT, title TEXT, summary TEXT, note TEXT, added REAL,
    PRIMARY KEY (topic, key)
);
CREATE TABLE IF NOT EXISTS extracted (
    run_id INTEGER, email_id TEXT, items TEXT, PRIMARY KEY (run_id, email_id)
);
CREATE TABLE IF NOT EXISTS judged (
    key TEXT PRIMARY KEY, answers TEXT, judged REAL
);
CREATE TABLE IF NOT EXISTS rejected (
    rid TEXT PRIMARY KEY, key TEXT, item TEXT, answers TEXT, reason TEXT,
    run_date TEXT, upgraded REAL
);
CREATE TABLE IF NOT EXISTS upgrades (
    rid TEXT PRIMARY KEY, source TEXT, category TEXT, reason TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, finished REAL,
    run_date TEXT, status TEXT, summary TEXT
);
"""


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ── runs ────────────────────────────────────────────────────
    def open_run(self, run_date: str) -> tuple[int, bool]:
        """Return (run_id, resumed). An unfinished run is continued, not restarted."""
        row = self.db.execute(
            "SELECT id FROM runs WHERE status = 'bezig' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row["id"], True
        cur = self.db.execute(
            "INSERT INTO runs (started, run_date, status) VALUES (?, ?, 'bezig')",
            (time.time(), run_date),
        )
        self.db.commit()
        return cur.lastrowid, False

    def run_date(self, run_id: int) -> str:
        return self.db.execute("SELECT run_date FROM runs WHERE id = ?", (run_id,)).fetchone()[0]

    def finish_run(self, run_id: int, status: str, summary: dict):
        self.db.execute(
            "UPDATE runs SET finished = ?, status = ?, summary = ? WHERE id = ?",
            (time.time(), status, json.dumps(summary, ensure_ascii=False), run_id),
        )
        self.db.execute("DELETE FROM extracted WHERE run_id = ?", (run_id,))
        self.db.commit()

    # ── extraction cache ────────────────────────────────────────
    def get_extracted(self, run_id: int, email_id: str) -> list[dict] | None:
        row = self.db.execute(
            "SELECT items FROM extracted WHERE run_id = ? AND email_id = ?", (run_id, email_id)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put_extracted(self, run_id: int, email_id: str, items: list[dict]):
        self.db.execute(
            "INSERT OR REPLACE INTO extracted VALUES (?, ?, ?)",
            (run_id, email_id, json.dumps(items, ensure_ascii=False, default=str)),
        )
        self.db.commit()

    # ── judgement cache ─────────────────────────────────────────
    def get_judged(self, key: str) -> dict | None:
        row = self.db.execute("SELECT answers FROM judged WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_judged(self, key: str, answers: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO judged VALUES (?, ?, ?)",
            (key, json.dumps(answers, ensure_ascii=False), time.time()),
        )
        self.db.commit()

    # ── duplicates ──────────────────────────────────────────────
    def is_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone() is not None

    def mark_seen(self, key: str, outcome: str, note: str = ""):
        self.db.execute(
            "INSERT OR REPLACE INTO seen VALUES (?, COALESCE((SELECT first_seen FROM seen WHERE key = ?), ?), ?, ?)",
            (key, key, time.time(), outcome, note),
        )

    def commit(self):
        self.db.commit()

    # ── topics ──────────────────────────────────────────────────
    def topic_entries(self, topic: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT title, summary, note FROM topics WHERE topic = ? ORDER BY added", (topic,)
        ).fetchall()
        return [dict(r) for r in rows]

    def add_topic_entry(self, topic: str, key: str, title: str, summary: str, note: str):
        self.db.execute(
            "INSERT OR REPLACE INTO topics VALUES (?, ?, ?, ?, ?, ?)",
            (topic, key, title, summary, note, time.time()),
        )

    # ── rejected + upgrades ─────────────────────────────────────
    def add_rejected(self, rid: str, key: str, item: dict, answers: dict | None, reason: str, run_date: str):
        self.db.execute(
            "INSERT OR REPLACE INTO rejected VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (rid, key, json.dumps(item, ensure_ascii=False, default=str),
             json.dumps(answers or {}, ensure_ascii=False), reason, run_date),
        )

    def get_rejected(self, rid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM rejected WHERE rid = ?", (rid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["item"] = json.loads(d["item"])
        d["answers"] = json.loads(d["answers"])
        return d

    def rejected_for_date(self, run_date: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT rid, item, answers, reason FROM rejected WHERE run_date = ? ORDER BY rowid",
            (run_date,),
        ).fetchall()
        out = []
        for r in rows:
            item = json.loads(r["item"])
            answers = json.loads(r["answers"])
            out.append({
                "rid": r["rid"],
                "title": answers.get("title") or item.get("title") or item.get("link_text") or "Zonder titel",
                "url": item.get("resolved_url") or item.get("source_url") or "",
                "reason": r["reason"],
                "why": item.get("_why", ""),
                "source": item.get("_source", ""),
            })
        return out

    def mark_upgraded(self, rid: str, source: str, category: str, reason: str):
        self.db.execute("UPDATE rejected SET upgraded = ? WHERE rid = ?", (time.time(), rid))
        self.db.execute(
            "INSERT OR REPLACE INTO upgrades VALUES (?, ?, ?, ?, ?)",
            (rid, source, category, reason, time.time()),
        )

    def upgrade_patterns(self, since_days: int, threshold: int) -> list[tuple[str, str, int]]:
        """(source, category, count) groups Kurt upgraded at least `threshold` times."""
        cutoff = time.time() - since_days * 86400
        rows = self.db.execute(
            "SELECT source, category, COUNT(*) n FROM upgrades WHERE at >= ? "
            "GROUP BY source, category HAVING n >= ? ORDER BY n DESC",
            (cutoff, threshold),
        ).fetchall()
        return [(r["source"], r["category"], r["n"]) for r in rows]

    def forget_rejected_before(self, run_date: str):
        self.db.execute("DELETE FROM rejected WHERE run_date < ? AND upgraded IS NULL", (run_date,))
        self.db.commit()
