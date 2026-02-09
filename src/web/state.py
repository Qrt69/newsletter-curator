"""
Reflex state classes for the Newsletter Curator review UI.

DigestState holds the list of runs and items.
Event handlers load data from SQLite, handle accept/reject/edit.
"""

import reflex as rx

from ..storage.digest import DigestStore
from ..intelligence.router import ROUTING_TABLE
from ..intelligence.feedback import FeedbackProcessor


# All possible target databases (from the routing table)
DATABASE_OPTIONS: list[str] = sorted(set(ROUTING_TABLE.values()))


def _get_store() -> DigestStore:
    """Create a fresh DigestStore connection (thread-safe)."""
    return DigestStore("digest.db")


class DigestState(rx.State):
    """Top-level state: run selection, items list, detail dialog."""

    # Run data
    runs: list[dict] = []
    selected_run_id: int = 0
    run_label: str = ""

    # Items for current run
    items: list[dict] = []
    pending_count: int = 0

    # Detail dialog
    show_detail: bool = False
    detail_item: dict = {}

    # Editable fields in detail dialog
    edit_name: str = ""
    edit_category: str = ""
    edit_database: str = ""
    edit_tags: str = ""

    # Rule proposals from feedback analysis
    rule_proposals: list[dict] = []

    def load_runs(self) -> None:
        """Load all runs from the database."""
        store = _get_store()
        self.runs = store.get_runs()
        self._load_rule_proposals()
        if self.runs and self.selected_run_id == 0:
            self.selected_run_id = self.runs[0]["id"]
            self._load_items()

    def _load_rule_proposals(self) -> None:
        """Load rule proposals from feedback analysis."""
        store = _get_store()
        proc = FeedbackProcessor(store)
        self.rule_proposals = proc.get_rule_proposals()

    def dismiss_proposal(self, index: int) -> None:
        """Remove a proposal from the list."""
        if 0 <= index < len(self.rule_proposals):
            self.rule_proposals = [
                p for i, p in enumerate(self.rule_proposals) if i != index
            ]

    def select_run(self, value: str) -> None:
        """Handle run selector change."""
        self.selected_run_id = int(value)
        self._load_items()

    def _load_items(self) -> None:
        """Load pending items for the selected run."""
        if self.selected_run_id == 0:
            self.items = []
            self.pending_count = 0
            return
        store = _get_store()
        # Show propose + review items that haven't been decided yet
        all_items = store.get_items(self.selected_run_id)
        self.items = [
            i for i in all_items
            if i.get("action") in ("propose", "review")
            and i.get("user_decision") is None
        ]
        self.pending_count = len(self.items)

    def open_detail(self, item_id: int) -> None:
        """Open the detail dialog for an item."""
        store = _get_store()
        item = store.get_item(item_id)
        if item is None:
            return
        self.detail_item = item
        self.edit_name = item.get("suggested_name") or ""
        self.edit_category = item.get("suggested_category") or ""
        self.edit_database = item.get("target_database") or ""
        self.edit_tags = ", ".join(item.get("tags") or [])
        self.show_detail = True

    def close_detail(self) -> None:
        """Close the detail dialog."""
        self.show_detail = False
        self.detail_item = {}

    def handle_dialog_open_change(self, is_open: bool) -> None:
        """Handle dialog open/close from the UI (e.g. clicking overlay)."""
        if not is_open:
            self.show_detail = False
            self.detail_item = {}

    def set_edit_name(self, value: str) -> None:
        """Update editable name field."""
        self.edit_name = value

    def set_edit_category(self, value: str) -> None:
        """Update editable category field."""
        self.edit_category = value

    def set_edit_database(self, value: str) -> None:
        """Update editable database field."""
        self.edit_database = value

    def set_edit_tags(self, value: str) -> None:
        """Update editable tags field."""
        self.edit_tags = value

    def accept_item(self, item_id: int) -> None:
        """Accept an item: save edits, record decision, refresh list."""
        store = _get_store()

        # Save any edits
        tags_list = [t.strip() for t in self.edit_tags.split(",") if t.strip()]
        store.update_item_fields(item_id, {
            "suggested_name": self.edit_name,
            "suggested_category": self.edit_category,
            "target_database": self.edit_database,
            "tags": tags_list,
        })

        store.set_decision(item_id, "accepted")
        self.show_detail = False
        self.detail_item = {}
        self._load_items()

    def reject_item(self, item_id: int) -> None:
        """Reject an item: record decision, refresh list."""
        store = _get_store()
        store.set_decision(item_id, "rejected")
        self.show_detail = False
        self.detail_item = {}
        self._load_items()

    def quick_accept(self, item_id: int) -> None:
        """Accept directly from the table (no edits)."""
        store = _get_store()
        store.set_decision(item_id, "accepted")
        self._load_items()

    def quick_reject(self, item_id: int) -> None:
        """Reject directly from the table."""
        store = _get_store()
        store.set_decision(item_id, "rejected")
        self._load_items()
