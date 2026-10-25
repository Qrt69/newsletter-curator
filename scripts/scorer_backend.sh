#!/usr/bin/env bash
# Switch the curator's scorer between Claude (Anthropic API) and LM Studio.
#
#   sudo bash scripts/scorer_backend.sh claude   # score with Claude, laptop not needed
#   sudo bash scripts/scorer_backend.sh local    # back to LM Studio on the laptop
#
# The choice lives in .env (SCORER_BACKEND), which docker-compose.yml reads.
# The model for Claude can be overridden with ANTHROPIC_MODEL in .env
# (default: claude-haiku-5-5).
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=.env
MODE="${1:-}"

case "$MODE" in
  claude) BACKEND=anthropic ;;
  local)  BACKEND=local ;;
  *) echo "Gebruik: sudo bash scripts/scorer_backend.sh claude|local"; exit 1 ;;
esac

# Never restart in the middle of a run.
if curl -fsS --max-time 5 http://127.0.0.1:8080/api/pipeline/status 2>/dev/null | grep -q '"running": *true'; then
  echo "Er loopt nog een run. Wacht tot die klaar is (of klik Force Stop) en probeer opnieuw."
  exit 1
fi

set_var() {  # set_var NAME VALUE  -- replace the line in .env, or add it
  local name="$1" value="$2"
  if grep -q "^${name}=" "$ENV_FILE"; then
    local tmp; tmp="$(mktemp)"
    grep -v "^${name}=" "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$name" "$value" >> "$tmp"
    cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
  else
    [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && echo >> "$ENV_FILE"
    printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
  fi
}

if [ "$BACKEND" = anthropic ]; then
  if ! grep -Eq '^ANTHROPIC_API_KEY=.+' "$ENV_FILE"; then
    echo "Er staat nog geen Anthropic API-sleutel in de instellingen."
    read -rsp "Plak je sleutel (begint met sk-ant-) en druk op Enter (je ziet niets verschijnen, dat is normaal): " KEY
    echo
    KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
    case "$KEY" in
      sk-ant-*) set_var ANTHROPIC_API_KEY "$KEY" ;;
      *) echo "Dat lijkt geen Anthropic-sleutel (moet met sk-ant- beginnen). Niets veranderd."; exit 1 ;;
    esac
  fi
fi

set_var SCORER_BACKEND "$BACKEND"

echo "Curator wordt opnieuw opgebouwd en herstart (duurt 2 a 5 minuten)..."
docker compose up -d --build web >/dev/null 2>&1 || { echo "Opbouwen mislukt:"; docker compose up -d --build web; exit 1; }

echo -n "Wachten tot de curator antwoordt"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:8080/ping >/dev/null 2>&1; then break; fi
  echo -n "."; sleep 5
done
echo

ACTIVE="$(docker compose exec -T web printenv SCORER_BACKEND || true)"
echo "Scorer staat nu op: ${ACTIVE}"

if [ "$BACKEND" = anthropic ]; then
  echo "Test: dezelfde testvraag die de curator zelf stelt..."
  # Goes through the curator's own Scorer, so the test sends exactly what a run
  # sends. (A bare API call passed on 2026-10-25 while every run failed on a
  # parameter only the Scorer added.)
  if docker compose exec -T web uv run python -c '
import sys; sys.path.insert(0, ".")
from src.intelligence.scorer import Scorer
s = Scorer()
s.preflight_check()
print("Claude (" + s._model + ") antwoordt: OK")
'; then
    echo "KLAAR. Start een run in de curator; je laptop hoeft niet aan te staan."
  else
    echo "MISLUKT: Claude antwoordt niet. Kijk de foutmelding hierboven na (sleutel of tegoed)."
    exit 1
  fi
else
  echo "KLAAR. LM Studio op de laptop is weer de scorer."
fi
