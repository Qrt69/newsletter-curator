"""
Tests for the shared pipeline lock (src/storage/lock.py).

Covers the three bugs it replaces:
  - a healthy long run being declared stale by a clock-based rule
  - Force Stop deleting the lock, freeing the button while the run went on
  - a new run wiping the cancel signal meant for the run it stacked on

Run: uv run pytest tests/test_lock.py
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def lock(tmp_path, monkeypatch):
    """A lock module bound to a throwaway DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.storage import lock as lock_module
    return importlib.reload(lock_module)


def test_acquire_blocks_second_run(lock):
    token = lock.acquire()
    assert token
    assert lock.is_locked()
    assert lock.acquire() is None, "a second run must not get the lock"

    lock.release(token)
    assert not lock.is_locked()
    assert lock.acquire(), "lock is free again after release"


def test_long_run_is_not_stale(lock):
    """The old 30-minute mtime rule declared 50-113 minute runs dead."""
    token = lock.acquire()
    old = 4 * 60 * 60  # four hours ago
    os.utime(lock.LOCK_FILE, (old, old))

    assert lock.is_locked(), "a live process holds it, however old the file is"
    assert lock.acquire() is None
    lock.release(token)


def test_dead_owner_frees_the_lock(lock):
    """A crashed run must not block the next one forever."""
    dead_pid = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid.wait()

    with open(lock.LOCK_FILE, "w") as f:
        f.write(f'{{"pid": {dead_pid.pid}, "host": "{lock._host()}", "token": "abc"}}')

    assert not lock.is_locked()
    assert not os.path.exists(lock.LOCK_FILE), "stale lock is cleaned up"
    assert lock.acquire()


def test_force_stop_leaves_the_lock_alone(lock):
    """Force Stop signals only; the run releases its own lock."""
    token = lock.acquire()

    assert lock.request_cancel() is True
    assert lock.is_locked(), "the run is still winding down"
    assert lock.acquire() is None, "no second run may start yet"
    assert lock.is_cancelled(token)

    lock.clear_cancel(token)
    lock.release(token)
    assert not lock.is_locked()


def test_cancel_is_addressed_to_one_run(lock):
    """A leftover stop signal must not kill the next run."""
    first = lock.acquire()
    lock.request_cancel()
    assert lock.is_cancelled(first)

    # First run ends without clearing (crash, kill -9, container restart)
    lock.release(first)

    second = lock.acquire()
    assert second != first
    assert not lock.is_cancelled(second), "the old signal was not addressed to us"


def test_cancel_does_not_leak_to_a_stacked_run(lock):
    """The reverse case: a new run must not wipe a live run's stop signal."""
    first = lock.acquire()
    lock.request_cancel()

    # Simulate the old bug: a second run starting while the first still holds
    # the lock. It must not get in, and must not clear the signal.
    assert lock.acquire() is None
    assert lock.is_cancelled(first), "the first run's stop signal survives"


def test_legacy_lock_file_is_understood(lock):
    """Locks written by the previous version must not wedge the pipeline."""
    with open(lock.LOCK_FILE, "w") as f:
        f.write("12345-deadbeef")

    info = lock.read()
    assert info.pid == 12345
    assert info.token == "deadbeef"

    # Unknown host, so it falls back to the heartbeat: fresh means running.
    assert lock.is_locked()

    old = 60 * 60
    os.utime(lock.LOCK_FILE, (old, old))
    assert not lock.is_locked(), "no heartbeat for an hour means it is gone"


def test_heartbeat_keeps_a_foreign_lock_alive(lock):
    """A run in the scheduler container proves liveness by heartbeating."""
    with open(lock.LOCK_FILE, "w") as f:
        f.write('{"pid": 1, "host": "other-container", "token": "xyz"}')

    old = 60 * 60
    os.utime(lock.LOCK_FILE, (old, old))
    assert not lock.is_locked()

    with open(lock.LOCK_FILE, "w") as f:
        f.write('{"pid": 1, "host": "other-container", "token": "xyz"}')
    lock.heartbeat()
    assert lock.is_locked()
