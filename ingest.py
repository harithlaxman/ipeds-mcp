"""Ingest IPEDS Access DBs into duckdb, preserving the original Access column types.

Types come from `mdb-schema` (the real schema stored in the .accdb), not from
sampling the CSV. duckdb then loads the exported CSV with that schema forced, so
no per-table type guessing happens and the same column has the same type in
every year's table.
"""

import argparse
import os
import re
import subprocess
import tempfile

import duckdb
from tqdm import tqdm

DB_PATH = "ipeds.db"
# Fixed date format we ask mdb-export to emit, and tell duckdb to expect.
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Access type (as printed by `mdb-schema`, default backend) -> duckdb type.
TYPE_MAP = {
    "Text": "VARCHAR",
    "Memo/Hyperlink": "VARCHAR",
    "GUID": "VARCHAR",
    "Replication ID": "VARCHAR",
    "Boolean": "BOOLEAN",
    "Byte": "UTINYINT",
    "Integer": "INTEGER",          # Access "Integer" is 16-bit; widen to be safe
    "Long Integer": "INTEGER",
    "Single": "REAL",
    "Double": "DOUBLE",
    "Float": "DOUBLE",
    "Currency": "DECIMAL(19,4)",
    "Numeric": "DOUBLE",
    "Decimal": "DOUBLE",
    "DateTime": "TIMESTAMP",
    "DateTime (Short)": "TIMESTAMP",
    "OLE": "BLOB",
    "Binary": "BLOB",
}

METADATA_TABLES = ["tables", "valuesets", "sectiontable", "filenames", "vartable", "newvariables"]

# Matches lines like:   [COLUMN NAME]		Text (510),
COL_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s+(?P<type>.+?)\s*,?\s*$")

# Filename like IPEDS200405.accdb -> start year 2004.
YEAR_RE = re.compile(r"(\d{4})\d{2}")


def start_year(db_filename: str) -> str:
    m = YEAR_RE.search(db_filename)
    if not m:
        raise ValueError(f"Could not parse year from {db_filename!r}")
    return m.group(1)


def access_to_duckdb_type(access_type: str) -> str:
    """Map an Access type token, ignoring size '(...)' and a 'NOT NULL' suffix."""
    base = re.sub(r"\s*\(.*?\)", "", access_type)        # drop size, e.g. Text (510)
    base = re.sub(r"\s+NOT\s+NULL\s*$", "", base, flags=re.I)  # drop NOT NULL
    base = base.strip()
    if base not in TYPE_MAP:
        raise ValueError(f"Unmapped Access type: {access_type!r}")
    return TYPE_MAP[base]


def get_schema(db_path: str, table: str) -> dict[str, str]:
    """Return {column_name: duckdb_type} for one table, in column order."""
    out = subprocess.run(
        ["mdb-schema", "-T", table, db_path],
        capture_output=True, text=True, check=True,
    ).stdout

    # Keep only the lines between the opening '(' and the closing ');'.
    cols: dict[str, str] = {}
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
            cols[m.group("name")] = access_to_duckdb_type(m.group("type"))
    if not cols:
        raise ValueError(f"No columns parsed for {table} in {db_path}")
    return cols


def columns_struct(cols: dict[str, str]) -> str:
    """Render a duckdb struct literal for read_csv's `columns=` argument."""
    inner = ", ".join(f"'{name}': '{dtype}'" for name, dtype in cols.items())
    return "{" + inner + "}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest IPEDS Access DBs into duckdb.")
    parser.add_argument("--accdb-dir", default="./data/accdb/", help="Directory containing .accdb files (default: ./data/accdb/)")
    parser.add_argument("--db", default=DB_PATH, help=f"Output duckdb path (default: {DB_PATH})")
    args = parser.parse_args()

    adbs_path = args.accdb_dir
    db_path = args.db

    if os.path.exists(db_path):
        raise SystemExit(f"{db_path} already exists; remove it first to re-ingest.")

    con = duckdb.connect(db_path)
    seen: dict[str, str] = {}  # table -> source db, to catch name collisions

    dbs = sorted(os.listdir(adbs_path))
    for db in tqdm(dbs, desc="years"):
        accdb = os.path.join(adbs_path, db)
        year = start_year(db)
        tables = subprocess.run(
            ["mdb-tables", "-1", accdb],
            capture_output=True, text=True, check=True,
        ).stdout.split()

        for table in tqdm(tables, desc=db, leave=False):
            dest = f"y{year}_{table}"
            if table[:-2].lower() in METADATA_TABLES:
                dest += "_meta"
            if dest in seen:
                raise SystemExit(
                    f"Duplicate table name {dest!r} in {accdb} "
                    f"(first seen in {seen[dest]}). Resolve naming before ingest."
                )
            seen[dest] = accdb

            cols = get_schema(accdb, table)

            csv = subprocess.run(
                ["mdb-export", "-D", DATE_FMT, "-q", '"', "-X", '"', accdb, table],
                capture_output=True, text=True, check=True,
            ).stdout

            with tempfile.NamedTemporaryFile(
                "w", suffix=".csv", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(csv)
                tmp = fh.name
            try:
                con.execute(
                    f'CREATE TABLE "{dest}" AS SELECT * FROM read_csv('
                    f"'{tmp}', header=true, "
                    f"columns={columns_struct(cols)}, "
                    f"nullstr='', quote='\"', escape='\"', "
                    f"timestampformat='{DATE_FMT}')"
                )
            finally:
                os.unlink(tmp)

    con.close()
    print(f"Done. Ingested {len(seen)} tables into {db_path}.")


if __name__ == "__main__":
    main()
