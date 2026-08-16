# Newsletter Curator

Intelligent newsletter curation system that automates finding, scoring, and organizing relevant newsletter content into a 14-database Notion vault. Processes weekly newsletters from Outlook, filters items via LLM scoring against an interest profile, presents a review UI, then auto-populates Notion databases.

## Features

- **Email ingestion** — fetches newsletters from M365 Outlook via Microsoft Graph API
- **Content extraction** — parallel extraction with trafilatura, Playwright fallback for Medium/Beehiiv
- **LLM scoring** — dual backend (local LM Studio or Claude API), scores items 0-10 against interest profile
- **Listicle explosion** — detects listicle articles, extracts individual sub-items via separate LLM call
- **Dedup checking** — fuzzy name + normalized URL matching against existing Notion entries
- **Web review UI** — Reflex-based interface with accept/reject, inline editing, skip reason badges
- **Notion writer** — auto-creates entries in the correct database with proper field mapping
- **Feedback loop** — learns from accept/reject decisions, proposes scoring rule updates

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- Notion integration with access to 14 databases (mapped in `databases.json`)
- Azure AD app registration for Microsoft Graph API
- Anthropic API key (for Claude backend) or LM Studio (for local backend)

## Setup

1. Clone and install:
   ```bash
   git clone <repository-url>
   cd newsletter-curator
   uv sync
   ```

2. Create `.env` with credentials:
   ```
   NOTION_API_KEY=ntn_xxxx
   ANTHROPIC_API_KEY=sk-ant-xxxx
   MS_GRAPH_CLIENT_ID=xxxx
   MS_GRAPH_CLIENT_SECRET=xxxx
   MS_GRAPH_TENANT_ID=xxxx
   MS_GRAPH_USER_EMAIL=user@example.com
   OUTLOOK_FOLDER_NAME=to qualify
   OUTLOOK_PROCESSED_FOLDER=to qualify/processed
   ```

3. Optional local LLM config:
   ```
   SCORER_BACKEND=local          # default; or "anthropic"
   LLM_BASE_URL=http://localhost:1234/v1
   LLM_MODEL=                    # auto-detected if empty
   ```

## Usage

```bash
uv run python scripts/run_weekly.py                # Run pipeline once
uv run python scripts/run_weekly.py --schedule      # Start weekly scheduler
uv run python scripts/run_weekly.py --write <id>    # Write run to Notion
uv run python scripts/run_weekly.py --browser-login # Medium magic-link login
uv run reflex run                                   # Start web UI
uv run pytest tests/                                # Run tests
```

## Project Structure

```
src/
  email/
    fetcher.py          # M365 email fetching (Graph API)
    extractor.py        # Link parsing, content extraction, parallel ThreadPoolExecutor
    browser.py          # Playwright browser for Medium/Beehiiv magic-link auth
  intelligence/
    scorer.py           # Dual backend LLM scoring (local/anthropic), context overflow handling
    router.py           # Route items to Notion databases + dedup check
    feedback.py         # Learn from user decisions, detect patterns, rule proposals
    prompts.py          # Scorer system prompt with interest profile
    exploder.py         # Listicle detection + sub-item extraction via LLM
  notion/
    client.py           # Notion API wrapper (14 databases)
    writer.py           # Write accepted items to Notion (per-DB property mappers)
    dedup.py            # In-memory dedup index (fuzzy name + normalized URL matching)
  storage/
    digest.py           # SQLite store for runs, items, feedback
  web/
    app.py              # Reflex UI components & Starlette API endpoints
    state.py            # Reflex state management (DigestState)
scripts/
  run_weekly.py         # Pipeline orchestration, scheduler (Sunday 18:00 UTC), CLI
tests/                  # Test suite covering all components
```

## Deployment

Docker Compose on Hetzner VPS with Caddy reverse proxy:

```bash
docker compose up -d --build
```

### Memory: every run gets its own process

A pipeline run leaves ~435MB of anonymous memory behind. While the run was a
thread inside the web process that was ~435MB the granian worker never gave
back, reaching `mem_limit: 6g` in under two weeks at a run a day. A
`gc.collect()` plus `malloc_trim(0)` at the end of a run recovered 40MB of it,
and capping glibc's arenas cost more in speed than it gave back in memory.

So the cause was never settled, and no longer has to be: a run happens in a
child process now (`src/web/runner.py`), which hands its whole address space
back to the kernel when it exits. Both the "Run Pipeline" button and
`GET /api/pipeline/trigger` go through it, so every run starts and ends clean.
The lock, cancel signal, progress display and log were already files under
`DATA_DIR`, so they cross the process boundary unchanged.

`scripts/nc-start-run.sh` additionally recycles the whole web container before
triggering a run -- belt-and-braces now rather than the fix itself. Its restart
is at the *start* of a run, never after: the review UI has to stay up
afterwards, because that is where proposals are judged before they go to Notion.
It lives on the host, so a rebuilt machine needs it installed again:

```bash
install -D -m 755 scripts/nc-start-run.sh /home/kurt/bin/nc-start-run.sh
/home/kurt/bin/nc-start-run.sh                      # model=auto
/home/kurt/bin/nc-start-run.sh qwen2.5-14b-instruct
```

No sudo: the `kurt` user is in the `docker` group. It refuses to restart while
a run is in progress, so it can never cost a run in flight. Deliberately not on
cron: a run needs the PC on with LM Studio reachable, and it moves emails to
"Processed" when it succeeds. Decisions land in `/home/kurt/nc-start-run.log`.

## Routing Table

| Item Type | Notion Database |
|-----------|----------------|
| python_library | Python Libraries |
| duckdb_extension | DuckDB Extensions |
| ai_tool | TAAFT |
| agent_workflow | Overview |
| model_release | Model information |
| platform_infra | Platforms & Infrastructure |
| concept_pattern | Topics & Concepts |
| article | Articles & Reads |
| book_paper | Books & Papers |
| coding_tool | AI Agents & Coding Tools |
| vibe_coding_tool | Vibe Coding Tools |
| ai_architecture | AI Architecture Topics |
| infra_reference | Infrastructure Knowledge Base |