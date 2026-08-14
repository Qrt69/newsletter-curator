#!/bin/bash
# Weekly safety net for newsletter-curator. Runs on the VPS, not in a container.
#
# Every pipeline run leaves ~435MB of anonymous memory behind in the granian
# worker that ran it, so without intervention the container walks into its
# 6g mem_limit in under two weeks and gets OOM-killed mid-run. A weekly restart
# puts a hard ceiling on that, whatever the in-app fixes turn out to be worth.
#
# It skips the restart while a pipeline is running: a run takes ~1.5h, and
# killing one mid-flight loses the run (emails stay in "To qualify", but the
# scoring work is gone). Missing one week is cheaper than that.
#
# Install (or reinstall after rebuilding the machine) as the kurt user — no
# sudo, kurt is in the docker group:
#
#   install -D -m 755 scripts/nc-weekly-restart.sh /home/kurt/bin/nc-weekly-restart.sh
#   { crontab -l 2>/dev/null; echo '30 3 * * 0 /home/kurt/bin/nc-weekly-restart.sh'; } | crontab -
#
# The VPS clock is CEST and cron follows local time, so that is Sunday 03:30
# local. Decisions are logged to /home/kurt/nc-weekly-restart.log.
# Set DRY_RUN=1 to exercise the logic without actually restarting, and
# LOCK_FILE=<a path that exists in the container> to exercise the run guard.
set -u

LOG=/home/kurt/nc-weekly-restart.log
NAME=newsletter-curator-web
LOCK=${LOCK_FILE:-/data/.pipeline_running}  # overridable so the guard is testable

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }
mem() { docker stats --no-stream --format '{{.MemUsage}}' "$1" 2>/dev/null; }

CID=$(docker ps -q -f "name=$NAME" | head -1)
if [ -z "$CID" ]; then
    log "container niet actief - niets te doen"
    exit 0
fi

if docker exec "$CID" test -f "$LOCK" 2>/dev/null; then
    log "pipeline draait - herstart overgeslagen tot volgende week"
    exit 0
fi

BEFORE=$(mem "$CID")

if [ "${DRY_RUN:-0}" = "1" ]; then
    log "DRY_RUN: zou nu herstarten (geheugen $BEFORE)"
    exit 0
fi

if ! docker restart "$CID" >/dev/null 2>&1; then
    log "FOUT: docker restart mislukt (geheugen was $BEFORE)"
    exit 1
fi

# Give Reflex/granian time to come up before reading the fresh figure.
sleep 30
NEW=$(docker ps -q -f "name=$NAME" | head -1)
AFTER=$(mem "$NEW")
log "herstart ok: $BEFORE -> ${AFTER:-onbekend}"
