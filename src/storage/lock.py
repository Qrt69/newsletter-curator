"""
Pipeline lock and cancel signalling.

One implementation, shared by the CLI (scripts/run_weekly.py) and the web layer
(src/web/app.py, src/web/state.py). There used to be three, each with its own
answer to "is a run in progress?":

  - run_weekly.py: stale after 30 minutes by mtime, and deletes the file
  - app.py:        stale after 30 minutes by mtime, but leaves the file
  - state.py:      the file existing at all means running

Runs take 50 to 113 minutes, so the 30-minute rule declared healthy runs dead
and let a second run start on top of the first. Liveness is decided by the
owning process now, not by the clock: the lock records the pid that holds it,
and a lock whose process is gone is stale however young it is.

Cancellation is addressed to one specific run. The old blanket cancel file let
a newly started run wipe the signal meant for the run it had stacked on top of,
and a leftover signal would otherwise cancel the *next* run instead.
"""

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass

DATA_DIR = os.environ.get("DATA_DIR", ".")
LOCK_FILE = os.path.join(DATA_DIR, ".pipeline_running")
CANCEL_FILE = os.path.join(DATA_DIR, ".pipeline_cancel")

# Only used when a lock was written in another PID namespace — the scheduler
# container shares /data with web, and its pids mean nothing here. A running
# pipeline heartbeats its lock, so silence this long means it is gone.
STALE_SECONDS = 15 * 60


@dataclass(frozen=True)
class LockInfo:
    pid: int
    host: str
    token: str


def _host() -> str:
    """Identity of the PID namespace we are in (the container id, in Docker)."""
    return socket.gethostname()


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid currently exists."""
    if pid <= 0:
        return False

    if os.name == "nt":
        # os.kill(pid, 0) on Windows does not probe — it calls TerminateProcess
        # and would kill the very process we are asking about.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell — assume alive rather than steal the lock
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return True
    return True


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def read() -> LockInfo | None:
    """Parse the lock file, or None if there is none."""
    try:
        with open(LOCK_FILE, "r") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None

    try:
        data = json.loads(raw)
        return LockInfo(
            pid=int(data.get("pid", 0)),
            host=str(data.get("host", "")),
            token=str(data.get("token", "")),
        )
    except (ValueError, TypeError):
        # Legacy format written by earlier versions: "<pid>-<token>". Host is
        # unknown, so liveness falls back to the heartbeat.
        pid, _, token = raw.partition("-")
        return LockInfo(pid=int(pid) if pid.isdigit() else 0, host="", token=token)


def is_locked() -> bool:
    """
    Whether a pipeline run is actually in progress.

    Removes the lock when its owning process is gone, so a crashed run cannot
    block the next one forever.
    """
    info = read()
    if info is None:
        return False

    if info.host and info.host == _host() and info.pid:
        if _pid_alive(info.pid):
            return True
        _remove(LOCK_FILE)
        return False

    try:
        age = time.time() - os.path.getmtime(LOCK_FILE)
    except OSError:
        return False
    if age > STALE_SECONDS:
        _remove(LOCK_FILE)
        return False
    return True


def acquire() -> str | None:
    """Take the lock. Returns a token identifying this run, or None if busy."""
    if is_locked():
        return None

    token = uuid.uuid4().hex[:12]
    payload = {"pid": os.getpid(), "host": _host(), "token": token, "started": time.time()}
    try:
        with open(LOCK_FILE, "w") as f:
            json.dump(payload, f)
    except OSError:
        return None

    # Any pending cancel now targets a run that no longer holds the lock — and
    # since we just acquired it, that run is gone. Dropping it here is what
    # keeps a force-stopped run from cancelling its successor, without a
    # blanket clear that would also wipe a live run's signal.
    target = _cancel_target()
    if target is not None and target != token:
        _remove(CANCEL_FILE)

    return token


def release(token: str | None = None):
    """Release the lock, unless it has since been taken by another run."""
    if token is not None:
        info = read()
        if info is not None and info.token and info.token != token:
            return
    _remove(LOCK_FILE)


def heartbeat(token: str | None = None):
    """Refresh the lock's mtime so cross-namespace staleness checks stay honest."""
    if token is not None:
        info = read()
        if info is None or (info.token and info.token != token):
            return
    try:
        os.utime(LOCK_FILE, None)
    except OSError:
        pass


# ── Cancellation ──────────────────────────────────────────────


def _cancel_target() -> str | None:
    """Token the pending cancel is addressed to, or None if there is none."""
    try:
        with open(CANCEL_FILE, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def request_cancel() -> bool:
    """
    Ask the run that currently holds the lock to stop.

    Deliberately does not touch the lock file: the run owns that, and releases
    it when it has actually wound down. Removing it here is what previously let
    the user start a second run while the first was still going.
    """
    info = read()
    if info is None:
        return False
    try:
        with open(CANCEL_FILE, "w") as f:
            f.write(info.token)
    except OSError:
        return False
    return True


def is_cancelled(token: str | None = None) -> bool:
    """Whether a cancel is pending for this run."""
    target = _cancel_target()
    if target is None:
        return False
    if not target or token is None:
        return True  # unaddressed signal (legacy, or a caller without a token)
    return target == token


def clear_cancel(token: str | None = None):
    """Remove the cancel signal, but only if it was addressed to this run."""
    if token is not None and not is_cancelled(token):
        return
    _remove(CANCEL_FILE)
