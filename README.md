# MCP to explore IPEDS data

The repo helps with ingesting the IPEDS data from https://nces.ed.gov/ipeds/use-the-data/download-access-database
and creates a MCP to navigate the database.

### Required
- All necessary .accdb files in one directory
- mdb-tools (refer to installation guide - https://github.com/mdbtools/mdbtools)
- uv (https://docs.astral.sh/uv/getting-started/installation)
- A PostgreSQL database (the target of the ingest; it must already exist)
- Docker + Docker Compose, to run the server the supported way

### Fetch IPEDS databases
`fetch_db.sh` downloads IPEDS Access Database archives and extracts their `.accdb` files.

Check the [IPEDS download page](https://nces.ed.gov/ipeds/use-the-data/download-access-database) to confirm which academic years are available.

Download one year or an inclusive range, optionally choosing an output directory:
```sh
./fetch_db.sh 2023
./fetch_db.sh --range 2004 2023
./fetch_db.sh --range 2004 2023 --out PATH_TO_ACCDB_DIR
```

Files are written to `./data/accdb` by default.

### Setup

Install dependencies
```sh
uv sync
```

Configure the connection. Copy `.env.example` to `.env` and set `DATABASE_URL` to your
Postgres connection string (`postgresql://USER:PASSWORD@HOST:PORT/DBNAME`). Every script
reads it from there; there is no built-in default, and nothing runs without it. The target
database must already exist — the ingest creates tables, not the database.

### Ingest

```sh
uv run python ingest.py --accdb-dir PATH_TO_ACCDB_DIR
```

`--database-url` overrides `$DATABASE_URL` if you need a one-off target. The ingest refuses
to run when a previous ingest is already present; pass `--drop-existing` to rebuild each
table as it goes (use this to recover from a run that failed partway through).

`--drop-existing` only drops the tables the current run recreates. Migrating from an older
ingest that used different table names leaves those tables behind, still advertised to
clients — drop that schema by hand (e.g. `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`)
before re-ingesting.

### Post-process (optional)

`postprocess.py` adds a `description_summary` column to each `tables<YY>_meta` table and
fills it with an LLM-condensed version of the long free-text table descriptions, via Azure
OpenAI. `get_table_descriptions_by_year` prefers those summaries when they exist and falls
back to the raw descriptions otherwise, so this step is optional.

Set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY` and `AZURE_OPENAI_DEPLOYMENT` in `.env`, then:
```sh
uv run python postprocess.py
```

It is resumable: only rows without a summary are sent to the model, so an interrupted run
can simply be re-run.

### Run the MCP server

The supported path is Docker Compose, which reads `DATABASE_URL` from the environment or the
`.env` file next to `docker-compose.yml`:
```sh
docker compose up --build
```

The server speaks streamable-HTTP; the MCP endpoint is `http://localhost:8000/mcp`. To run it
without Docker instead:
```sh
uv run python server.py
```

Add the MCP to Claude Code
```sh
claude mcp add --transport http ipeds http://localhost:8000/mcp
```

For any other client, register it as a streamable-HTTP (not stdio) MCP server pointing at
`http://localhost:8000/mcp`; check your client's docs for where that goes.

> **Note:** the container publishes port 8000 with no authentication in front of the MCP
> endpoint. That is fine bound to localhost, but put an authenticating proxy in front of it
> (and restrict the published port) before exposing it on any shared or public network.
