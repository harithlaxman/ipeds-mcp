import os

import psycopg
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load configuration (DATABASE_URL, ...) from a local .env if one is present.
load_dotenv()

# No hardcoded fallback: the connection URL (with credentials) must come from the
# environment / .env so secrets never live in source. See .env.example.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit(
        "DATABASE_URL is not set. Add it to a .env file (see .env.example) "
        "or set it in the environment."
    )

# Cap on rows returned by execute_readonly_sql, to keep a stray unbounded SELECT
# from flooding the model's context.
MAX_ROWS = 500

SERVER_CONTEXT = """
This server provides read-only access to a PostgreSQL database containing 20 years of IPEDS data from
2004-05 to 2023-24. Data tables keep their original IPEDS names, which already embed the year
(e.g. HD2023, EFFY2023). Data table NAMES are mixed/upper case and must be double-quoted, but their
COLUMN names are all lowercase; never upper-case-quote a column (e.g. SELECT unitid, instnm
FROM "HD2023" is correct; SELECT "UNITID" FROM "HD2023" fails).
The database also contains rich metadata tables for each year
describing the data tables in detail. All the metadata tables have a suffix "_meta" and embed the
two-digit year (e.g. tables23_meta). Metadata table names and their column names are all lowercase.
The main metadata tables of interest are:
- tables<YY>_meta - Contains a list of all the tables for the year along with a description.
- vartable<YY>_meta - Contains a list of all the variables in each table along with a description of the variable and data type.
- valuesets<YY>_meta - Contains a list of all the possible values for each categorical variable.
    E.g. to understand categorical numbers like 1=Public, 2=Private, check the valuesets<YY>_meta table.
- newvariables<YY>_meta - not all the years have this. Contains a list of new variables that were added in the given year.
"""

mcp = FastMCP(
    "IPEDS_Navigator",
    instructions=SERVER_CONTEXT,
    # Bind all interfaces so the server is reachable from outside the container.
    host="0.0.0.0",
    port=8000,
)


def get_connection():
    # Read-only enforced at the session level; the SQL tool also gates on SELECT/WITH.
    con = psycopg.connect(
        DATABASE_URL,
        options="-c default_transaction_read_only=on",
        connect_timeout=10,
    )
    con.autocommit = True
    return con


def _two_digit_year(year: int) -> str:
    """Return the two-digit form of a four-digit year, rejecting anything else.

    Without this, `str(23)[2:]` yields '' and the LIKE patterns below match every
    year's metadata tables at once. The lower bound is 2000 rather than 1900 so a
    typo like 1923 errors instead of quietly serving 2023 data.
    """
    if not 2000 <= year <= 2100:
        raise ValueError(
            f"year must be a four-digit year between 2000 and 2100, got {year!r}. "
            "Pass the IPEDS survey start year, e.g. 2023."
        )
    return f"{year % 100:02d}"


def _find_meta_table(con, year: int, pattern: str) -> str | None:
    # Metadata tables embed the two-digit year and carry a '_meta' suffix,
    # e.g. vartable23_meta. Matched case-insensitively to be safe. ORDER BY keeps
    # the choice deterministic if a second table ever matches the pattern.
    yy = _two_digit_year(year)
    row = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND lower(table_name) LIKE lower(%s) ESCAPE '!' "
        "ORDER BY table_name LIMIT 1",
        [f"%{pattern}%{yy}!_meta"],
    ).fetchone()
    return row[0] if row else None


def _has_column(con, table: str, column: str) -> bool:
    """True if `table` has `column` (used to detect postprocess.py's output)."""
    row = con.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        [table, column],
    ).fetchone()
    return row is not None


@mcp.tool()
def get_meta_tables_by_year(year: int) -> list[str]:
    """Get a list of tables that contain meta data for a given year"""
    yy = _two_digit_year(year)
    with get_connection() as con:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE %s ESCAPE '!'",
            [f"%{yy}!_meta"],
        ).fetchall()
    return [r[0] for r in rows]


@mcp.tool()
def get_table_descriptions_by_year(year: int) -> dict:
    """Get descriptions for each table that exist for a given year
    Uses the meta table tables<YY>_meta

    Prefers the condensed `description_summary` written by postprocess.py and
    falls back to the raw `description` where no summary exists.
    """
    with get_connection() as con:
        t_table = _find_meta_table(con, year, "tables")
        if t_table is None:
            return {}
        # description_summary only exists once postprocess.py has run.
        desc_expr = (
            "coalesce(description_summary, description)"
            if _has_column(con, t_table, "description_summary")
            else "description"
        )
        rows = con.execute(
            f'SELECT tablename, tabletitle, {desc_expr} FROM "{t_table}"'
        ).fetchall()
    # Missing title/description are dropped rather than rendered as "None".
    return {
        name: "\n".join(part for part in (title, desc) if part)
        for name, title, desc in rows
    }


@mcp.tool()
def get_column_descriptions_of_table(year: int, table: str) -> dict:
    """Get descriptions for each column in a given table for a given year.
    Returns a dict keyed by VarName with fields: tablename, varnumber, vartitle, longdescription.
    Pass the raw IPEDS table name, e.g. 'HD2023'.
    """
    with get_connection() as con:
        vt_table = _find_meta_table(con, year, "vartable")
        if vt_table is None:
            return {}

        rows = con.execute(
            f'SELECT tablename, varnumber, varname, vartitle, longdescription FROM "{vt_table}" '
            "WHERE lower(tablename) = lower(%s) "
            "ORDER BY varorder, varnumber",
            [table.strip()],
        ).fetchall()

    return {
        varname: {
            "tablename": tablename,
            "varnumber": varnumber,
            "vartitle": vartitle,
            "longdescription": longdescription,
        }
        for tablename, varnumber, varname, vartitle, longdescription in rows
    }


# Built from MAX_ROWS instead of a docstring so the row cap advertised to clients
# can't drift from the one actually enforced below.
SQL_TOOL_DESCRIPTION = f"""
Executes a read-only SELECT query against the PostgreSQL database to fetch actual data.
The session is read-only, so INSERT/UPDATE/DELETE will fail.
Table names are mixed case and must be double-quoted, e.g. SELECT * FROM "HD2023".
Column names are all lowercase.
At most {MAX_ROWS} rows are returned; add your own LIMIT/aggregation for bigger results.
"""


@mcp.tool(description=SQL_TOOL_DESCRIPTION)
def execute_readonly_sql(sql: str) -> str:
    # Additional app-level safety check
    if not sql.strip().lower().startswith(
        "select"
    ) and not sql.strip().lower().startswith("with"):
        return "Error: Only SELECT or WITH (CTE) statements are allowed."

    try:
        with get_connection() as conn:
            cur = conn.execute(sql)
            # Cap the rows pulled into the LLM's context. One extra row is fetched
            # purely to detect (and report) truncation.
            results = cur.fetchmany(MAX_ROWS + 1)
            columns = [d[0] for d in cur.description] if cur.description else []

        if not results:
            return "Query executed successfully, but returned no rows."

        truncated = len(results) > MAX_ROWS
        results = results[:MAX_ROWS]

        # Format as a list of dictionaries for the LLM
        output = [dict(zip(columns, row)) for row in results]
        rendered = str(output)
        if truncated:
            rendered += (
                f"\n\n[Truncated: only the first {MAX_ROWS} rows are shown. "
                "Refine the query with LIMIT, filters, or aggregation.]"
            )
        return rendered
    except Exception as e:
        return f"SQL Error: {str(e)}"


@mcp.tool()
def lookup_valueset(year: int, table: str, var_name: str) -> str:
    """Return all allowed code values and their labels for a categorical IPEDS column.

    Use this whenever a column stores integer or short-string codes whose meaning
    is not obvious (e.g. CONTROL=1 means "Public", CARNEGIE=15 means "Doctoral").
    Call it before writing WHERE filters or SELECT labels so you use the right codes.

    Args:
        year: The IPEDS survey start year, e.g. 2023.
        table: The data table that contains the column, e.g. "HD2023".
        var_name: The column (variable) name whose value set you want, e.g. "CONTROL".
    """
    with get_connection() as con:
        # Find the valuesets table for this year (name casing varies across years).
        vs_table = _find_meta_table(con, year, "valueset")
        if vs_table is None:
            return f"No valuesets table found for year {year}."

        raw_table = table.strip()

        # codevalue is Text in every year, so the tiebreak sorts it numerically when
        # it looks like a number (otherwise '10' would sort before '9') and falls
        # back to text order. NULLS LAST keeps missing sort keys out of the middle.
        rows = con.execute(
            f'SELECT codevalue, valuelabel FROM "{vs_table}" '
            "WHERE lower(tablename) = lower(%s) AND lower(varname) = lower(%s) "
            "ORDER BY valueorder NULLS LAST, "
            "CASE WHEN codevalue ~ '^\\s*-?[0-9]+(\\.[0-9]+)?\\s*$' "
            "THEN trim(codevalue)::numeric END NULLS LAST, "
            "codevalue NULLS LAST",
            [raw_table, var_name],
        ).fetchall()

    if not rows:
        return (
            f"No value set found for column {var_name!r} in table {raw_table!r} "
            f"(year {year}). The column may be continuous or the name may differ."
        )

    lines = [f"Value set for {raw_table}.{var_name} (year {year}):"]
    for codevalue, valuelabel in rows:
        lines.append(f"  {codevalue} — {valuelabel}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
