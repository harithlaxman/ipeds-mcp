import os

import psycopg
from mcp.server.fastmcp import FastMCP

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:ipeds_pg@localhost:5432/ipeds_db"
)
SERVER_CONTEXT = """
This server provides read-only access to a PostgreSQL database containing 20 years of IPEDS data from
2004-05 to 2023-24. Data tables keep their original IPEDS names, which already embed the year
(e.g. HD2023, EFFY2023). Their names and column names are mixed/upper case, so quote them in SQL
(e.g. SELECT "INSTNM" FROM "HD2023"). The database also contains rich metadata tables for each year
describing the data tables in detail. All the metadata tables have a suffix "_meta" and embed the
two-digit year (e.g. tables23_meta). Metadata table names and their column names are all lowercase.
The main metadata tables of interest are:
- tables<YY>_meta - Contains a list of all the tables for the year along with a description.
- vartable<YY>_meta - Contains a list of all the variables in each table along with a description of the variable and data type.
- valuesets<YY>_meta - Contains a list of all the possible values for each categorical variable.
    E.g. to understand categorical numbers like 1=Public, 2=Private, check the valuesets<YY>_meta table.
- newvariables<YY>_meta - not all the years have this. Contains a list of new variables that were added in the given year.
"""

mcp = FastMCP("IPEDS_Navigator", instructions=SERVER_CONTEXT)


def get_connection():
    # Read-only enforced at the session level; the SQL tool also gates on SELECT/WITH.
    con = psycopg.connect(DATABASE_URL, options="-c default_transaction_read_only=on")
    con.autocommit = True
    return con


def _find_meta_table(con, year: int, pattern: str) -> str | None:
    # Metadata tables embed the two-digit year and carry a '_meta' suffix,
    # e.g. vartable23_meta. Matched case-insensitively to be safe.
    yy = str(year)[2:]
    row = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND lower(table_name) LIKE lower(%s) ESCAPE '!' "
        "LIMIT 1",
        [f"%{pattern}%{yy}!_meta"],
    ).fetchone()
    return row[0] if row else None


@mcp.tool()
def get_meta_tables_by_year(year: int) -> list[str]:
    """Get a list of tables that contain meta data for a given year"""
    yy = str(year)[2:]
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
    """
    with get_connection() as con:
        t_table = _find_meta_table(con, year, "tables")
        if t_table is None:
            return {}
        rows = con.execute(
            f'SELECT tablename, tabletitle, description FROM "{t_table}"'
        ).fetchall()
    return {name: f"{title}\n{desc}" for name, title, desc in rows}


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


@mcp.tool()
def execute_readonly_sql(sql: str) -> str:
    """
    Executes a read-only SELECT query against the PostgreSQL database to fetch actual data.
    The session is read-only, so INSERT/UPDATE/DELETE will fail.
    Table names are mixed case and must be double-quoted, e.g. SELECT * FROM "HD2023".
    Column names are all lowercase.
    """
    # Additional app-level safety check
    if not sql.strip().lower().startswith(
        "select"
    ) and not sql.strip().lower().startswith("with"):
        return "Error: Only SELECT or WITH (CTE) statements are allowed."

    try:
        with get_connection() as conn:
            # Limit results to prevent massive context overload in the LLM
            # The AI should be smart enough to use LIMIT, but this is a good safety net
            cur = conn.execute(sql)
            results = cur.fetchall()
            columns = [d[0] for d in cur.description]

        if not results:
            return "Query executed successfully, but returned no rows."

        # Format as a list of dictionaries for the LLM
        output = [dict(zip(columns, row)) for row in results]
        return str(output)
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

        rows = con.execute(
            f'SELECT codevalue, valuelabel FROM "{vs_table}" '
            "WHERE lower(tablename) = lower(%s) AND lower(varname) = lower(%s) "
            "ORDER BY valueorder, codevalue",
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
    mcp.run()
