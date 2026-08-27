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
from ..storage import lock
from ..intelligence.router import ROUTING_TABLE
from ..intelligence.feedback import FeedbackProcessor


# All possible target databases (from the routing table)
DATABASE_OPTIONS: list[str] = sorted(set(ROUTING_TABLE.values()))

# Retry budget for the /v1/models call. See DigestState._get_models for why a
# single attempt is not enough over the SSH tunnel.
_MODEL_FETCH_ATTEMPTS = 3
_MODEL_FETCH_BACKOFF = 0.4  # seconds, multiplied by the attempt number
# Hard ceiling on the whole retry loop. Page load calls this synchronously, so
# a dead tunnel (every attempt burning the full connect timeout) must not hold
# the UI for attempts x timeout seconds.
_MODEL_FETCH_DEADLINE = 12.0


def _lm_studio_error(exc: Exception) -> str:
    """
    Status-bar message for a failed model fetch, named after the actual cause.

    "LM Studio is off" does not reach us as a refusal. socat on the VPS listens
    on 11234 independently of the tunnel and always accepts, then finds nothing
    behind 127.0.0.1:22236 and closes without sending a byte; the same happens
    one hop further when the tunnel is up but LM Studio is not. httpx calls that
    RemoteProtocolError or ReadError -- "Server disconnected without sending a
    response" -- which says nothing about the real problem. Measured over 14
    days of tunnel.log, 99.4% of tunnel restarts happened while LM Studio was
    not listening, so that signature means "not running", not "flaky network".
    """
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError,
                        httpx.ReadError, httpx.WriteError)):
        return "LM Studio doesn't appear to be running -- start it and try again"
    if isinstance(exc, httpx.TimeoutException):
        return "LM Studio did not answer in time -- still loading a model?"
    return f"Could not read the model list: {type(exc).__name__}"


def _get_store() -> DigestStore:
    """Create a fresh DigestStore connection (thread-safe)."""
    return DigestStore()


class DedupMatch(rx.Base):
    """Typed model for dedup match entries (needed for rx.foreach)."""
    name: str = ""
    database: str = ""


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
    detail_dedup_matches: list[DedupMatch] = []

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

    @rx.var(cache=True)
    def model_options(self) -> list[str]:
        """All model options including 'auto' for the selector.

        Keeps whatever is currently selected in the list even when the fetch
        failed and available_models is empty, so a dropped model list does not
        blank out the selector or silently reset the choice.
        """
        options = ["auto"] + self.available_models
        if self.selected_model and self.selected_model not in options:
            options.append(self.selected_model)
        return options

    # Pipeline trigger
    pipeline_running: bool = False
    pipeline_status: str = ""
    _force_stopped: bool = False

    # Notion write
    writing_to_notion: bool = False
    write_status: str = ""
    accepted_count: int = 0

    def _check_lock_file(self) -> bool:
        """Check whether a pipeline run is in progress (see src/storage/lock.py)."""
        return lock.is_locked()

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

    def _build_models_url(self) -> str:
        """Build the LM Studio /v1/models URL from config."""
        base_url = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
        api_base = base_url.rstrip("/")
        if api_base.endswith("/v1"):
            return f"{api_base}/models"
        return f"{api_base}/v1/models"

    def _get_models(self) -> list[str]:
        """
        Ask LM Studio for its chat models, retrying transient transport errors.

        The connection to LM Studio is a chain of hops -- container ->
        socat on the VPS -> SSH reverse tunnel -> LM Studio on the PC -- and any
        of them can accept a TCP connection and then drop it before answering.
        httpx reports that as RemoteProtocolError ("Server disconnected without
        sending a response") or ReadError. A refused connection is different --
        that is a definite "LM Studio is off", so it is not retried. The pipeline never sees those: the scorer talks to
        the same /v1/models endpoint through the OpenAI SDK, which keeps a warm
        pooled connection and retries connection errors twice on its own. This
        call used to be a bare one-shot httpx.get, which is why only the UI
        complained. Retry here too, and give the read leg more than 5s -- the PC
        answers slowly while it is busy scoring.

        Raises the last httpx error if every attempt fails.
        """
        models_url = self._build_models_url()
        timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
        deadline = time.monotonic() + _MODEL_FETCH_DEADLINE
        last_exc: Exception | None = None
        with httpx.Client(timeout=timeout) as client:
            for attempt in range(_MODEL_FETCH_ATTEMPTS):
                try:
                    resp = client.get(models_url)
                    resp.raise_for_status()
                    data = resp.json()
                    models = [m["id"] for m in data.get("data", []) if m.get("id")]
                    # Filter out embedding models (not usable for scoring)
                    return sorted(m for m in models if "embed" not in m.lower())
                except httpx.ConnectError:
                    # Refused / no route: a definite "not running", and each
                    # attempt costs a full SYN timeout. Report it immediately.
                    raise
                except httpx.TransportError as exc:
                    last_exc = exc
                    backoff = _MODEL_FETCH_BACKOFF * (attempt + 1)
                    if attempt == _MODEL_FETCH_ATTEMPTS - 1:
                        break
                    if time.monotonic() + backoff >= deadline:
                        break  # out of budget; report the failure now
                    time.sleep(backoff)
        raise last_exc  # type: ignore[misc]

    def _apply_models(
        self, models: list[str], exc: Exception | None, quiet: bool
    ) -> bool:
        """
        Store the outcome of a model fetch.

        Split out from the network call so the blocking and background paths
        share one place that decides what the status bar says.

        Returns:
            True if LM Studio answered (even with an empty list), False if it
            could not be reached at all.
        """
        if exc is None:
            self.available_models = models
            if not quiet and not models:
                self.pipeline_status = "LM Studio running but no chat models loaded"
            elif self.pipeline_status and "LM Studio" in self.pipeline_status:
                # Clear stale LM Studio error now that models loaded OK
                self.pipeline_status = ""
            return True
        self.available_models = []
        if not quiet:
            self.pipeline_status = _lm_studio_error(exc)
        return False

    async def _fetch_models_bg(self, quiet: bool = False) -> None:
        """
        Fetch models off the event loop.

        LM Studio is normally off, and a fetch against a dead tunnel takes a
        second or two to give up. Doing that inline would freeze the UI, so the
        blocking call runs in a thread and only the result touches state.

        models_loading doubles as an in-flight guard, so it is cleared in a
        finally: leaving it set would lock out every later fetch.
        """
        async with self:
            if self.models_loading:
                return  # a fetch is already in flight
            self.models_loading = True
        try:
            models: list[str] = []
            exc: Exception | None = None
            try:
                models = await asyncio.to_thread(self._get_models)
            except Exception as err:
                exc = err
            async with self:
                self._apply_models(models, exc, quiet)
        finally:
            async with self:
                self.models_loading = False

    @rx.event(background=True)
    async def fetch_models(self):
        """Refresh button: always report what happened."""
        await self._fetch_models_bg(quiet=False)

    @rx.event(background=True)
    async def fetch_models_on_open(self, is_open: bool):
        """
        Load the model list when the dropdown is opened, not before.

        The UI is mostly used to review items, with LM Studio switched off, so
        probing it on page load only produced errors for a server that was
        never meant to be running yet.
        """
        if is_open:
            await self._fetch_models_bg(quiet=False)

    def set_selected_model(self, value: str) -> None:
        """Set the selected model for the next pipeline run."""
        self.selected_model = value

    def check_pipeline_status(self) -> None:
        """Check if pipeline is still running (via the shared lock)."""
        was_running = self.pipeline_running
        self.pipeline_running = self._check_lock_file()
        if was_running and not self.pipeline_running:
            # A force-stopped run also ends here; don't report it as completed.
            self.pipeline_status = "Force stopped" if self._force_stopped else "Complete!"
            self._force_stopped = False
            self._reload_runs()

    def _reload_runs(self) -> None:
        """Reload runs list without resetting selected run."""
        store = _get_store()
        self.runs = store.get_runs()
        self._load_rule_proposals()
        if self.selected_run_id:
            self._load_items()

    def force_stop_pipeline(self) -> None:
        """
        Ask the running pipeline to stop.

        Only signals; the run releases its own lock once it has wound down. The
        old version deleted the lock file here, which freed the button while the
        old thread kept going — so a second run could start on top of the first,
        and both kept extracting and scoring.
        """
        if not lock.request_cancel():
            self.pipeline_running = False
            self.pipeline_status = "No run to stop"
            return
        self.pipeline_status = "Stopping — waiting for the current step to finish..."
        self._force_stopped = True

    @rx.event(background=True)
    async def trigger_pipeline(self):
        """Start the pipeline in a background thread and poll until done."""
        async with self:
            if self._check_lock_file():
                self.pipeline_status = "Pipeline already running"
                self.pipeline_running = True
                return
            local_backend = os.environ.get("SCORER_BACKEND", "local") == "local"
            if local_backend:
                self.pipeline_status = "Checking LM Studio..."

        # Pre-flight: look at LM Studio, but only refuse to start on a definite
        # "no". A failed model list is not proof that LM Studio is down -- the
        # relay reports a dead tunnel and a briefly-restarting one identically
        # (see _lm_studio_error) -- and blocking the run on it left no way to
        # start at all. The scorer does its own preflight_check and fails the
        # run fast with a clear message if the server really is unreachable.
        # Run off the event loop: this is a network call behind a state lock.
        warning = ""
        if local_backend:
            reached: bool
            try:
                models = await asyncio.to_thread(self._get_models)
                reached, exc = True, None
            except Exception as err:
                models, reached, exc = [], False, err
            async with self:
                self._apply_models(models, exc, quiet=True)
                if reached and not models:
                    self.pipeline_status = "Cannot start: LM Studio has no chat models loaded"
                    return
            if not reached:
                warning = " (model list unavailable -- starting anyway)"

        async with self:
            self.pipeline_running = True
            self.pipeline_status = "Starting pipeline..." + warning
            self._force_stopped = False
            model = self.selected_model

        thread_error: list[str] = []  # mutable container to capture errors from thread

        def _run_in_thread(model_name: str):
            import sys
            import traceback
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            try:
                from scripts.run_weekly import run_pipeline
                m = None if model_name == "auto" else model_name or None
                asyncio.run(run_pipeline(model=m))
            except Exception as exc:
                tb = traceback.format_exc()
                thread_error.append(f"{exc}")
                print(f"\n*** PIPELINE ERROR ***\n{tb}")

        t = threading.Thread(target=_run_in_thread, args=(model,), daemon=True)
        t.start()

        # Poll every 3 seconds until the run really ends. A force stop no longer
        # breaks out early: the thread is still winding down, and pretending it
        # is finished is what allowed two runs to overlap.
        while t.is_alive():
            async with self:
                if not self._force_stopped:
                    progress = self._read_progress_file()
                    if progress:
                        self.pipeline_status = progress
            await asyncio.sleep(3)

        async with self:
            if self._force_stopped:
                self._force_stopped = False
                self.pipeline_running = False
                self.pipeline_status = "Force stopped"
                self._reload_runs()
            elif thread_error:
                self.pipeline_running = False
                self.pipeline_status = f"ERROR: {thread_error[0]}"
                self._reload_runs()
            else:
                self.pipeline_running = False
                self.pipeline_status = "Complete!"
                self._reload_runs()
                # Auto-select the newest run
                if self.runs:
                    self.selected_run_id = self.runs[0]["id"]
                    self._load_items()

    @rx.event(background=True)
    async def resume_progress_if_running(self):
        """Re-attach the live progress display to an already-running pipeline.

        The progress poll in trigger_pipeline() is bound to the browser session
        that launched the run. After a disconnect (e.g. the PC sleeps) or a plain
        page refresh, that loop is gone, so the live "Scoring (x/y items)" status
        would not reappear even though the run is still going on the server. On
        page load, if the lock file shows a run in progress, resume polling the
        progress file so the status (and how far along it is) comes back.
        """
        async with self:
            if not self._check_lock_file():
                return
            self.pipeline_running = True
            progress = self._read_progress_file()
            if progress:
                self.pipeline_status = progress

        # Poll every 3 seconds until the run finishes (lock file removed)
        while True:
            await asyncio.sleep(3)
            async with self:
                if not self._check_lock_file():
                    break
                progress = self._read_progress_file()
                if progress:
                    self.pipeline_status = progress

        async with self:
            self.pipeline_running = False
            if self._force_stopped:
                self._force_stopped = False
                self.pipeline_status = "Force stopped"
            else:
                self.pipeline_status = "Complete!"
            self._reload_runs()
            # Auto-select the newest run so results appear without a manual refresh
            if self.runs:
                self.selected_run_id = self.runs[0]["id"]
                self._load_items()

    def load_runs(self) -> None:
        """Load all runs from the database."""
        self.check_pipeline_status()
        # No model fetch here: see fetch_models_on_open.
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
        self.detail_dedup_matches = [
            DedupMatch(name=m.get("name", ""), database=m.get("database", ""))
            for m in (item.get("dedup_matches") or [])
        ]
        self.edit_name = item.get("suggested_name") or ""
        self.edit_category = item.get("suggested_category") or ""
        self.edit_database = item.get("target_database") or ""
        self.edit_tags = ", ".join(item.get("tags") or [])
        self.show_detail = True

    def close_detail(self) -> None:
        """Close the detail dialog."""
        self.show_detail = False
        self.detail_item = {}
        self.detail_dedup_matches = []

    def handle_dialog_open_change(self, is_open: bool) -> None:
        """Handle dialog open/close from the UI (e.g. clicking overlay)."""
        if not is_open:
            self.show_detail = False
            self.detail_item = {}
            self.detail_dedup_matches = []

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
