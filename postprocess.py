"""Post-process the ingested Postgres DB: LLM-summarize table descriptions.

Runs *after* `ingest.py`. For every `tables{YY}_meta` table, adds a
`description_summary` column and fills it with a succinct Azure OpenAI summary
of the long `description`. Resumable: only rows where `description_summary IS
NULL` are processed, so a partial run can be re-run without re-ingesting.

Config (from environment / a local .env):
    DATABASE_URL              Postgres connection URL (same one ingest.py uses).
    AZURE_OPENAI_ENDPOINT     e.g. https://<resource>.openai.azure.com
    AZURE_OPENAI_KEY          Azure OpenAI API key.
    AZURE_OPENAI_DEPLOYMENT   Deployment (model) name to call.
    AZURE_OPENAI_API_VERSION  Optional; defaults to a recent stable version.
"""

import argparse
import os

import psycopg
from dotenv import load_dotenv
from openai import AzureOpenAI
from psycopg import sql
from tqdm import tqdm

# Load configuration (DATABASE_URL, Azure creds, ...) from a local .env if present.
load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
DEFAULT_API_VERSION = "2024-10-21"

SUMMARY_SYSTEM_PROMPT = (
    "You summarize metadata descriptions of IPEDS data tables. You are given a table's "
    "title, its year coverage, and its full description. Produce ONE dense, self-contained "
    "summary of the DESCRIPTION that preserves all substantive information (what the table "
    "contains, key entities, notable caveats) while cutting filler and redundancy. "
    "The title and year coverage are stored separately, so do NOT restate them or repeat "
    "any information they already convey (e.g. the years covered, or wording already in the "
    "title). 1-3 sentences. No preamble."
)


def get_azure_client() -> AzureOpenAI:
    """Build an AzureOpenAI client from the environment (fails loud if unset)."""
    try:
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
        api_key = os.environ["AZURE_OPENAI_KEY"]
    except KeyError as e:
        raise SystemExit(
            f"{e.args[0]} is not set. Add it to your .env (see .env.example)."
        ) from e
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
    )


def summarize_description(
    client: AzureOpenAI,
    deployment: str,
    description: str | None,
    title: str | None = None,
    year_coverage: str | None = None,
) -> str | None:
    """Summarize `description`, giving the model `title` and `year_coverage` as context.

    The title/year-coverage are passed so the model can *avoid* repeating information
    they already carry; only the description is summarized. Returns None when there's
    no description to summarize.
    """
    if not description or not description.strip():
        return None
    user_content = (
        f"Table title: {title or '(none)'}\n"
        f"Year coverage: {year_coverage or '(none)'}\n\n"
        f"Description:\n{description}"
    )
    resp = client.chat.completions.create(
        model=deployment,  # Azure "model" == deployment name
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
    )
    content = resp.choices[0].message.content
    return content.strip() if content else None


def find_tables_meta(con: psycopg.Connection) -> list[str]:
    """Return the names of the ingested tables metadata tables (e.g. 'tables23_meta')."""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' "
        "AND lower(table_name) LIKE 'tables%!_meta' ESCAPE '!' "  # ESCAPE '!' to treat it as a escape character
        "ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def summarize_table(
    con: psycopg.Connection, client: AzureOpenAI, deployment: str, table: str
) -> int:
    """Add/populate `description_summary` for one meta table. Returns rows updated."""
    # Idempotent: only adds the column if absent.
    con.execute(
        sql.SQL(
            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS description_summary TEXT"
        ).format(sql.Identifier(table))
    )
    # Resumable: only unsummarized rows. ctid (physical row address) is the only
    # way to update exact rows — early-year metadata tables have no PK.
    rows = con.execute(
        sql.SQL(
            "SELECT ctid::text, tabletitle, yearcoverage, description "
            "FROM {} WHERE description_summary IS NULL"
        ).format(sql.Identifier(table))
    ).fetchall()

    updated = 0
    for ctid, tabletitle, yearcoverage, description in tqdm(
        rows, desc=table, leave=False
    ):
        summary = summarize_description(
            client, deployment, description, tabletitle, yearcoverage
        )
        if summary is None:
            continue
        con.execute(
            sql.SQL(
                "UPDATE {} SET description_summary = %s WHERE ctid = %s::tid"
            ).format(sql.Identifier(table)),
            [summary, ctid],
        )
        updated += 1
    return updated


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Summarize IPEDS table descriptions in Postgres via Azure OpenAI."
    )
    p.add_argument(
        "--database-url",
        default=DATABASE_URL,
        help="Postgres connection URL (defaults to $DATABASE_URL).",
    )
    p.add_argument(
        "--deployment",
        default=os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"),
        help="Azure OpenAI deployment name (defaults to $AZURE_OPENAI_DEPLOYMENT_NAME).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.database_url:
        raise SystemExit(
            "DATABASE_URL is not set. Add it to a .env file (see .env.example) "
            "or pass --database-url."
        )
    if not args.deployment:
        raise SystemExit(
            "AZURE_OPENAI_DEPLOYMENT_NAME is not set. Add it to a .env file (see "
            ".env.example) or pass --deployment."
        )

    client = get_azure_client()
    con = psycopg.connect(args.database_url)
    con.autocommit = True  # commit each UPDATE so an interrupted run keeps its progress

    tables = find_tables_meta(con)
    if not tables:
        raise SystemExit(
            "No 'tables*_meta' tables found. Run ingest.py first (and check DATABASE_URL)."
        )

    total = 0
    for table in tqdm(tables, desc="tables-meta"):
        total += summarize_table(con, client, args.deployment, table)

    con.close()
    print(f"Done. Summarized {total} rows across {len(tables)} meta tables.")


if __name__ == "__main__":
    main()
