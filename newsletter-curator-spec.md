# Newsletter Curator - Project Specification

## Project Overview

A system that processes AI-related newsletters, scores items against Kurt's interests, checks for duplicates in his Notion vault, and presents a weekly digest via a web interface. Kurt reviews and approves items, which then get saved directly to Notion.

**Owner:** Kurt Farasyn
**Approach:** Step-by-step development, learning-focused. Kurt wants to understand each module before moving to the next. Explain clearly, build incrementally, test together.

---

## Kurt's Profile (for the LLM scoring prompt)

### Interests (score +3 each)
- AI agents & workflows
- Python libraries
- DuckDB ecosystem
- RAG / knowledge graphs
- Local LLMs / inference
- Machine learning
- Deep learning
- Graph theory
- Coding tools / vibe coding
- AI productivity tools (NotebookLM, Canva AI, Notion AI, Flourish, Gamma)
- PostgreSQL (learned from feedback)

### Context
- ERP/BC consultant with 25 years experience
- Technical but not a professional developer
- Builds Python projects with AI assistance (Claude Code)
- Wants practical, usable tools and knowledge

### Rejection criteria (score -3 each)
- Domain-specific tools for other industries (real estate, HR, legal, etc.)
- Pure consumer/entertainment AI
- Marketing fluff without real artifacts (repo/docs/demo)
- Enterprise dev tooling requiring large teams
- Content that's too basic ("What is AI?")
- Frontend frameworks (React, Vue, Angular)

---

## Architecture

```
┌─────────────────┐
│ M365 Mailbox    │ ← Newsletters arrive here
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Email Fetcher   │ ← Microsoft Graph API
│ (Python)        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Content         │ ← Extract URLs, text
│ Extractor       │ ← Playwright for Medium (logged in)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Scorer &        │ ← LLM evaluates each item
│ Router          │ ← Checks Notion for duplicates
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Digest DB       │ ← SQLite: pending items + feedback history
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Web Interface   │ ← Reflex app on VPS
│ (Reflex)        │ ← You review & approve
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Notion Writer   │ ← Saves approved items
│                 │ ← Creates relations
└─────────────────┘
```

---

## Notion Databases (with IDs)

| Database | ID | Purpose |
|----------|-----|---------|
| Articles & Reads | `2cc1d067-a128-80e8-bdb1-d81fff250a54` | Articles worth keeping |
| Infrastructure Knowledge Base | `2c81d067-a128-80fa-8762-de1c655c431f` | Ops/sysadmin reference |
| Notes & Insights | `2c21d067-a128-8085-92a6-da1115fdc2f2` | Personal observations |
| Topics & Concepts | `2bc1d067-a128-8062-a981-c105b6dee624` | Patterns, concepts |
| Books & Papers | `2bc1d067-a128-80ac-a9e4-c2c1943657cf` | Long-form reading |
| Platforms & Infrastructure | `94c3611a-2f3c-41ac-b4df-248013160107` | Infrastructure tools |
| AI Agents & Coding Tools | `5dec10bd-ae78-44a7-81be-4b9b1bd85da4` | Coding-specific tools |
| Model information | `2c81d067-a128-80eb-9bef-f3492a68c4c2` | Models & Benchmarks |
| Vibe Coding Tools | `b63aa8e4-9c70-4688-b30b-3c5817777f4c` | Vibe coding tools |
| AI Architecture Topics | `3a87061e-957d-4bf9-9133-f49932edbbdb` | Architecture signals |
| Overview (Agents & Workflows) | `2c81d067-a128-80d0-aa60-caaab0e81ea5` | Agent tools + concepts |
| TAAFT | `2c81d067-a128-809b-9031-d607131ea7c0` | Raw tool intake |
| Python Libraries | `2c61d067-a128-80e0-8841-dbe01e199e03` | Python ecosystem (344 entries) |
| DuckDB Extensions | `2ce1d067-a128-8091-95a8-e1a82bbd872f` | DuckDB extensions |

---

## Routing Logic

| Item type | Route to | Key fields |
|-----------|----------|------------|
| Python library | Python Libraries | Name, Pillar, Category, Primary Use, Learning Priority |
| DuckDB extension | DuckDB Extensions | Extension Name, Category, Learning Priority |
| AI tool / SaaS | TAAFT | Name, Category, Type, Stack Layer, Verdict, Usefulness |
| Agent or workflow tool | Agents & Workflows (Overview) | Name, Type, Core Idea |
| Model release | Model information | Name, Category, Type, Why It Matters |
| Platform / infrastructure | Platforms & Infrastructure | Platform Name, Category, Priority |
| Concept / pattern | Topics & Concepts | Name, Type, Category, Description |
| Article | Articles & Reads | Name, Source, URL, Tags, Summary, Why it matters |
| Book or paper | Books & Papers | Name, Type, Author, Difficulty |

### Pillar mapping (for Python Libraries)
- Pillar 1: Core Python (utilities, CLI, testing)
- Pillar 2: Data science (pandas, polars, visualization)
- Pillar 3: AI/ML/NLP (pytorch, transformers, LLM tools)
- Pillar 4: UI/Apps (streamlit, reflex, nicegui)
- Pillar 5: Infrastructure (airflow, dagster, orchestration)

---

## Scoring System

### Positive signals
| Factor | Points |
|--------|--------|
| Matches core interest domain | +3 |
| Has real artifact (repo/docs/demo) | +2 |
| Similar to previously accepted items | +2 |
| New version of something in vault | +2 |
| From trusted source | +1 |
| Practical/actionable | +1 |

### Negative signals
| Factor | Points |
|--------|--------|
| Out of scope domain | -3 |
| Duplicate, no new info | -3 |
| No artifact, just landing page | -2 |
| Similar to previously rejected items | -2 |
| Marketing heavy, substance light | -2 |
| Listicle with no depth | -1 |

### Thresholds
| Score | Verdict | Action |
|-------|---------|--------|
| 5+ | ⭐⭐ Strong fit | Auto-propose |
| 3-4 | ⭐ Likely fit | Propose with review |
| 1-2 | 🟡 Maybe | Show in maybe list |
| 0 or below | ❌ Reject | Skip (log reason) |

---

## Duplicate Handling

### Three levels
1. **Within newsletter batch** — dedupe by normalized URL
2. **Across newsletter sources** — same item from multiple newsletters
3. **Against Notion vault** — check all 14 databases

### When duplicate found
- **Exact match, no new info** → Flag as "Already in vault" (skip)
- **Match with new info** → Flag as "Update candidate" (propose update)

### What counts as "new info"
- Version bump (v1.2 → v2.0)
- New capability announced
- Major update (new API, features)

---

## Relation Handling

### Auto-link
When related entries exist in Notion, automatically link them.

### Propose creation
When a secondary entry makes sense (e.g., tool is also a Python library), propose:
> "Cognee is also a Python package. Create entry in Python Libraries? [Yes/No]"

Don't auto-create — always ask.

---

## Feedback & Learning

### What gets stored
Every accept/reject decision with:
- Item details
- Curator's verdict
- Your decision
- Timestamp

### How system learns

**Option C (immediate):** Recent overrides included in LLM prompt as examples.

**Option A (accumulating):** When pattern detected (e.g., 4+ Postgres accepts), propose rule update:
> "Add PostgreSQL to interests? [Yes/No]"

---

## Development Phases

### Phase 1: Foundation
**Goal:** Basic infrastructure, prove concepts work

1. **Module: Notion client wrapper**
   - Query databases
   - Create entries
   - Update entries
   - Create relations
   - Test with one database (e.g., Books & Papers)

2. **Module: Dedup index**
   - Load entries from all databases
   - Build searchable index (names, URLs)
   - Fuzzy matching function
   - Test: "Does Marimo exist?" → Yes, in Python Libraries

### Phase 2: Email & Content
**Goal:** Fetch and process newsletters

3. **Module: M365 email fetcher**
   - Microsoft Graph API authentication
   - Fetch emails from newsletter folder
   - Extract URLs and text
   - Store raw items in SQLite

4. **Module: Content extractor**
   - Fetch URL content
   - Playwright setup for Medium (with login)
   - Extract article text
   - Handle different sources (GitHub, docs, blogs)

### Phase 3: Intelligence
**Goal:** Score and route items

5. **Module: Scorer**
   - Build the scoring prompt
   - Call LLM (Claude API)
   - Parse response
   - Calculate final score

6. **Module: Router**
   - Determine item type
   - Select target database
   - Find related entries
   - Propose relations

### Phase 4: Interface
**Goal:** Web UI for review

7. **Module: Digest database**
   - SQLite schema for pending items
   - Feedback history table
   - Pattern detection queries

8. **Module: Reflex web app**
   - Digest list view
   - Item detail view (clickable)
   - Accept/reject buttons
   - Edit fields before saving
   - View original article link

### Phase 5: Integration
**Goal:** End-to-end flow

9. **Module: Notion writer**
   - Save approved items
   - Create relations
   - Handle update vs. create

10. **Module: Scheduler**
    - Weekly run trigger
    - n8n integration (optional)
    - Or Python scheduler (APScheduler)

### Phase 6: Learning
**Goal:** System improves over time

11. **Module: Feedback processor**
    - Track overrides
    - Detect patterns
    - Propose rule updates
    - Update prompt examples

---

## Environment Variables (.env)

```
NOTION_API_KEY=ntn_xxxx
ANTHROPIC_API_KEY=sk-ant-xxxx
MS_GRAPH_CLIENT_ID=xxxx
MS_GRAPH_CLIENT_SECRET=xxxx
MS_GRAPH_TENANT_ID=xxxx
MS_GRAPH_USER_EMAIL=kurt.farasyn@higeja.tech
OUTLOOK_FOLDER_NAME=to qualify
OUTLOOK_PROCESSED_FOLDER=to qualify/processed
MEDIUM_EMAIL=your@email.com
MEDIUM_PASSWORD=xxxx
```

**Note:** Create both folders in Outlook:
- `to qualify` — newsletters land here (via rules or drag-drop)
- `to qualify/processed` — curator moves emails here after processing

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.13+ |
| Package manager | uv |
| Notion API | notion-client |
| Email | Microsoft Graph API (msgraph-sdk) |
| Content extraction | trafilatura, BeautifulSoup, Playwright (Medium/Beehiiv fallback) |
| LLM (scoring) | Dual backend: local (LM Studio/OpenAI-compatible) or Anthropic Claude API |
| Database | SQLite (WAL mode) |
| Web framework | Reflex |
| Deployment | Docker + Caddy reverse proxy + Redis on Hetzner VPS |

---

## Project Structure

```
newsletter-curator/
├── .env                     # Secrets (not in git)
├── .env.example             # Template (in git)
├── pyproject.toml           # uv project config
├── README.md
│
├── src/
│   ├── __init__.py
│   │
│   ├── notion/
│   │   ├── client.py        # Notion API wrapper (14 databases)
│   │   ├── writer.py        # Write accepted items to Notion (per-DB property mappers)
│   │   └── dedup.py         # In-memory dedup index (fuzzy name + URL matching)
│   │
│   ├── email/
│   │   ├── fetcher.py       # M365 Graph API email fetching
│   │   ├── extractor.py     # Link parsing, content extraction, parallel ThreadPoolExecutor
│   │   └── browser.py       # Playwright for Medium/Beehiiv magic-link auth
│   │
│   ├── intelligence/
│   │   ├── scorer.py        # Dual backend LLM scoring (local/anthropic), context overflow handling
│   │   ├── router.py        # Route items to Notion databases + dedup check
│   │   ├── prompts.py       # Scorer system prompt with interest profile
│   │   ├── exploder.py      # Listicle detection + sub-item extraction via LLM
│   │   └── feedback.py      # Learning loop, rule proposals from user decisions
│   │
│   ├── storage/
│   │   └── digest.py        # SQLite store for runs, items, feedback
│   │
│   └── web/
│       ├── app.py           # Reflex UI components & Starlette API endpoints
│       └── state.py         # Reflex state management (DigestState)
│
├── tests/                   # Test suite covering all components
│
└── scripts/
    └── run_weekly.py        # Pipeline orchestration, scheduler, CLI
```

---

## How to Work on This Project

For each module:

1. **Explain** what the module needs to do — make sure Kurt understands the concept
2. **Create** the file with clear comments explaining each part
3. **Test** with a simple example
4. **Review** together — Kurt should understand before moving on
5. **Iterate** if needed

Kurt is learning as he builds. Don't rush ahead. One module at a time.

Example start:
> "Let's work on the Notion client wrapper. First, I'll explain what functions we need, then create src/notion/client.py with clear comments. We'll start simple — just the function to query a database. We'll add more later."

---

## Success Criteria

- [x] Can fetch emails from M365 newsletter folder
- [x] Can extract content from Medium with login
- [x] Can check Notion for duplicates across all databases
- [x] Can score items with LLM (dual backend: local + Claude API)
- [x] Can route items to correct database
- [x] Web interface shows digest with skip reason badges
- [x] Clicking item shows details with dedup match info
- [x] Accept saves to Notion with correct field mapping
- [x] Feedback is stored
- [x] System suggests rule improvements over time
- [x] Listicle articles exploded into individual sub-items
- [x] Context overflow handling for local LLM backend
- [x] Docker deployment on Hetzner VPS

---

## Note for Claude Code

Kurt is not a professional developer, but he is technical and wants to learn. Always:
- Explain concepts before implementing
- Add clear comments in code
- Keep modules focused and simple
- Test each piece before moving on
- Ask if something is unclear before proceeding
