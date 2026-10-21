#!/bin/bash
set -e

# Ensure data directory exists
mkdir -p /data

# Run alembic migrations if present
[ -d alembic ] && uv run reflex db migrate

# Start Redis in the background
redis-server --daemonize yes --loglevel warning

# Start Caddy in the background
caddy start --config /app/Caddyfile.docker

set +e

# Start Reflex backend in the background so the watchdog below can supervise it.
uv run reflex run --env prod --backend-only --loglevel info &
BACKEND_PID=$!

# -----------------------------------------------------------------------------
# Watchdog
#
# Caddy serves the frontend from /srv on its own, so when the Reflex backend (or
# the Redis it keeps state in) dies or hangs, the page still loads but shows
# "Cannot connect to server: websocket error". The container keeps "running",
# so Docker's restart policy never kicks in and only a manual stop/start helped.
#
# This loop probes the backend and Redis every CHECK_INTERVAL seconds. After
# MAX_FAILURES consecutive failures it exits non-zero; the container stops and
# `restart: unless-stopped` brings it back -- the same stop/start, automatic.
# Each restart is logged to /data/watchdog.log with the reason.
# -----------------------------------------------------------------------------
CHECK_INTERVAL=60
MAX_FAILURES=3
STARTUP_GRACE=300   # backend may take a while to come up after a rebuild
LOG=/data/watchdog.log

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# Probe through the in-container Caddy: the same path the browser's websocket
# takes, so a dead Caddy is caught as well as a dead backend.
backend_ok() { curl -fsS -m 10 -o /dev/null "http://localhost:${PORT:-8080}/ping"; }
redis_ok()   { [ "$(redis-cli -t 5 ping 2>/dev/null)" = "PONG" ]; }
# /ping can answer while the browser connection is dead (seen 2026-10-21: ping
# 200, page "websocket error", container at 100% CPU for days). So also open the
# websocket the browser uses and check the app answers on it.
ws_ok()      { python3 -I /app/scripts/ws_probe.py "${PORT:-8080}" 10; }
# A pipeline run lives inside the backend, so restarting kills it half-way.
# While a run's process is alive, a dead websocket is logged but not acted on;
# the run finishes first and the next checks restart the container if needed.
run_active() {
    local pid
    pid=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid") or "")' \
          /data/.pipeline_running 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# Wait for the backend to answer once before judging it.
waited=0
until backend_ok && ws_ok >/dev/null 2>&1; do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        log "Backend process exited during startup -- restarting container"
        exit 1
    fi
    if [ "$waited" -ge "$STARTUP_GRACE" ]; then
        log "Backend did not answer within ${STARTUP_GRACE}s of startup -- restarting container"
        exit 1
    fi
    sleep 5
    waited=$((waited + 5))
done
log "Backend up after ${waited}s"

failures=0
while true; do
    sleep "$CHECK_INTERVAL"

    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        log "Backend process is gone -- restarting container"
        exit 1
    fi

    reason=""
    backend_ok || reason="backend /ping not answering"
    redis_ok   || reason="${reason:+$reason; }redis not answering"
    if ! ws_err=$(ws_ok 2>&1); then
        if run_active; then
            log "Websocket not answering during a pipeline run, waiting for the run to end: $ws_err"
        else
            reason="${reason:+$reason; }websocket not answering ($ws_err)"
        fi
    fi

    if [ -z "$reason" ]; then
        failures=0
        continue
    fi

    failures=$((failures + 1))
    log "Check failed ($failures/$MAX_FAILURES): $reason"
    if [ "$failures" -ge "$MAX_FAILURES" ]; then
        log "Giving up after $MAX_FAILURES failed checks -- restarting container"
        kill "$BACKEND_PID" 2>/dev/null
        exit 1
    fi
done
