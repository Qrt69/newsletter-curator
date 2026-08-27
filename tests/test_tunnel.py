"""
Tests for the tunnel supervisor's idle behaviour.

LM Studio is only switched on for a run, so the supervisor must sit still
while it is off instead of reconnecting into a dead end. These tests drive
wait_for_local_llm() with a fake port check -- no sockets, no ssh, no waiting.

Run: uv run python tests/test_tunnel.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from scripts import start_tunnel


# -- Helpers ---------------------------------------------------

def _fake_port(script):
    """
    Replace the local port check with a scripted sequence of results.

    Returns a dict with a "calls" counter so tests can assert how often the
    port was polled.
    """
    state = {"calls": 0}

    def check():
        result = script[min(state["calls"], len(script) - 1)]
        state["calls"] += 1
        return result

    start_tunnel.is_local_port_open = check
    return state


def _no_sleep():
    start_tunnel.time.sleep = lambda seconds: None


# -- Tests -----------------------------------------------------

def test_returns_immediately_when_lm_studio_is_up():
    """Already listening: no idling, and a falsy value so the backoff stands."""
    print("TEST: no idle when LM Studio is already up")
    original_check, original_sleep = start_tunnel.is_local_port_open, start_tunnel.time.sleep
    try:
        state = _fake_port([True])
        _no_sleep()
        idled = start_tunnel.wait_for_local_llm()
        assert idled == 0.0, idled
        assert not idled, "0.0 must be falsy: an unrelated failure keeps backing off"
        assert state["calls"] == 1, state["calls"]
    finally:
        start_tunnel.is_local_port_open, start_tunnel.time.sleep = original_check, original_sleep
    print("PASS\n")


def test_waits_until_lm_studio_appears():
    """Closed port: keep polling, then report a non-zero idle time."""
    print("TEST: idles until LM Studio starts listening")
    original_check, original_sleep = start_tunnel.is_local_port_open, start_tunnel.time.sleep
    original_interval = start_tunnel.IDLE_POLL_INTERVAL
    try:
        start_tunnel.IDLE_POLL_INTERVAL = 0.5
        state = _fake_port([False, False, False, True])
        _no_sleep()
        idled = start_tunnel.wait_for_local_llm()
        # Three closed checks -> three intervals waited.
        assert idled == 1.5, idled
        assert idled, "a non-zero idle marks a fresh start so the backoff resets"
        assert state["calls"] == 4, state["calls"]
    finally:
        start_tunnel.is_local_port_open, start_tunnel.time.sleep = original_check, original_sleep
        start_tunnel.IDLE_POLL_INTERVAL = original_interval
    print("PASS\n")


def test_idle_does_not_start_a_tunnel():
    """While idling, no ssh process may be spawned -- that was the whole point."""
    print("TEST: idling never spawns ssh")
    original_check, original_sleep = start_tunnel.is_local_port_open, start_tunnel.time.sleep
    original_start = start_tunnel._start_tunnel_process
    started = []
    try:
        start_tunnel._start_tunnel_process = lambda: started.append(1)
        _fake_port([False, False, True])
        _no_sleep()
        start_tunnel.wait_for_local_llm()
        assert started == [], "the supervisor connected while LM Studio was off"
    finally:
        start_tunnel.is_local_port_open, start_tunnel.time.sleep = original_check, original_sleep
        start_tunnel._start_tunnel_process = original_start
    print("PASS\n")


class _StopLoop(Exception):
    """Breaks supervise() out of its endless loop inside a test."""


class _DeadProcess:
    """A tunnel process that has already exited by the time it is polled."""

    returncode = 1

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return ("", None)

    def terminate(self):
        pass


def _run_one_supervise_cycle(port_states):
    """
    Run supervise() until it asks for the local port one time too many.

    port_states drives is_local_port_open() call by call; when it runs out the
    loop is broken so the test does not hang. Returns the log messages.
    """
    messages = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    states = iter(port_states)

    def fake_port():
        try:
            return next(states)
        except StopIteration:
            raise _StopLoop

    originals = (
        start_tunnel.is_local_port_open,
        start_tunnel._start_tunnel_process,
        start_tunnel.kill_stale_local_tunnels,
        start_tunnel.time.sleep,
    )
    handler = _Capture()
    start_tunnel.logger.addHandler(handler)
    # Without an explicit level the logger inherits root's WARNING and the
    # INFO records under test never reach the handler.
    previous_level = start_tunnel.logger.level
    start_tunnel.logger.setLevel(logging.INFO)
    try:
        start_tunnel.is_local_port_open = fake_port
        start_tunnel._start_tunnel_process = lambda: _DeadProcess()
        start_tunnel.kill_stale_local_tunnels = lambda: None
        start_tunnel.time.sleep = lambda seconds: None
        try:
            start_tunnel.supervise()
        except _StopLoop:
            pass
    finally:
        start_tunnel.logger.setLevel(previous_level)
        start_tunnel.logger.removeHandler(handler)
        (start_tunnel.is_local_port_open,
         start_tunnel._start_tunnel_process,
         start_tunnel.kill_stale_local_tunnels,
         start_tunnel.time.sleep) = originals
    return messages


def test_no_reconnect_promise_when_lm_studio_is_gone():
    """
    The log must not announce a reconnect that never happens.

    A tunnel that dies because LM Studio was switched off is followed by
    idling, not by a reconnect -- so "Reconnecting in Ns..." would be a lie.
    Chasing exactly that kind of line has cost real debugging time.
    """
    print("TEST: no reconnect is promised when LM Studio is gone")
    # up (start the tunnel) -> it exits -> down (so the loop should idle)
    messages = _run_one_supervise_cycle([True, False])
    joined = "\n".join(messages)
    assert not any("Reconnecting" in m for m in messages), joined
    assert any("Tunnel exited" in m for m in messages), joined
    print("PASS\n")


def test_reconnect_is_announced_when_lm_studio_is_still_up():
    """With LM Studio still listening the reconnect is real, so say so."""
    print("TEST: reconnect is announced when LM Studio is still up")
    # up (start) -> it exits -> still up (so it really does reconnect)
    messages = _run_one_supervise_cycle([True, True])
    assert any("Reconnecting" in m for m in messages), "\n".join(messages)
    print("PASS\n")


# -- Main ------------------------------------------------------

def main():
    test_returns_immediately_when_lm_studio_is_up()
    test_waits_until_lm_studio_appears()
    test_idle_does_not_start_a_tunnel()
    test_no_reconnect_promise_when_lm_studio_is_gone()
    test_reconnect_is_announced_when_lm_studio_is_still_up()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
