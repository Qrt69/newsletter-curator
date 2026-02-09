# Newsletter Curator

A Python tool that connects to a Notion workspace to query and display content from a curated collection of knowledge base databases. Built to support newsletter curation 
by aggregating entries across topics like AI, infrastructure, coding tools, and more.

## Notion Databases

The project connects to the following Notion databases (mapped in `databases.json`):

- **Articles & Reads** — collected articles and reading material
- **Books & Papers** — academic papers and books
- **Notes & Insights** — personal notes and takeaways
- **Topics & Concepts** — high-level topic tracking
- **Infrastructure Knowledge Base** — infrastructure-related knowledge
- **Platforms & Infrastructure** — platform and infra tooling
- **AI Agents & Coding Tools** — AI-powered development tools
- **AI Architecture Topics** — AI system design and architecture
- **Model Information** — AI/ML model details
- **Vibe Coding Tools** — vibe coding ecosystem tools
- **Python Libraries** — notable Python packages
- **DuckDB Extensions** — DuckDB extension catalog
- **TAAFT** — tool/resource directory
- **Overview** — high-level overview dashboard

## Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (package manager)
- A Notion integration with access to the databases listed above

## Setup

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd newsletter-curator
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

3. Create a `.env` file with your Notion API key:
   ```
   NOTION_API_KEY=your_notion_api_key_here
   ```

4. Make sure your Notion integration has access to the relevant databases.

## Usage

Query and display entries from a Notion database:

```bash
uv run python test_notion.py
```

This connects to the configured database, retrieves all entries (with pagination), and prints each entry's properties to the console.

## Project Structure

```
├── databases.json      # Notion database name → ID mapping
├── hello.py            # Entrypoint placeholder
├── test_notion.py      # Notion database query and display script
├── pyproject.toml      # Project metadata and dependencies
├── uv.lock             # Locked dependency versions
├── .env                # Notion API key (not committed)
├── .gitignore          # Git ignore rules
└── .python-version     # Python version pin (3.13)
```

## Dependencies

- [notion-client](https://github.com/ramnes/notion-sdk-py) — Official Notion SDK for Python
- [python-dotenv](https://github.com/theskumar/python-dotenv) — Load environment variables from `.env`