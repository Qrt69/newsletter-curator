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

# Start Reflex backend (foreground)
exec uv run reflex run --env prod --backend-only --loglevel info
