FROM python:3.13

ARG PORT=8080
ENV PORT=$PORT \
    REFLEX_API_URL=http://localhost:$PORT \
    REFLEX_REDIS_URL=redis://localhost \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# System dependencies: Caddy, Redis, Playwright browser deps
RUN apt-get update -y && \
    apt-get install -y caddy redis-server && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files first (cache layer)
COPY pyproject.toml uv.lock ./

# Install Python dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

# Reflex needs the app_name directory to exist
RUN mkdir -p newsletter_curator

# Install Playwright Chromium + system deps
RUN uv run playwright install --with-deps chromium

# Build frontend: init Reflex, export static files, move to /srv
RUN uv run reflex init && \
    uv run reflex export --frontend-only --no-zip && \
    mv .web/build/client/* /srv/ && \
    rm -rf .web

# Create data directory
RUN mkdir -p /data

STOPSIGNAL SIGKILL

EXPOSE $PORT

CMD ["bash", "start.sh"]
