"""
Reflex state classes for the Newsletter Curator review UI.

DigestState holds the list of runs and items.
Event handlers load data from SQLite, handle accept/reject/edit.
"""

import asyncio
import os
import threading
import time

import httpx
import reflex as rx

from ..storage.digest import DigestStore
from ..intelligence.router import ROUTING_TABLE
from ..intelligence.feedback import FeedbackProcessor


# All possible target databases (from the routing table)
DATABASE_OPTIONS: list[str] = sorted(set(ROUTING_TABLE.values()))


def _get_store() -> DigestStore:
    """Create a fresh DigestStore connection (thread-safe)."""
    return DigestStore()


class DigestState(rx.State):
    """Top-level state: run selection, items list, detail dialog."""

    # Run data
    runs: list[dict] = []
    selected_run_id: int = 0
    run_label: str = ""

    # Items for current run
    items: list[dict] = []
    pending_count: int = 0
    show_all_items: bool = False
    sort_by_score: str = ""  # "", "desc", or "asc"
    total_count: int = 0

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

    # Model selector
    selected_model: str = "auto"
    available_models: list[str] = []
    models_loading: bool = False

    # Pipeline trigger
    pipeline_running: bool = False
    pipeline_status: str = ""
    _force_stopped: bool = False

    # Notion write
    writing_to_notion: bool = False
    write_status: str = ""
    accepted_count: int = 0

    def _check_lock_file(self) -> bool:
        """Check if the pipeline lock file exists."""
        data_dir = os.environ.get("DATA_DIR", ".")
        lock_path = os.path.join(data_dir, ".pipeline_running")
        return os.path.exists(lock_path)

    @staticmethod
    def _read_progress_file() -> str:
        """Read the pipeline progress file, returning its content or empty string."""
        data_dir = os.environ.get("DATA_DIR", ".")
        progress_path = os.path.join(data_dir, ".pipeline_progress")
        try:
            with open(progress_path, "r") as f:
                return f.read().strip()
        except OSError:
            return ""

    def fetch_models(self) -> None:
        """Fetch available models from LM Studio /v1/models endpoint."""
        self.models_loading = True
        base_url = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
        try:
            resp = httpx.get(f"{base_url}/models", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", []) if m.get("id")]
            self.available_models = sorted(models)
        except Exception:
            self.available_models = []
        self.models_loading = False

    def set_selected_model(self, value: str) -> None:
        """Set the selected model for the next pipeline run."""
        self.selected_model = value

    def check_pipeline_status(self) -> None:
        """Check if pipeline is still running (via lock file)."""
        was_running = self.pipeline_running
        self.pipeline_running = self._check_lock_file()
        if was_running and not self.pipeline_running:
            self.pipeline_status = "Complete!"
            self._reload_runs()

    def _reload_runs(self) -> None:
        """Reload runs list without resetting selected run."""
        store = _get_store()
        self.runs = store.get_runs()
        self._load_rule_proposals()
        if self.selected_run_id:
            self._load_items()

    def force_stop_pipeline(self) -> None:
        """Force-stop the pipeline by removing the lock file."""
        data_dir = os.environ.get("DATA_DIR", ".")
        lock_path = os.path.join(data_dir, ".pipeline_running")
        try:
            os.remove(lock_path)
        except OSError:
            pass
        self.pipeline_running = False
        self.pipeline_status = "Force stopped"
        self._force_stopped = True

    @rx.event(background=True)
    async def trigger_pipeline(self):
        """Start the pipeline in a background thread and poll until done."""
        async with self:
            if self._check_lock_file():
                self.pipeline_status = "Pipeline already running"
                self.pipeline_running = True
                return

            self.pipeline_running = True
            self.pipeline_status = "Running..."
            self._force_stopped = False
            model = self.selected_model

        def _run_in_thread(model_name: str):
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            from scripts.run_weekly import run_pipeline
            m = None if model_name == "auto" else model_name or None
            asyncio.run(run_pipeline(model=m))

        t = threading.Thread(target=_run_in_thread, args=(model,), daemon=True)
        t.start()

        # Poll every 3 seconds until pipeline finishes or force-stopped
        while t.is_alive():
            async with self:
                if self._force_stopped:
                    break
                progress = self._read_progress_file()
                if progress:
                    self.pipeline_status = progress
            await asyncio.sleep(3)

        async with self:
            if self._force_stopped:
                # Force stop already set status; just reload runs
                self._force_stopped = False
                self._reload_runs()
            else:
                self.pipeline_running = False
                self.pipeline_status = "Complete!"
                self._reload_runs()
                # Auto-select the newest run
                if self.runs:
                    self.selected_run_id = self.runs[0]["id"]
                    self._load_items()

    def load_runs(self) -> None:
        """Load all runs from the database."""
        self.check_pipeline_status()
        self.fetch_models()
        store = _get_store()
        # Silent cleanup of old rejected/skipped items on page load
        store.cleanup_old_items()
        self.runs = store.get_runs()
        self._load_rule_proposals()
        if self.runs and self.selected_run_id == 0:
            self.selected_run_id = self.runs[0]["id"]
            self._load_items()

    def _load_rule_proposals(self) -> None:
        """Load rule proposals from feedback analysis, excluding dismissed ones."""
        store = _get_store()
        proc = FeedbackProcessor(store)
        dismissed = store.get_dismissed_proposals()
        self.rule_proposals = [
            p for p in proc.get_rule_proposals()
            if (p["detail"], p["type"]) not in dismissed
        ]

    def dismiss_proposal(self, index: int) -> None:
        """Permanently dismiss a proposal so it won't reappear."""
        if 0 <= index < len(self.rule_proposals):
            proposal = self.rule_proposals[index]
            store = _get_store()
            store.dismiss_proposal(proposal["detail"], proposal["type"])
            self.rule_proposals = [
                p for i, p in enumerate(self.rule_proposals) if i != index
            ]

    def select_run(self, value: str) -> None:
        """Handle run selector change."""
        self.selected_run_id = int(value)
        self._load_items()

    def toggle_show_all(self, checked: bool) -> None:
        """Toggle between showing only proposed items and all items."""
        self.show_all_items = checked
        self._load_items()

    def toggle_sort_score(self) -> None:
        """Cycle score sort: unsorted -> desc -> asc -> unsorted."""
        if self.sort_by_score == "":
            self.sort_by_score = "desc"
        elif self.sort_by_score == "desc":
            self.sort_by_score = "asc"
        else:
            self.sort_by_score = ""
        self._load_items()

    @rx.event(background=True)
    async def write_to_notion(self):
        """Write accepted items for the selected run to Notion (background task)."""
        async with self:
            if self.selected_run_id == 0 or self.writing_to_notion:
                return
            self.writing_to_notion = True
            self.write_status = "Writing..."
            run_id = self.selected_run_id

        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts.run_weekly import write_accepted

        try:
            result = write_accepted(run_id)
            created = result.get("created", 0) if isinstance(result, dict) else 0
            failed = result.get("failed", 0) if isinstance(result, dict) else 0
            async with self:
                if failed:
                    errors = result.get("errors", []) if isinstance(result, dict) else []
                    error_detail = "; ".join(errors) if errors else "unknown error"
                    self.write_status = f"Done: {created} created, {failed} failed — {error_detail}"
                else:
                    self.write_status = f"Written to Notion! ({created} items)"
        except Exception as exc:
            async with self:
                self.write_status = f"Error: {exc}"

        async with self:
            self.writing_to_notion = False
            self._update_accepted_count()

    def _update_accepted_count(self) -> None:
        """Count accepted items not yet written to Notion."""
        if self.selected_run_id == 0:
            self.accepted_count = 0
            return
        store = _get_store()
        self.accepted_count = len(store.get_accepted_items(self.selected_run_id))

    def _load_items(self) -> None:
        """Load items for the selected run."""
        if self.selected_run_id == 0:
            self.items = []
            self.pending_count = 0
            self.total_count = 0
            self.accepted_count = 0
            return
        store = _get_store()
        all_items = store.get_items(self.selected_run_id)
        undecided = [i for i in all_items if i.get("user_decision") is None]
        self.total_count = len(undecided)
        if self.show_all_items:
            # Show all undecided items (including skipped)
            self.items = [
                i for i in all_items
                if i.get("user_decision") is None
            ]
        else:
            # Show only proposed items that haven't been decided yet
            self.items = [
                i for i in all_items
                if i.get("action") == "propose"
                and i.get("user_decision") is None
            ]
        if self.sort_by_score == "desc":
            self.items.sort(key=lambda i: i.get("score", 0), reverse=True)
        elif self.sort_by_score == "asc":
            self.items.sort(key=lambda i: i.get("score", 0))
        self.pending_count = len(self.items)
        self._update_accepted_count()

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

    def dismiss_all(self) -> None:
        """Bulk-dismiss all undecided items in the selected run."""
        if self.selected_run_id == 0:
            return
        store = _get_store()
        store.dismiss_undecided(self.selected_run_id)
        self._load_items()
