import argparse
import re

import duckdb
from mcp.server.fastmcp import FastMCP

_parser = argparse.ArgumentParser(description="IPEDS MCP server")
_parser.add_argument("--db", default="./data/ipeds.db", help="Path to the DuckDB database file")
_args, _ = _parser.parse_known_args()
DB_PATH = _args.db
_YEAR_PREFIX_RE = re.compile(r"^y\d{4}_", re.IGNORECASE)
SERVER_CONTEXT = """
This server provides read-only access to a duckdb instance containing 20 years of IPEDS data from
2004-05 to 2023-24. The database contains rich metadata tables for each year describing the data tables
in detail. All the tables have a prefix "y<YYYY>" to indicate the year. All the metadata tables have a
suffix "_meta". For example, y2023_Tables23_meta. The main metadata tables of interest are:
- Tables<YY> - Contains a list of all the tables for the year along with a short description.
- varTable<YY> - Contains a list of all the variables in each table along with a description of the variable and data type.
- valueSets<YY> - Contains a list of all the possible values for each categorical variable. 
    E.g. to understand categorical numbers like 1=Public, 2=Private, check the valueSets<YY> table.
- newVariables<YY> - not all the years have this. Contains a list of new variables that were added in the given year.
"""

mcp = FastMCP("IPEDS_Navigator", instructions=SERVER_CONTEXT)


def get_connection():
    return duckdb.connect(DB_PATH, read_only=True)


@mcp.tool()
def get_meta_tables_by_year(year: int) -> list[str]:
    """Get a list of tables that contain meta data for a given year"""
    con = get_connection()
    df = con.execute("SHOW TABLES").df()
    tables = df["name"].astype(str).to_list()
    prefix = f"y{year}"
    meta_tables = []
    for table in tables:
        if table.startswith(prefix) and table.endswith("meta"):
            meta_tables.append(table)
    return meta_tables


@mcp.tool()
def get_table_descriptions_by_year(year: int) -> dict:
    """Get descriptions for each table that exist for a given year
    Uses the meta table y<YYYY>_Tables<YY>_meta
    """
    con = get_connection()
    df = con.execute(
        f"SELECT TableName, TableTitle, Description FROM y{year}_Tables{str(year)[2:]}_meta;"
    ).df()
    df["TableName"] = df["TableName"].apply(lambda x: f"y{year}_{x}")
    df["Description"] = df["TableTitle"] + "\n" + df["Description"]
    df.drop(columns=["TableTitle"], inplace=True)
    df.set_index("TableName", inplace=True)
    return df.to_dict()["Description"]


@mcp.tool()
def get_column_descriptions_by_table(year: int, table: str) -> dict:
    """Get descriptions for each column in a given table for a given year.
    Uses the meta table y<YYYY>_varTable<YY>_meta.
    Returns a dict keyed by VarName with fields: TableName, VarNumber, VarTitle, LongDescription.
    Accepts either a qualified name ('y2023_HD2023') or a raw name ('HD2023').
    """
    con = get_connection()
    try:
        row = con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE lower(table_name) LIKE lower(?) "
            "LIMIT 1",
            [f"y{year}_%vartable%"],
        ).fetchone()
        if row is None:
            return {}
        vt_table = row[0]

        raw_table = _YEAR_PREFIX_RE.sub("", table.strip())

        df = con.execute(
            f'SELECT TableName, VarNumber, VarName, VarTitle, LongDescription FROM "{vt_table}" '
            "WHERE lower(TableName) = lower(?) "
            "ORDER BY VarOrder, VarNumber",
            [raw_table],
        ).df()
    finally:
        con.close()

    if df.empty:
        return {}

    df.columns = [c.lower() for c in df.columns]
    df.set_index("varname", inplace=True)
    return df.to_dict(orient="index")


@mcp.tool()
def execute_readonly_sql(sql: str) -> str:
    """
    Executes a read-only SELECT query against the DuckDB database to fetch actual data.
    DuckDB is connected in read-only mode, so INSERT/UPDATE/DELETE will fail.
    """
    # Additional app-level safety check
    if not sql.strip().lower().startswith(
        "select"
    ) and not sql.strip().lower().startswith("with"):
        return "Error: Only SELECT or WITH (CTE) statements are allowed."

    conn = get_connection()
    try:
        # Limit results to prevent massive context overload in the LLM
        # The AI should be smart enough to use LIMIT, but this is a good safety net
        results = conn.execute(sql).fetchall()
        columns = [d[0] for d in conn.description]
        conn.close()

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
        table: The data table that contains the column. Either a qualified name
            ("y2023_HD2023") or a raw name ("HD2023") is accepted.
        var_name: The column (variable) name whose value set you want, e.g. "CONTROL".
    """
    con = get_connection()
    try:
        # Find the valuesets table for this year (name casing varies across years).
        row = con.execute(
            "SELECT table_name FROM duckdb_tables() "
            "WHERE lower(table_name) LIKE lower(?) "
            "LIMIT 1",
            [f"y{year}_%valueset%"],
        ).fetchone()
        if row is None:
            return f"No valuesets table found for year {year}."
        vs_table = row[0]

        # Strip year prefix so the lookup matches the raw TableName stored in valuesets.
        raw_table = _YEAR_PREFIX_RE.sub("", table.strip())

        df = con.execute(
            f'SELECT Codevalue, ValueLabel FROM "{vs_table}" '
            "WHERE lower(TableName) = lower(?) AND lower(VarName) = lower(?) "
            "ORDER BY ValueOrder, Codevalue",
            [raw_table, var_name],
        ).df()
        # Normalize column names — older years use lowercase (valueLabel, etc.)
        df.columns = [c.lower() for c in df.columns]
    finally:
        con.close()

    if df.empty:
        return (
            f"No value set found for column {var_name!r} in table {raw_table!r} "
            f"(year {year}). The column may be continuous or the name may differ."
        )

    lines = [f"Value set for {raw_table}.{var_name} (year {year}):"]
    for _, r in df.iterrows():
        lines.append(f"  {r['codevalue']} — {r['valuelabel']}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
