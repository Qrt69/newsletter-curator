#!/bin/bash
# Start a pipeline run in a freshly restarted container. Runs on the VPS, not
# inside a container.
#
# A pipeline run leaves ~435MB of anonymous memory behind, so a container that
# accumulated it walked into its 6g mem_limit and got OOM-killed mid-run. The
# actual fix is in the app: a run gets its own process (src/web/runner.py), so
# it hands that memory back to the kernel when it exits, whether the cause was
# a leak or the allocator holding on. The "Run Pipeline" button is safe.
#
# This script recycles the whole container on top of that, for when you want a
# run to start from a genuinely fresh app -- belt-and-braces, not the fix.
#
# The restart happens at the *start* of a run, not after it: the review UI has
# to stay up afterwards, because the proposals are judged there and only then
# written to Notion. During the first hour of a run the UI is not needed.
#
# Not on cron on purpose: a run only works when the PC is on with LM Studio
# reachable through the tunnel, and a successful run moves emails to
# "Processed", so it should not fire unattended. Same reason the scheduler
# profile in docker-compose.yml is off by default.
#
# Install (or reinstall after rebuilding the machine) as the kurt user -- no
# sudo, kurt is in the docker group:
#
#   install -D -m 755 scripts/nc-start-run.sh /home/kurt/bin/nc-start-run.sh
#
# Then start a run with:
#
#   /home/kurt/bin/nc-start-run.sh            # model=auto
#   /home/kurt/bin/nc-start-run.sh qwen2.5-14b-instruct
#
# Decisions are logged to /home/kurt/nc-start-run.log. Set DRY_RUN=1 to
# exercise the logic without restarting or triggering, and LOCK_FILE=<a path
# that exists in the container> to exercise the run guard.
set -u

LOG=/home/kurt/nc-start-run.log
NAME=newsletter-curator-web
LOCK=${LOCK_FILE:-/data/.pipeline_running}  # overridable so the guard is testable
API=http://127.0.0.1:8080
MODEL=${1:-auto}
READY_TIMEOUT=180  # granian + Reflex need well over 30s to answer again

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }
mem() { docker stats --no-stream --format '{{.MemUsage}}' "$1" 2>/dev/null; }

CID=$(docker ps -q -f "name=$NAME" | head -1)
if [ -z "$CID" ]; then
    log "container niet actief - geen run gestart"
    exit 1
fi

# Never restart on top of a run in flight: that loses ~1.5h of scoring work.
if docker exec "$CID" test -f "$LOCK" 2>/dev/null; then
    log "pipeline draait al - geen herstart, geen nieuwe run"
    exit 1
fi

BEFORE=$(mem "$CID")

if [ "${DRY_RUN:-0}" = "1" ]; then
    log "DRY_RUN: zou herstarten (geheugen $BEFORE) en dan run starten (model=$MODEL)"
    exit 0
fi

if ! docker restart "$CID" >/dev/null 2>&1; then
    log "FOUT: docker restart mislukt (geheugen was $BEFORE) - geen run gestart"
    exit 1
fi

CID=$(docker ps -q -f "name=$NAME" | head -1)
if [ -z "$CID" ]; then
    log "FOUT: container kwam niet terug na herstart - geen run gestart"
    exit 1
fi

# Wait for the app to actually answer before triggering: a POST into a
# half-started Reflex silently does nothing.
WAITED=0
until curl -fsS --max-time 5 "$API/api/pipeline/status" >/dev/null 2>&1; do
    if [ "$WAITED" -ge "$READY_TIMEOUT" ]; then
        log "FOUT: app antwoordt niet binnen ${READY_TIMEOUT}s na herstart - geen run gestart"
        exit 1
    fi
    sleep 5
    WAITED=$((WAITED + 5))
done

AFTER=$(mem "$CID")
log "herstart ok na ${WAITED}s: $BEFORE -> ${AFTER:-onbekend}"

RESP=$(curl -fsS --max-time 15 "$API/api/pipeline/trigger?model=$MODEL" 2>&1)
case "$RESP" in
    *started*)         log "run gestart (model=$MODEL)" ;;
    *already_running*) log "FOUT: app meldt already_running vlak na een herstart - lockfile blijven staan?" ; exit 1 ;;
    *)                 log "FOUT: onverwacht antwoord van trigger: $RESP" ; exit 1 ;;
esac
