"""Ingest IPEDS Access DBs into Postgres, preserving the original Access column types.

Types come from `mdb-schema` (the real schema stored in the .accdb), not from
sampling the CSV. Postgres then loads the exported CSV via COPY with that schema
forced, so no per-table type guessing happens and the same column has the same
type in every year's table.

Data tables keep their original IPEDS table names (already consistent across
years); metadata table names drift in casing between years, so they are
lowercased and given a '_meta' suffix. Column names are lowercased everywhere so
clients never have to quote a column. Table names are mixed case in general, so
always quote them.

The NCES-derived rollup tables (DRV*, DFR*) are not ingested, and the rows
describing them are dropped from every metadata table that names tables, so the
catalog only advertises tables that actually exist; see EXCLUDED_PREFIXES.
Revised-data catalogs (e.g. Tables19_RV) are skipped entirely.
"""

import argparse
import os
import re
import subprocess

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from tqdm import tqdm

# Load configuration (DATABASE_URL, ...) from a local .env if one is present.
load_dotenv()

ACCDB_PATH = "./data/accdb/"
# No hardcoded fallback: the connection URL (with credentials) must come from the
# environment / .env so secrets never live in source. See .env.example.
DATABASE_URL = os.environ.get("DATABASE_URL")
# Fixed date format we ask mdb-export to emit; matches Postgres timestamp input.
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Access type (as printed by `mdb-schema`, default backend) -> Postgres type.
TYPE_MAP = {
    "Text": sql.SQL("VARCHAR"),
    "Memo/Hyperlink": sql.SQL("VARCHAR"),
    "GUID": sql.SQL("VARCHAR"),
    "Replication ID": sql.SQL("VARCHAR"),
    "Boolean": sql.SQL("BOOLEAN"),
    "Byte": sql.SQL("SMALLINT"),
    "Integer": sql.SQL("INTEGER"),  # Access "Integer" is 16-bit; widen to be safe
    "Long Integer": sql.SQL("INTEGER"),
    "Single": sql.SQL("REAL"),
    "Double": sql.SQL("DOUBLE PRECISION"),
    "Float": sql.SQL("DOUBLE PRECISION"),
    "Currency": sql.SQL("NUMERIC(19,4)"),
    "Numeric": sql.SQL("DOUBLE PRECISION"),
    "Decimal": sql.SQL("DOUBLE PRECISION"),
    "DateTime": sql.SQL("TIMESTAMP"),
    "DateTime (Short)": sql.SQL("TIMESTAMP"),
    "OLE": sql.SQL("BYTEA"),
    "Binary": sql.SQL("BYTEA"),
}

METADATA_TABLES = [
    "tables",
    "valuesets",
    "sectiontable",
    "filenames",
    "vartable",
    "newvariables",
]

# NCES-computed rollups over the survey tables (DRV*, and the older DFR* Data
# Feedback Report tables that DRV* replaced in 2006-07). Not source data, so
# they are skipped on ingest.
EXCLUDED_PREFIXES = ("DRV", "DFR")

# A metadata table is exactly one of METADATA_TABLES followed by a two-digit year,
# e.g. 'Tables23'. Anchored so near-misses (notably the 'Tables19_RV' revised-data
# catalog) are not mistaken for one and quietly ingested as a data table.
_META_KINDS = "|".join(map(re.escape, METADATA_TABLES))
META_RE = re.compile(rf"^(?:{_META_KINDS})\d{{2}}$", re.IGNORECASE)
# Revised-data variants of the metadata catalogs; skipped entirely.
META_RV_RE = re.compile(rf"^(?:{_META_KINDS})\d{{2}}_RV$", re.IGNORECASE)

# Matches lines like:   [COLUMN NAME]		Text (510),
COL_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s+(?P<type>.+?)\s*,?\s*$")


def access_to_pg_type(access_type: str) -> sql.SQL:
    """Map an Access type token, ignoring size '(...)' and a 'NOT NULL' suffix."""
    base = re.sub(r"\s*\(.*?\)", "", access_type)  # drop size, e.g. Text (510)
    base = re.sub(r"\s+NOT\s+NULL\s*$", "", base, flags=re.IGNORECASE)  # drop NOT NULL
    base = base.strip()
    if base not in TYPE_MAP:
        raise ValueError(f"Unmapped Access type: {access_type!r}")
    return TYPE_MAP[base]


def get_schema(db_path: str, table: str) -> dict[str, sql.SQL]:
    """Return {column_name: postgres_type} for one table, in column order."""
    out = subprocess.run(
        ["mdb-schema", "-T", table, db_path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # Keep only the lines between the opening '(' and the closing ');'.
    cols: dict[str, sql.SQL] = {}
    in_body = False
    for line in out.splitlines():
        if not in_body:
            if line.strip().startswith("("):
                in_body = True
            continue
        if line.strip().startswith(")"):
            break
        m = COL_RE.match(line)
        if m:
            cols[m.group("name")] = access_to_pg_type(m.group("type"))
    if not cols:
        raise ValueError(f"No columns parsed for {table} in {db_path}")
    return cols


def column_defs(cols: dict[str, sql.SQL]) -> sql.Composed:
    """Render CREATE TABLE column definitions.

    Every column name is lowercased, in data and metadata tables alike, so
    clients can write unquoted SQL (`SELECT unitid FROM "HD2023"`).

    Column names go through sql.Identifier for proper quoting; the Postgres type
    comes from the controlled TYPE_MAP, so it is injected as raw SQL.
    """
    return sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(name.lower()), dtype)
        for name, dtype in cols.items()
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest IPEDS Access DBs into Postgres.")
    p.add_argument(
        "--accdb-dir",
        default=ACCDB_PATH,
        help="Directory containing the IPEDS .accdb files.",
    )
    p.add_argument(
        "--database-url",
        default=DATABASE_URL,
        help="Postgres connection URL (defaults to $DATABASE_URL; there is no built-in default).",
    )
    p.add_argument(
        "--drop-existing",
        action="store_true",
        help=(
            "Drop each destination table before creating it, instead of refusing to "
            "run when a previous ingest is present. Use this to recover from a run "
            "that failed partway through. It only drops tables this run recreates, "
            "so tables left by an ingest that used different names must be dropped "
            "by hand."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.database_url:
        raise SystemExit(
            "DATABASE_URL is not set. Add it to a .env file (see .env.example) "
            "or pass --database-url."
        )
    # The target database must already exist; we don't create it. Fail loud with a
    # hint if it's unreachable (wrong URL, server down, or DB not created yet).
    try:
        con = psycopg.connect(args.database_url)
    except psycopg.OperationalError as e:
        raise SystemExit(
            f"Could not connect to the database: {e}\n"
            "Check DATABASE_URL, and create the database first if it doesn't exist "
            "(e.g. `createdb <dbname>`)."
        ) from e
    con.autocommit = True

    # Table names are unprefixed (IPEDS names are already globally unique); metadata
    # tables carry a '_meta' suffix, so their presence marks an existing ingest.
    existing = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name LIKE '%!_meta' ESCAPE '!'"
    ).fetchone()
    if existing is not None and existing[0] and not args.drop_existing:
        raise SystemExit(
            f"Database already has {existing[0]} *_meta tables; drop them first to "
            "re-ingest, or pass --drop-existing to overwrite them as they are rebuilt."
        )

    seen: dict[str, str] = {}  # table -> source db, to catch name collisions

    dbs = sorted(f for f in os.listdir(args.accdb_dir) if f.lower().endswith(".accdb"))
    for db in tqdm(dbs, desc="years"):
        accdb = os.path.join(args.accdb_dir, db)
        tables = subprocess.run(
            ["mdb-tables", "-1", accdb],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        # `-1` prints one name per line precisely because names can contain spaces.
        tables = [t.strip() for t in tables if t.strip()]

        for table in tqdm(tables, desc=db, leave=False):
            if table.upper().startswith(EXCLUDED_PREFIXES):
                continue
            # Revised-data catalogs duplicate the regular ones for a subset of rows;
            # ingesting them would advertise a second catalog per year.
            if META_RV_RE.match(table):
                tqdm.write(f"skipping revised-data metadata table {table} in {db}")
                continue

            # Data tables keep their original IPEDS name (already consistent across
            # years). Metadata table names drift in casing across years, so they get
            # a '_meta' suffix and are lowercased for a uniform schema when exploring
            # information_schema. Columns are lowercased everywhere (see column_defs).
            is_meta = META_RE.match(table) is not None  # e.g. 'Tables23'
            dest = (table + "_meta").lower() if is_meta else table
            if dest in seen:
                raise SystemExit(
                    f"Duplicate table name {dest!r} in {accdb} "
                    f"(first seen in {seen[dest]}). Resolve naming before ingest."
                )
            seen[dest] = accdb

            cols = get_schema(accdb, table)
            if args.drop_existing:
                con.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(dest))
                )
            con.execute(
                sql.SQL("CREATE TABLE {} ({})").format(
                    sql.Identifier(dest), column_defs(cols)
                )
            )

            # Stream mdb-export straight into COPY: the largest IPEDS tables are
            # hundreds of MB, so the CSV is never held in memory as a whole. Bytes
            # are passed through untouched; decoding them here would go through the
            # process locale and mangle non-ASCII text. The exit code is checked
            # *inside* the copy block so a dead mdb-export (which otherwise just
            # looks like EOF) aborts the COPY instead of committing a partial table.
            with subprocess.Popen(
                ["mdb-export", "-D", DATE_FMT, "-q", '"', "-X", '"', accdb, table],
                stdout=subprocess.PIPE,
            ) as export:
                with (
                    con.cursor() as cur,
                    cur.copy(
                        sql.SQL(
                            "COPY {} FROM STDIN WITH (FORMAT csv, HEADER true, "
                            "NULL '', QUOTE '\"', ESCAPE '\"')"
                        ).format(sql.Identifier(dest))
                    ) as copy,
                ):
                    while chunk := export.stdout.read(1 << 20):
                        copy.write(chunk)
                    if export.wait() != 0:
                        raise SystemExit(
                            f"mdb-export failed for {table} in {accdb} "
                            f"(exit {export.returncode}); see stderr above."
                        )

            # The DRV*/DFR* data tables are skipped above, so drop the catalog rows
            # describing them from every metadata table that names tables; otherwise
            # the metadata advertises tables that aren't in the database. upper()
            # guards against casing drift across years.
            if is_meta and "tablename" in {c.lower() for c in cols}:
                con.execute(
                    sql.SQL(
                        "DELETE FROM {} WHERE upper(tablename) LIKE ANY(%s)"
                    ).format(sql.Identifier(dest)),
                    [[p + "%" for p in EXCLUDED_PREFIXES]],
                )

    con.close()
    print(
        f"Done. Ingested {len(seen)} tables into {args.database_url.rsplit('/', 1)[-1]}."
    )


if __name__ == "__main__":
    main()
