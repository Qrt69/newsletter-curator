"""
Starting a pipeline run as its own process.

The pipeline used to run as a thread inside the web process, which is why every
run left ~435MB of anonymous memory behind in the granian worker that happened
to serve the request: after run 283 that worker sat at 520MB of anonymous
memory against 105MB for its 15 idle siblings, and a `gc.collect()` plus
`malloc_trim(0)` at the end of a run gave only 40MB of it back. Whether the rest
is a live leak or glibc holding on to per-thread arenas was never settled -- and
no longer has to be, which is the point. A child process hands its entire
address space back to the kernel when it exits, so a run both starts and ends
with clean memory whichever of the two it was.

Nothing else about a run had to move. The lock, the cancel signal, the progress
display and the log are all files under DATA_DIR (see src/storage/lock.py), so
they work across processes exactly as they worked across threads. The child
takes the lock itself, so the pid in the lock file is the pid actually doing the
work -- which makes `lock.is_locked()`'s liveness check meaningful for the first
time: a crashed run is now detectably gone instead of leaving the web process
holding a lock for work that is no longer happening.
"""

import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _PROJECT_ROOT / "scripts" / "run_weekly.py"

DATA_DIR = os.environ.get("DATA_DIR", ".")
# Whatever the run writes to stdout/stderr. Structured logging already goes to
# pipeline.log; this is for tracebacks and prints, and is what the UI quotes
# when a run dies. A file rather than a PIPE on purpose: nobody drains a pipe
# while the run is going, and a full one would block the pipeline mid-run.
OUTPUT_FILE = os.path.join(DATA_DIR, ".pipeline_output")


def start(model: str | None = None) -> subprocess.Popen:
    """
    Launch a pipeline run in a separate process and return it immediately.

    The caller decides how to wait: the UI polls so it can keep showing
    progress, the API endpoint just reaps (see app.py).
    """
    cmd = [sys.executable, str(_SCRIPT)]
    if model and model != "auto":
        cmd += ["--model", model]

    # Truncated per run: the previous run's traceback must not be reported as
    # this one's.
    out = open(OUTPUT_FILE, "w")

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # Own process group, so a signal aimed at the web process cannot take
        # the run with it, and so Chromium's children can be cleaned up as a
        # group rather than one pid at a time.
        kwargs["start_new_session"] = True

    try:
        return subprocess.Popen(
            cmd,
            cwd=str(_PROJECT_ROOT),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
            **kwargs,
        )
    finally:
        # Popen dup'd the descriptor; the child keeps writing after we let go.
        out.close()


def error_tail(limit: int = 400) -> str:
    """Last of the run's output, for reporting a failed run in the UI."""
    try:
        with open(OUTPUT_FILE, "r", errors="replace") as fh:
            text = fh.read().strip()
    except OSError:
        return ""
    return text[-limit:]
