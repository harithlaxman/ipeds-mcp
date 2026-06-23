# MCP to explore IPEDS data

The repo helps with ingesting the IPEDS data from https://nces.ed.gov/ipeds/use-the-data/download-access-database
and creates a MCP to navigate the database.

### Required
- All necessary .accdb files in one directory
- mdb-tools (refer to installation guide - https://github.com/mdbtools/mdbtools)
- uv (https://docs.astral.sh/uv/getting-started/installation)

### Setup
Install Dependencies
`uv sync`

Ingest the data to a single DB (if DB doesn't exist)
`uv run python ingest.py --accdb-dir PATH_TO_ACCDB_DIR --db PATH_TO_DB`

Add MCP to claude/codex
`claude mcp add ipeds -- uv --directory PATH_TO_PROJECT_ROOT run python ipeds_mcp.py --db PATH_TO_DB`
`codex mcp add ipeds -- uv --directory PATH_TO_PROJECT_ROOT run python ipeds_mcp.py --db PATH_TO_DB`

If `--db` is omitted, it defaults to `./data/ipeds.db`.
