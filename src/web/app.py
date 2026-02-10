"""
Reflex web app for the Newsletter Curator review UI.

Single page with run selector, items table, and detail dialog.
"""

import asyncio
import os
import sys
import threading
from pathlib import Path

import reflex as rx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse

from .state import DigestState, DATABASE_OPTIONS


# ── Helper components ────────────────────────────────────────


def score_badge(score) -> rx.Component:
    """Colored badge for the score value."""
    s = score.to(int)
    return rx.cond(
        s >= 5,
        rx.badge(s, color_scheme="green", variant="solid"),
        rx.cond(
            s >= 3,
            rx.badge(s, color_scheme="blue", variant="solid"),
            rx.cond(
                s >= 1,
                rx.badge(s, color_scheme="yellow", variant="solid"),
                rx.badge(s, color_scheme="red", variant="solid"),
            ),
        ),
    )


def verdict_badge(verdict) -> rx.Component:
    """Colored badge for the verdict."""
    v = verdict.to(str)
    return rx.cond(
        v == "strong_fit",
        rx.badge("strong fit", color_scheme="green"),
        rx.cond(
            v == "likely_fit",
            rx.badge("likely fit", color_scheme="blue"),
            rx.cond(
                v == "maybe",
                rx.badge("maybe", color_scheme="yellow"),
                rx.badge(v, color_scheme="red"),
            ),
        ),
    )


def item_row(item: dict) -> rx.Component:
    """One row in the items table."""
    return rx.table.row(
        rx.table.cell(score_badge(item["score"])),
        rx.table.cell(
            rx.link(
                rx.text(item["suggested_name"], weight="medium"),
                on_click=DigestState.open_detail(item["id"]),
                cursor="pointer",
                _hover={"text_decoration": "underline"},
            ),
        ),
        rx.table.cell(rx.text(item["item_type"], size="2", color="gray")),
        rx.table.cell(rx.text(item["target_database"], size="2")),
        rx.table.cell(verdict_badge(item["verdict"])),
        rx.table.cell(
            rx.hstack(
                rx.button(
                    "Accept",
                    size="1",
                    color_scheme="green",
                    variant="soft",
                    on_click=DigestState.quick_accept(item["id"]),
                ),
                rx.button(
                    "Reject",
                    size="1",
                    color_scheme="red",
                    variant="soft",
                    on_click=DigestState.quick_reject(item["id"]),
                ),
                spacing="2",
            ),
        ),
    )


def items_table() -> rx.Component:
    """The main items table."""
    return rx.table.root(
        rx.table.header(
            rx.table.row(
                rx.table.column_header_cell("Score"),
                rx.table.column_header_cell("Name"),
                rx.table.column_header_cell("Type"),
                rx.table.column_header_cell("Database"),
                rx.table.column_header_cell("Verdict"),
                rx.table.column_header_cell("Actions"),
            ),
        ),
        rx.table.body(
            rx.foreach(DigestState.items, item_row),
        ),
        width="100%",
    )


def proposal_card(proposal, index) -> rx.Component:
    """Single rule proposal card."""
    return rx.card(
        rx.hstack(
            rx.box(
                rx.text(proposal["proposal"], size="2"),
                rx.text(
                    "Evidence: "
                    + proposal["evidence_count"].to(str)
                    + " overrides",
                    size="1",
                    color="gray",
                ),
                flex="1",
            ),
            rx.button(
                "Dismiss",
                size="1",
                variant="ghost",
                color_scheme="gray",
                on_click=DigestState.dismiss_proposal(index),
            ),
            align="center",
            width="100%",
        ),
        background="var(--yellow-2)",
        border="1px solid var(--yellow-6)",
        width="100%",
    )


def rule_proposals_section() -> rx.Component:
    """Section showing rule proposals from feedback analysis."""
    return rx.cond(
        DigestState.rule_proposals.length() > 0,
        rx.box(
            rx.hstack(
                rx.text("Rule Proposals", weight="bold", size="3"),
                rx.badge(
                    DigestState.rule_proposals.length().to(str),
                    color_scheme="yellow",
                    variant="solid",
                    size="1",
                ),
                align="center",
                spacing="2",
                margin_bottom="8px",
            ),
            rx.foreach(
                DigestState.rule_proposals,
                lambda proposal, idx: proposal_card(proposal, idx),
            ),
            margin_bottom="16px",
        ),
        rx.fragment(),
    )


def run_selector() -> rx.Component:
    """Dropdown to select which processing run to review."""
    return rx.select.root(
        rx.select.trigger(placeholder="Select a run..."),
        rx.select.content(
            rx.foreach(
                DigestState.runs,
                lambda run: rx.select.item(
                    rx.text(
                        "Run #"
                        + run["id"].to(str)
                        + " - "
                        + run["status"].to(str)
                        + " ("
                        + run["items_proposed"].to(str)
                        + " proposed)",
                    ),
                    value=run["id"].to(str),
                ),
            ),
        ),
        value=DigestState.selected_run_id.to(str),
        on_change=DigestState.select_run,
    )


def detail_dialog() -> rx.Component:
    """Detail dialog for reviewing a single item."""
    item = DigestState.detail_item

    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title(
                rx.hstack(
                    rx.text("Review Item"),
                    score_badge(item["score"]),
                    verdict_badge(item["verdict"]),
                    align="center",
                    spacing="3",
                ),
            ),

            # Source info
            rx.cond(
                item["url"],
                rx.box(
                    rx.link(
                        item["url"],
                        href=item["url"],
                        is_external=True,
                        size="2",
                        color="blue",
                    ),
                    margin_bottom="12px",
                ),
                rx.fragment(),
            ),

            rx.cond(
                item["email_subject"],
                rx.text(
                    "From: " + item["email_subject"].to(str),
                    size="2",
                    color="gray",
                    margin_bottom="12px",
                ),
                rx.fragment(),
            ),

            # Reasoning
            rx.box(
                rx.text("Reasoning", weight="bold", size="2"),
                rx.text(item["reasoning"], size="2"),
                margin_bottom="16px",
            ),

            # Article text preview
            rx.cond(
                item["text"],
                rx.box(
                    rx.text("Article Preview", weight="bold", size="2"),
                    rx.text(
                        item["text"],
                        size="1",
                        color="gray",
                    ),
                    padding="8px",
                    border_radius="4px",
                    background="var(--gray-2)",
                    margin_bottom="16px",
                    max_height="120px",
                    overflow_y="auto",
                ),
                rx.fragment(),
            ),

            rx.separator(margin_y="12px"),

            # Editable fields
            rx.text("Edit Fields", weight="bold", size="3", margin_bottom="8px"),

            rx.box(
                rx.text("Name", size="2", weight="medium"),
                rx.input(
                    value=DigestState.edit_name,
                    on_change=DigestState.set_edit_name,
                ),
                margin_bottom="8px",
            ),

            rx.box(
                rx.text("Category", size="2", weight="medium"),
                rx.input(
                    value=DigestState.edit_category,
                    on_change=DigestState.set_edit_category,
                ),
                margin_bottom="8px",
            ),

            rx.box(
                rx.text("Target Database", size="2", weight="medium"),
                rx.select.root(
                    rx.select.trigger(placeholder="Select database..."),
                    rx.select.content(
                        *[
                            rx.select.item(db, value=db)
                            for db in DATABASE_OPTIONS
                        ],
                    ),
                    value=DigestState.edit_database,
                    on_change=DigestState.set_edit_database,
                ),
                margin_bottom="8px",
            ),

            rx.box(
                rx.text("Tags (comma-separated)", size="2", weight="medium"),
                rx.input(
                    value=DigestState.edit_tags,
                    on_change=DigestState.set_edit_tags,
                ),
                margin_bottom="16px",
            ),

            # Action buttons
            rx.hstack(
                rx.button(
                    "Accept",
                    color_scheme="green",
                    on_click=DigestState.accept_item(item["id"]),
                ),
                rx.button(
                    "Reject",
                    color_scheme="red",
                    variant="soft",
                    on_click=DigestState.reject_item(item["id"]),
                ),
                rx.dialog.close(
                    rx.button("Cancel", variant="outline"),
                ),
                spacing="3",
                justify="end",
                width="100%",
            ),

            max_width="600px",
        ),
        open=DigestState.show_detail,
        on_open_change=DigestState.handle_dialog_open_change,
    )


# ── Main page ────────────────────────────────────────────────


@rx.page(route="/", on_load=DigestState.load_runs)
def index() -> rx.Component:
    """Main review page."""
    return rx.box(
        rx.container(
            # Header
            rx.hstack(
                rx.heading("Newsletter Curator", size="6"),
                rx.spacer(),
                rx.hstack(
                    rx.cond(
                        DigestState.pipeline_status,
                        rx.text(
                            DigestState.pipeline_status,
                            size="2",
                            color="gray",
                        ),
                        rx.fragment(),
                    ),
                    rx.cond(
                        DigestState.pipeline_running,
                        rx.button(
                            rx.spinner(size="1"),
                            "Running...",
                            size="2",
                            variant="soft",
                            disabled=True,
                        ),
                        rx.button(
                            "Run Pipeline",
                            size="2",
                            variant="solid",
                            color_scheme="purple",
                            on_click=DigestState.trigger_pipeline,
                        ),
                    ),
                    rx.badge(
                        DigestState.pending_count.to(str) + " pending",
                        color_scheme="blue",
                        variant="solid",
                        size="2",
                    ),
                    align="center",
                    spacing="3",
                ),
                align="center",
                padding_y="16px",
            ),

            rx.separator(margin_bottom="16px"),

            # Rule proposals (from feedback analysis)
            rule_proposals_section(),

            # Run selector + filter toggle
            rx.hstack(
                rx.text("Processing Run:", weight="medium", size="3"),
                run_selector(),
                rx.spacer(),
                rx.hstack(
                    rx.switch(
                        checked=DigestState.show_all_items,
                        on_change=DigestState.toggle_show_all,
                        size="1",
                    ),
                    rx.text(
                        "Show all ("
                        + DigestState.total_count.to(str)
                        + " items)",
                        size="2",
                        color="gray",
                    ),
                    align="center",
                    spacing="2",
                ),
                align="center",
                spacing="3",
                margin_bottom="16px",
            ),

            # Items table or empty state
            rx.cond(
                DigestState.pending_count > 0,
                items_table(),
                rx.box(
                    rx.text(
                        "No pending items.",
                        size="4",
                        color="gray",
                        align="center",
                    ),
                    padding_y="48px",
                    text_align="center",
                ),
            ),

            # Detail dialog (always rendered, shown/hidden via state)
            detail_dialog(),

            size="4",
            padding_y="24px",
        ),
    )


# ── API endpoints for curl access ────────────────────────────

_DATA_DIR = os.environ.get("DATA_DIR", ".")
_LOCK_FILE = os.path.join(_DATA_DIR, ".pipeline_running")
_LOCK_STALE_SECONDS = 30 * 60


def _is_locked() -> bool:
    """Check if the pipeline lock file exists and is not stale."""
    if not os.path.exists(_LOCK_FILE):
        return False
    import time
    try:
        age = time.time() - os.path.getmtime(_LOCK_FILE)
        if age > _LOCK_STALE_SECONDS:
            return False
    except OSError:
        return False
    return True


def _start_pipeline_thread():
    """Start the pipeline in a background thread."""
    def _run():
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from scripts.run_weekly import run_pipeline
        asyncio.run(run_pipeline())

    t = threading.Thread(target=_run, daemon=True)
    t.start()


async def _api_pipeline_trigger(request):
    """Trigger the pipeline via HTTP."""
    if _is_locked():
        return JSONResponse({"status": "already_running"})
    _start_pipeline_thread()
    return JSONResponse({"status": "started"})


async def _api_pipeline_status(request):
    """Check pipeline status via HTTP."""
    from ..storage.digest import DigestStore
    running = _is_locked()
    store = DigestStore()
    runs = store.get_runs()
    last_run = runs[0] if runs else None
    return JSONResponse({
        "running": running,
        "last_run": last_run,
    })


_custom_api = Starlette(routes=[
    Route("/api/pipeline/trigger", _api_pipeline_trigger, methods=["GET"]),
    Route("/api/pipeline/status", _api_pipeline_status, methods=["GET"]),
])

app = rx.App(api_transformer=_custom_api)
