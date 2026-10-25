# Newsletter Curator - Project Memory

## Project Overview
Intelligent newsletter curation system that automates finding, scoring, and organizing relevant newsletter content into Kurt Farasyn's 14-database Notion vault.

**Owner:** Kurt Farasyn (ERP/BC consultant, 25 years experience, Python enthusiast)
**Core Purpose:** Process weekly newsletters from Outlook, filter items matching Kurt's interests via Claude AI scoring, review in web UI, then auto-populate Notion databases.

## Tech Stack
- **Backend:** Python 3.13+, Anthropic Claude API (claude-sonnet-4-5-20250929), Microsoft Graph API
- **Frontend:** Reflex (Python-based React framework)
- **Database:** SQLite (digest.db)
- **Email:** Azure/Microsoft Graph (M365 Outlook)
- **Notion:** notion-client SDK (14 databases)
- **Content Extraction:** trafilatura, BeautifulSoup, Playwright (Medium/Beehiiv fallback)
- **Deployment:** Docker + Caddy reverse proxy + Redis on Hetzner VPS
- **Package Manager:** uv

## Directory Structure
```
src/
  email/
    fetcher.py          # M365 email fetching (Graph API)
    extractor.py        # Link parsing, content extraction, trafilatura, parallel ThreadPoolExecutor
    browser.py          # Playwright browser for Medium/Beehiiv magic-link auth
  intelligence/
    scorer.py           # Claude-based item scoring (0-10, verdicts)
    router.py           # Route items to Notion databases + dedup check
    feedback.py         # Learn from user decisions, detect patterns, rule proposals
    prompts.py          # Scorer system prompt with interest profile
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
  run_weekly.py         # Pipeline orchestration + CLI (also holds the unused scheduler)
tests/                  # 12 test modules covering all components
```

## Pipeline Flow (6 Steps)
1. **Fetch** emails from Outlook "to qualify" folder (M365 Graph API)
2. **Extract** article links & content (trafilatura, parallel with 8 workers, Playwright fallback for Medium/Beehiiv)
3. **Score** items with Claude API (0-10 score, verdict, item_type, reasoning, tags) + feedback injection
4. **Route** to Notion databases + dedup check (fuzzy name match 80%, normalized URL match)
5. **Store** in SQLite digest.db, record run statistics
6. **Move** processed emails to "processed" folder

## Routing Table (item_type -> Notion database)
```
python_library    -> Python Libraries       ai_tool          -> TAAFT
duckdb_extension  -> DuckDB Extensions      agent_workflow   -> Overview
model_release     -> Model information      platform_infra   -> Platforms & Infrastructure
concept_pattern   -> Topics & Concepts      article          -> Articles & Reads
book_paper        -> Books & Papers         coding_tool      -> AI Agents & Coding Tools
vibe_coding_tool  -> Vibe Coding Tools      ai_architecture  -> AI Architecture Topics
infra_reference   -> Infrastructure Knowledge Base
```

## Web UI Features
- Run selector dropdown (auto-selects newest after pipeline)
- Items table with score badges, inline accept/reject
- Detail dialog with editable fields (name, category, database, tags)
- "Run Pipeline" button with polling (3s interval, 30min stale lock)
- "Write to Notion" button with live status
- Bulk dismiss, show-all toggle, rule proposal alerts
- API endpoints: /api/pipeline/trigger, /api/pipeline/status, /api/notion/write, /api/cleanup

## Key Configuration
- `databases.json` - Notion database ID mappings
- `.env` - Credentials (NOTION_API_KEY, MS_GRAPH_*, ANTHROPIC_API_KEY)
- `rxconfig.py` - Reflex config
- `BROWSER_POOL_SIZE` - concurrent Chromiums during extraction (default 4, see "Extraction Speed")
- `.browser_state.json` - Playwright cookies for Medium auth (7-day validity)
- `.dedup_cache.json` - Dedup index cache (7-day TTL)
- `.pipeline_running` - Lock file with PID

## Deployment (Docker)
- `Dockerfile`: python:3.13, caddy, redis, Playwright Chromium
- `docker-compose.yml`: `web` container only (port 8080), shared volume for digest.db
- `Caddyfile.docker`: Routes API/events to Reflex backend, static files from /srv
- `start.sh`: Starts redis, caddy, reflex backend, then a watchdog: if `/ping` (via Caddy), Redis, or a real websocket handshake on `/_event` (`scripts/ws_probe.py`, stdlib only) fails 3 checks in a row (60s apart) the script exits, so `restart: unless-stopped` restarts the container. Before this, a dead backend left the page loading with "websocket error" until a manual stop/start. Restarts are logged to `/data/watchdog.log`. The websocket check was added after 2026-10-21, when `/ping` kept answering 200 for days while the page showed "websocket error" and the container sat at 100% CPU. While a pipeline run's process is alive, a failing websocket is only logged, so a restart never kills a run half-way.

**There is no scheduler. Runs are started by hand from the UI.** The `scheduler` service
still exists in `docker-compose.yml` but sits behind `profiles: ["scheduler"]`, so
`docker compose up` never starts it, and its dead container was removed on 2026-08-27.
It was switched off deliberately: it ran every Sunday unnoticed and a successful run
moves the emails to "Processed" without anyone reviewing them. It also cannot work
unattended, because LM Studio is only switched on for a run. Do not re-enable it, and do
not "fix" its absence.

## Curator 2.0 (branch `v2`, folder `curator2/`)

Rebuild agreed with Kurt on 2026-10-25; design doc: "Curator 2.0 — ontwerp" (Claude Docs).
Runs on the laptop only (no VPS, no tunnel, no Notion, no web UI), every evening at 20:00
via a Windows scheduled task (`curator2/install.ps1`, installs to `C:\Dev\curator2`).
Reuses `src/email` (Graph fetcher, extractor, BrowserPool) unchanged.

- Flow (`pipeline.py`): LM Studio check -> upgrades ticked in Verworpen -> fetch mails ->
  extract (cached per email) -> real duplicates (`urls.normalize`, vs `state.seen` and the
  vault's `sleutel` properties) -> fixed rules (`config/regels.toml`) -> judge (cached per
  link) -> events out -> GitHub vital signs -> lanes: toppers + all repos kept, regular
  items ranked, best `percentage`% kept -> same-topic items get a novelty question ->
  write notes -> rejected list rendered from the DB -> only then move mails.
- The model never scores: it answers fixed questions (`judge.py`); points come from
  `config/beoordeling.toml`. Kurt edits the three TOML files, not code.
- OSK output under `<vault>/Curator/`: `Items/` (one note per kept item, properties for
  Bases), `Curator.base` (written once, never overwritten), `Verworpen/<date>.md` (tick
  boxes with `%%rid:..%%`; removed after 90 days), `Runs/<date>.md`.
- State: SQLite in `%LOCALAPPDATA%\curator2\curator2.sqlite`, log next to it. An
  unfinished run (status `bezig`) is resumed by the next start.
- Weekly repo review is done by Claude, not by the program: `curator2/repo_review.md`.
- Tests: `uv run python -m pytest -q curator2/tests` (fake mails, fake LM Studio, fake GitHub).

## Scorer Backend: Claude or LM Studio
`SCORER_BACKEND` in `.env` picks the scorer: `local` (LM Studio on the laptop, via the SSH
tunnel) or `anthropic` (Claude API, laptop not needed). `docker-compose.yml` reads it as
`${SCORER_BACKEND:-local}`. Switch with `sudo bash scripts/scorer_backend.sh claude|local`,
which edits `.env`, rebuilds `web` and tests the API. Claude model: `ANTHROPIC_MODEL`
(default `claude-haiku-5-5`). In `anthropic` mode a non-`claude*` model name from the UI's
LM Studio dropdown is ignored. Switched to Claude on 2026-10-25 after LM Studio fell to
~1 token/s (vs 11-23 normally) with unchanged settings.

## CLI Usage
```bash
uv run python scripts/run_weekly.py                # Run pipeline once
uv run python scripts/run_weekly.py --schedule     # Weekly scheduler -- deliberately unused, see Deployment
uv run python scripts/run_weekly.py --write <id>   # Write run to Notion
uv run python scripts/run_weekly.py --browser-login # Medium magic-link login
```

## Testing
```bash
uv run pytest tests/
```

## Extraction Speed: Read This Before "Fixing" It

**The fast extraction runs from before August 2026 were fast because they were doing
almost nothing. Do not treat the slowdown as a regression to revert.**

Almost every link in these newsletters is a beehiiv tracking URL
(`link.mail.beehiiv.com/ss/c/...`). Beehiiv answers plain HTTP clients with **403**
(measured: 6/6 links via httpx, from the VPS), so such a link can only be resolved by a
real browser. Until commit `5738863` (2026-08-13) the browser path silently failed:
`BrowserFetcher` launched Chromium inside a worker of the extractor's per-email
ThreadPoolExecutor, that pool was torn down after each email, and every later call died on
Playwright's thread affinity ("cannot switch to a different thread"). `resolve_url` then
fell through to httpx, got its 403, and stored the item with the raw tracking URL and no
text at all.

What that cost, straight out of `digest.db`:

| run | date | items | still on a tracking URL | empty text | extraction |
|-----|------|-------|-------------------------|------------|------------|
| 281 | 2026-08-11 | 335 | 264 (79%) | 290 (**87%**) | 16s |
| 283 | 2026-08-14 | 426 | 107 (25%) |  72 (17%)     | 8m54s |

So 87% of the items in a "fast" run reached the scorer empty — the LLM was scoring bare
URLs. The extraction time is now real work, not a bug.

**Why extraction is nonetheless parallel now.** The old extractor also held a global
`_browser_lock`, so browser calls were always one-at-a-time; the owner-thread fix did not
reduce concurrency, it just made the serialized work actually happen (16m07s for 30 emails,
container CPU ~2% — pure waiting). `BrowserPool` (`src/email/browser.py`) keeps K
`BrowserFetcher` instances, each with its own owner thread, leased out via a LIFO queue:

- Thread affinity is per instance, so each Chromium is still closed by the thread that
  launched it — that is what stops the orphan Chromium trees `5738863` was fixing.
- Measured on the VPS: **1.2s per link at size=1, 0.4s at size=3 (3.0x, linear)**.
- **~115MB RSS per launched instance**, fully released on `close()`. K=4 (`BROWSER_POOL_SIZE`)
  sits at ~460MB inside the container's 6GB `mem_limit`.
- Fetchers launch lazily and the queue is LIFO, so a run with few browser links keeps
  reusing one warm instance and never launches the other three.

Do not put a lock back around `self._browser` in the extractor: that would return every
link to a single Chromium and the 16 minutes with it.

**Every browser call has a deadline, and blowing it kills the fetcher.** Of the Playwright
calls in `_fetch_page` only `page.goto` has a timeout of its own; `new_context()`,
`page.content()` and `context.close()` have none. Run 289 (2026-08-21) parked on
`Extracting content (1/21 emails)` for 50 minutes because one Chromium lost the zygote it
forks renderers from: the call never returned, its owner thread never came back, and
`pool.map` in the extractor waited on it forever. `BrowserFetcher._run` now takes a hard
`timeout=`; on expiry it *wedges* the fetcher — SIGKILLs its browser tree, which is the
only thing that can unblock the parked owner thread — and `BrowserPool` swaps in a fresh,
unlaunched fetcher so the pool keeps its size. The killed pids are found via the node
driver's pid (`_driver_pid`), not by diffing `/proc` around the launch: concurrent launches
see each other's children, so the old diff had every fetcher claiming all four Chromiums.

## Conventions
- Type hints throughout (Python 3.13+)
- Graceful fallbacks (HTTP -> Browser for extraction)
- ThreadPoolExecutor for I/O parallelism
- SQLite WAL mode for concurrent access
- Feedback loop: user decisions injected into scorer prompt (max 10 examples)
- Rule proposals: pattern detection from feedback (min 4 same-type overrides)

## Recent Work (as of Feb 2025)
- Auto-select newest run after pipeline completes
- Fix pipeline lock expiry and Playwright close error
- Parallelize content extraction with ThreadPoolExecutor
- Auto-detect pipeline completion and fix total items count
- Bulk dismiss and auto-cleanup for rejected/skipped items
- Fix tracking URL resolution: browser first for blocked domains
- Notion write with live status updates
- Docker deployment setup for Hetzner VPS
- Fix overly aggressive dedup: drop token_set_ratio subset matching
- Score items on metadata only, skip sending article text to Claude
- Show live pipeline progress in web UI via progress file
- Tighten extractor link filters to reduce items per email from 60-70 to ~5-15 (was causing excessive API costs ~$50/run)

## Upcoming / Planned
- Evaluate local LLM for scoring task to eliminate per-call API costs (structured task: interest profile in, score/verdict/item_type out)
- Scorer prompt changes may be in progress (src/intelligence/prompts.py has uncommitted edits)
