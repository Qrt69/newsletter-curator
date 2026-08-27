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


# -- Main ------------------------------------------------------

def main():
    test_returns_immediately_when_lm_studio_is_up()
    test_waits_until_lm_studio_appears()
    test_idle_does_not_start_a_tunnel()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
