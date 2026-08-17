"""Scrape tables from the NCES Digest of Education Statistics into JSONL.

The Digest publishes one HTML page per table (plus an Excel mirror). This script
walks the "current tables" index, keeps the tables under a given chapter
(chapter 3 = Postsecondary Education by default), then parses each table page
into structured records.

Ground truth is read from the HTML table on the table page rather than the Excel
file: the HTML is present for every Digest edition (older editions ship legacy
.xls, which needs a different reader), and colspan/rowspan give us the header
hierarchy directly. The .xlsx URL is still recorded in each record's metadata.

Output is filtered to what the IPEDS database can actually answer:

  * only tables whose SOURCE footnote cites IPEDS and nothing else -- the Digest
    also republishes NPSAS, CPS, HSLS, BPS and Census figures, which no query
    against IPEDS reproduces;
  * only rows and columns whose labels fall inside the database's year coverage
    (2004-05 through 2023-24), since Digest time series reach back to 1949-50.

Pass --all-sources / --no-year-filter to keep everything.

Usage:
    uv run python eval/get_dataset.py --limit 5
    uv run python eval/get_dataset.py --chapter 3 --out eval/digest_ch3.jsonl
    uv run python eval/get_dataset.py --all-sources --no-year-filter
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag

INDEX_URL = "https://nces.ed.gov/programs/digest/current_tables.asp"
BASE_URL = "https://nces.ed.gov/programs/digest/"

USER_AGENT = (
    "ipeds-mcp-eval/0.1 (research dataset builder; "
    "contact via https://github.com/; respects robots.txt)"
)

# Cells that mean "no value" in Digest tables rather than a number.
MISSING_MARKERS = {"†", "‡", "#", "—", "–", "-", "---", "(†)", "(‡)", "!"}

# Trailing rows of a Digest table hold footnotes, not data.
NOTE_PREFIX_RE = re.compile(
    r"^\s*(NOTE|SOURCE|†|‡|#|—|\*|\d+\s)",
)

PLACEHOLDER_COL_RE = re.compile(r"^col_\d+$")

# Surveys named in a table's SOURCE footnote. Only IPEDS-sourced tables can be
# reproduced from the IPEDS database, so the rest are dropped by default.
SURVEY_PATTERNS = {
    "IPEDS": r"IPEDS|Integrated Postsecondary",
    "NPSAS": r"NPSAS|National Postsecondary Student Aid",
    "HSLS": r"HSLS",
    "BPS": r"BPS:",
    "B&B": r"B&B",
    "CPS": r"Current Population Survey",
    "Census": r"Census Bureau",
    "Projections": r"Projections of Education",
    # Chapter 3 also republishes these; naming them keeps the skip list readable.
    "NSOPF": r"NSOPF|National Study of Postsecondary Faculty",
    "SASS": r"Schools and Staffing Survey|SASS",
    "OpenDoors": r"Open Doors|Institute of International Education",
    "NSF": r"National Science Foundation",
    "SED": r"Survey of Earned Doctorates",
    "NSDUH": r"National Survey on Drug Use and Health",
    "PulseSurvey": r"Household Pulse Survey",
    "MLA": r"Modern Language Association",
    "FSA": r"Office of Federal Student Aid",
}

# Year shapes seen in Digest labels: "2021-22", "1999-2000", "Fall 2021",
# "2009 entry cohort", "2010-11 to 2020-21". Only the 4-digit start year is
# captured; an academic year maps to the IPEDS year of its first half, matching
# the database's table naming ("2021-22" -> HD2021).
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?:\s*-\s*(?:\d{2}|\d{4}))?\b")

# Database coverage: IPEDS collection years 2004-05 through 2023-24.
DEFAULT_YEAR_MIN = 2004
DEFAULT_YEAR_MAX = 2023


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def decode_body(body: bytes) -> str:
    """Decode an NCES page.

    The table pages serve UTF-8 bytes but declare ISO-8859-1 (both in the
    Content-Type header and in a <meta> tag), so trusting the declared charset
    turns footnote markers like "†" into mojibake. Try UTF-8 first and fall back
    to cp1252 for the pages that really are single-byte.
    """
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


class Fetcher:
    """Polite HTTP client: one session, retries, delay between calls, disk cache."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        delay: float = 0.5,
        timeout: float = 30.0,
        retries: int = 3,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.cache_dir = cache_dir
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last_request = 0.0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.html"

    def _throttle(self) -> None:
        """Hold `delay` between requests, however the last one turned out."""
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def get_text(self, url: str) -> str:
        cached = self._cache_path(url)
        if cached and cached.exists():
            return cached.read_text(encoding="utf-8")

        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout)
                self._last_request = time.monotonic()
                response.raise_for_status()
                text = decode_body(response.content)
                if cached:
                    cached.write_text(text, encoding="utf-8")
                return text
            except requests.RequestException as exc:
                last_error = exc
                self._last_request = time.monotonic()
                # No point backing off after the last attempt -- nothing follows it.
                if attempt < self.retries - 1:
                    time.sleep(2**attempt)
        raise RuntimeError(f"failed to fetch {url}: {last_error}")

    def head_ok(self, url: str) -> bool:
        try:
            self._throttle()
            response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            self._last_request = time.monotonic()
            return response.status_code == 200
        except requests.RequestException:
            return False


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def clean_text(node: Any) -> str:
    """Collapse whitespace and non-breaking spaces into a single-spaced string."""
    text = node if isinstance(node, str) else node.get_text(" ")
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def strip_footnote_markers(cell: Tag) -> Tag:
    """Remove <sup> footnote markers so 'Total\\1\\' reads as 'Total'."""
    for sup in cell.find_all("sup"):
        sup.decompose()
    return cell


def indent_level(cell: Tag) -> int:
    """Digest row labels encode hierarchy as leading non-breaking spaces."""
    raw = cell.get_text().replace("\t", "").replace("\n", "")
    leading = len(raw) - len(raw.lstrip("\xa0 "))
    return leading // 2


def parse_value(raw: str) -> Any:
    """Return a number when the cell holds one, otherwise the raw marker/string."""
    text = raw.strip()
    if not text:
        return None
    if text in MISSING_MARKERS:
        return text
    candidate = text.replace(",", "").replace("$", "").rstrip("!").strip()
    # Values like "(1.2)" are standard errors, and "12.3" plain numbers.
    negative = candidate.startswith("(") and candidate.endswith(")")
    if negative:
        candidate = candidate[1:-1]
    try:
        number = float(candidate)
    except ValueError:
        return text
    if candidate.lstrip("-").isdigit():
        return int(number)
    return number


def classify_sources(footnotes: List[str]) -> Tuple[List[str], bool]:
    """Read the SOURCE footnote and name the surveys the table draws on.

    Returns (surveys, ipeds_only). A table is IPEDS-only when IPEDS is the sole
    survey matched: anything citing NPSAS/CPS/HSLS/Census alongside it mixes in
    data the IPEDS database does not hold.

    Long time series also cite pre-IPEDS predecessors (HEGIS, the Education
    Directory) for their oldest rows. Those are deliberately not listed above, so
    they neither disqualify a table nor need special-casing here.
    """
    source = " ".join(f for f in footnotes if f.lstrip().upper().startswith("SOURCE"))
    surveys = [
        name
        for name, pattern in SURVEY_PATTERNS.items()
        if re.search(pattern, source, re.IGNORECASE)
    ]
    return surveys, surveys == ["IPEDS"]


def parse_years(text: str) -> List[int]:
    """Extract IPEDS-style start years from a label. See YEAR_RE for the shapes."""
    if not text:
        return []
    return [int(m.group(1)) for m in YEAR_RE.finditer(text)]


def _years_in_range(years: List[int], lo: int, hi: int) -> bool:
    """True when every detected year sits inside the range.

    Deliberately strict: a label like "Percent change, 1990 to 2020" spans years
    the database cannot supply, so the whole cell is unusable even though 2020
    on its own would qualify.
    """
    return bool(years) and all(lo <= y <= hi for y in years)


# --------------------------------------------------------------------------
# Index page
# --------------------------------------------------------------------------


@dataclass
class TableRef:
    number: str
    title: str
    page_url: str
    xlsx_url: str
    digest_year: str
    chapter: str


TABLE_HREF_RE = re.compile(r"/?d(\d{2})/tables/dt\d{2}_([\d.]+?)\.asp", re.IGNORECASE)


def find_chapter_tables(html: str, chapter: int) -> List[TableRef]:
    """Collect every table link nested under the given chapter of the index."""
    soup = BeautifulSoup(html, "html.parser")

    chapter_re = re.compile(rf"^Chapter\s+{chapter}\b")
    chapter_li: Optional[Tag] = None
    chapter_name = ""
    for anchor in soup.find_all("a"):
        text = clean_text(anchor)
        if chapter_re.match(text):
            chapter_li = anchor.find_parent("li")
            chapter_name = text.rstrip(".")
            break

    if chapter_li is None:
        raise RuntimeError(f"Chapter {chapter} not found on {INDEX_URL}")

    refs: List[TableRef] = []
    seen: set[str] = set()

    for anchor in chapter_li.find_all("a", href=True):
        href = anchor["href"]
        match = TABLE_HREF_RE.search(href)
        if not match:
            continue

        year_dir, number = match.group(1), match.group(2)

        # Index links carry a "?current=yes" query string, and a few are
        # absolute URLs pointing at an internal NCES host (e.g.
        # http://192.168.105.125/programs/digest/...) that is unreachable from
        # outside. Keep only the path and rebuild it against the public host.
        path = urlparse(href.split("?", 1)[0]).path.lstrip("/")
        if path.startswith("programs/digest/"):
            path = path[len("programs/digest/") :]
        page_url = BASE_URL + path

        # Table page d22/tables/dt22_301.10.asp mirrors an Excel file at
        # d22/tables/xls/tabn301.10.xlsx. Editions through 2020 publish .xls
        # instead; 2022 onward publish .xlsx.
        suffix = "xls" if int(year_dir) <= 20 else "xlsx"
        xlsx_url = f"{BASE_URL}d{year_dir}/tables/xls/tabn{number}.{suffix}"

        label = clean_text(anchor)
        # The title is the text of the surrounding <li>, minus the link label.
        list_item = anchor.find_parent("li")
        title = ""
        if list_item is not None:
            full = clean_text(list_item)
            title = full[len(label) :].strip() if full.startswith(label) else full

        if page_url in seen:
            continue
        seen.add(page_url)

        refs.append(
            TableRef(
                number=number,
                title=title,
                page_url=page_url,
                xlsx_url=xlsx_url,
                digest_year=f"20{year_dir}",
                chapter=chapter_name,
            )
        )

    return refs


# --------------------------------------------------------------------------
# Table page
# --------------------------------------------------------------------------


@dataclass
class ParsedTable:
    title: str
    columns: List[str] = field(default_factory=list)
    records: List[Dict[str, Any]] = field(default_factory=list)
    footnotes: List[str] = field(default_factory=list)


def _largest_table(soup: BeautifulSoup) -> Optional[Tag]:
    tables = soup.find_all("table")
    if not tables:
        return None
    return max(tables, key=lambda t: len(t.find_all("tr")))


def _is_column_number_row(cells: List[Tag]) -> bool:
    """Digest tables separate header from body with a row numbering the columns.

    Some tables pad that row with empty trailing cells (for flag columns that
    carry no number of their own), so ignore blanks at the end.
    """
    texts = [clean_text(c) for c in cells]
    while texts and not texts[-1]:
        texts.pop()
    if len(texts) < 2 or not all(t.isdigit() for t in texts):
        return False
    return [int(t) for t in texts] == list(range(1, len(texts) + 1))


def _build_header_grid(rows: List[Tag]) -> List[str]:
    """Expand colspan/rowspan into a grid, then join each column's levels."""
    grid: Dict[Tuple[int, int], str] = {}
    occupied: set[Tuple[int, int]] = set()
    width = 0

    for r, row in enumerate(rows):
        col = 0
        for cell in row.find_all(["th", "td"]):
            while (r, col) in occupied:
                col += 1
            colspan = int(cell.get("colspan") or 1)
            rowspan = int(cell.get("rowspan") or 1)
            text = clean_text(strip_footnote_markers(cell))
            for dr in range(rowspan):
                for dc in range(colspan):
                    occupied.add((r + dr, col + dc))
                    grid[(r + dr, col + dc)] = text
            col += colspan
            width = max(width, col)

    columns: List[str] = []
    for col in range(width):
        levels: List[str] = []
        for r in range(len(rows)):
            text = grid.get((r, col), "")
            if text and (not levels or levels[-1] != text):
                levels.append(text)
        columns.append(" | ".join(levels) if levels else f"col_{col + 1}")
    return columns


def _dedupe(columns: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for name in columns:
        if name in seen:
            seen[name] += 1
            out.append(f"{name} ({seen[name]})")
        else:
            seen[name] = 1
            out.append(name)
    return out


def parse_table_page(html: str, fallback_title: str) -> Optional[ParsedTable]:
    soup = BeautifulSoup(html, "html.parser")
    table = _largest_table(soup)
    if table is None:
        return None

    rows = table.find_all("tr")
    if len(rows) < 3:
        return None

    caption = table.find("caption")
    title = clean_text(caption) if caption else fallback_title

    # Split header from body at the column-number row.
    split_at: Optional[int] = None
    for i, row in enumerate(rows[:12]):
        cells = row.find_all(["th", "td"])
        if _is_column_number_row(cells):
            split_at = i
            break

    if split_at is None:
        # Fall back to the leading run of all-<th> rows.
        split_at = 0
        for i, row in enumerate(rows):
            cells = row.find_all(["th", "td"])
            if cells and all(c.name == "th" for c in cells):
                split_at = i + 1
            else:
                break

    header_rows = [r for r in rows[:split_at] if r.find_all(["th", "td"])]
    body_rows = rows[split_at + 1 :]

    columns = _dedupe(_build_header_grid(header_rows))
    width = len(columns)
    if width == 0:
        return None

    parsed = ParsedTable(title=title, columns=columns)
    section = ""

    for row in body_rows:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue

        text = clean_text(row)
        if not text:
            continue

        # Footnote / NOTE / SOURCE rows sit at the bottom and span the table.
        spans_table = len(cells) == 1 or int(cells[0].get("colspan") or 1) >= width
        if spans_table:
            if NOTE_PREFIX_RE.match(text) or len(cells) == 1:
                parsed.footnotes.append(text)
            continue

        label_cell = cells[0]
        level = indent_level(label_cell)
        label = clean_text(strip_footnote_markers(label_cell))
        values = [clean_text(strip_footnote_markers(c)) for c in cells[1:]]

        if not any(values):
            # A bold, value-less row is a section heading for the rows below it.
            section = label
            continue

        record: Dict[str, Any] = {
            "_row_label": label,
            "_section": section,
            "_indent": level,
            columns[0]: label,
        }
        for name, raw in zip(columns[1:], values):
            record[name] = parse_value(raw)
        parsed.records.append(record)

    _drop_empty_spacer_columns(parsed)
    parsed.footnotes.extend(_collect_note_tables(soup, table))
    return parsed


def _drop_empty_spacer_columns(parsed: ParsedTable) -> None:
    """Some tables pad rows with unlabelled spacer cells; drop them from the tail."""
    while len(parsed.columns) > 1:
        last = parsed.columns[-1]
        if not PLACEHOLDER_COL_RE.match(last):
            break
        if any(r.get(last) not in (None, "") for r in parsed.records):
            break
        parsed.columns.pop()
        for record in parsed.records:
            record.pop(last, None)


@dataclass
class YearFilterResult:
    kept: bool
    year_axis: str  # "rows", "columns", "both", "table" or "none"
    years: List[int] = field(default_factory=list)
    dropped_rows: int = 0
    dropped_columns: int = 0


def filter_by_year(parsed: ParsedTable, lo: int, hi: int) -> YearFilterResult:
    """Drop rows and columns whose labels reference years outside [lo, hi].

    Digest tables put the year axis in different places -- in the column headers,
    in the row labels, or nowhere at all for single-year snapshots (where only
    the title names the year) -- so this works cell-side rather than per table.
    """
    value_columns = parsed.columns[1:]
    column_years = {c: parse_years(c) for c in value_columns}
    # Paired with the records rather than keyed by id(), so the association
    # survives the filtering below structurally instead of by object identity.
    rows = [(r, parse_years(r["_row_label"])) for r in parsed.records]

    has_column_years = any(column_years.values())
    has_row_years = sum(1 for _, y in rows if y) > 3

    if has_column_years and has_row_years:
        axis = "both"
    elif has_column_years:
        axis = "columns"
    elif has_row_years:
        axis = "rows"
    else:
        axis = "table"

    if axis == "table":
        # No year axis: the data year lives in the title, so the table is kept or
        # dropped whole. "any" rather than "all" here because snapshot titles name
        # several data years ("Fall 2021 and 2020-21") and each one is in scope.
        title_years = parse_years(parsed.title)
        kept = any(lo <= y <= hi for y in title_years)
        return YearFilterResult(
            kept=kept,
            year_axis=axis,
            years=sorted({y for y in title_years if lo <= y <= hi}),
        )

    dropped_columns = 0
    if has_column_years:
        keep = [c for c in value_columns if not column_years[c] or _years_in_range(column_years[c], lo, hi)]
        dropped_columns = len(value_columns) - len(keep)
        if dropped_columns:
            parsed.columns = [parsed.columns[0]] + keep
            drop = set(value_columns) - set(keep)
            for record in parsed.records:
                for name in drop:
                    record.pop(name, None)

    dropped_rows = 0
    if has_row_years:
        keep = [(r, y) for r, y in rows if not y or _years_in_range(y, lo, hi)]
        dropped_rows = len(rows) - len(keep)
        rows = keep
        parsed.records = [r for r, _ in rows]

    _drop_empty_spacer_columns(parsed)

    years: set[int] = set()
    for name in parsed.columns[1:]:
        years.update(y for y in column_years.get(name, []) if lo <= y <= hi)
    for _, label_years in rows:
        years.update(y for y in label_years if lo <= y <= hi)
    if not years:
        years.update(y for y in parse_years(parsed.title) if lo <= y <= hi)

    kept = bool(parsed.records) and len(parsed.columns) > 1
    return YearFilterResult(
        kept=kept,
        year_axis=axis,
        years=sorted(years),
        dropped_rows=dropped_rows,
        dropped_columns=dropped_columns,
    )


def _collect_note_tables(soup: BeautifulSoup, data_table: Tag) -> List[str]:
    """Older Digest pages put footnotes in a separate table after the data."""
    notes: List[str] = []
    for table in soup.find_all("table"):
        if table is data_table:
            continue
        classes = table.get("class") or []
        if not any("note" in c.lower() for c in classes):
            continue
        for row in table.find_all("tr"):
            text = clean_text(row)
            if text:
                notes.append(text)
    return notes


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def build_dataset(
    out_path: Path,
    chapter: int = 3,
    limit: Optional[int] = None,
    max_rows: Optional[int] = None,
    delay: float = 0.5,
    cache_dir: Optional[Path] = None,
    verify_xlsx: bool = False,
    ipeds_only: bool = True,
    year_min: Optional[int] = DEFAULT_YEAR_MIN,
    year_max: Optional[int] = DEFAULT_YEAR_MAX,
) -> int:
    fetcher = Fetcher(cache_dir=cache_dir, delay=delay)

    print(f"Fetching index: {INDEX_URL}")
    refs = find_chapter_tables(fetcher.get_text(INDEX_URL), chapter)
    print(f"Found {len(refs)} tables in chapter {chapter}")
    if limit:
        refs = refs[:limit]

    written = 0
    failures: List[str] = []
    skipped_source: collections.Counter = collections.Counter()
    skipped_years: List[str] = []
    total_dropped_rows = 0
    total_dropped_cols = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for i, ref in enumerate(refs, 1):
            print(f"[{i}/{len(refs)}] Table {ref.number} ({ref.digest_year}) ", end="")
            try:
                html = fetcher.get_text(ref.page_url)
                parsed = parse_table_page(html, ref.title)
            except Exception as exc:  # noqa: BLE001 - keep scraping the rest
                print(f"FAILED: {exc}")
                failures.append(f"{ref.number}: {exc}")
                continue

            if parsed is None or not parsed.records:
                print("FAILED: no data rows parsed")
                failures.append(f"{ref.number}: no data rows parsed")
                continue

            surveys, is_ipeds_only = classify_sources(parsed.footnotes)
            if ipeds_only and not is_ipeds_only:
                print(f"SKIP: sources {surveys or ['unknown']}")
                skipped_source["+".join(surveys) or "unknown"] += 1
                continue

            year_result = YearFilterResult(kept=True, year_axis="none")
            if year_min is not None and year_max is not None:
                year_result = filter_by_year(parsed, year_min, year_max)
                total_dropped_rows += year_result.dropped_rows
                total_dropped_cols += year_result.dropped_columns
                if not year_result.kept:
                    print(f"SKIP: no data within {year_min}-{year_max}")
                    skipped_years.append(ref.number)
                    continue

            records = parsed.records
            if max_rows:
                records = records[:max_rows]

            xlsx_url = ref.xlsx_url
            if verify_xlsx and not fetcher.head_ok(xlsx_url):
                # Fall back to the other Excel flavour before giving up.
                other = (
                    xlsx_url[:-1] if xlsx_url.endswith(".xlsx") else xlsx_url + "x"
                )
                xlsx_url = other if fetcher.head_ok(other) else ""

            entry = {
                "eval_id": f"digest_{ref.number}",
                "table_number": ref.number,
                "chapter": ref.chapter,
                "digest_year": ref.digest_year,
                "title": parsed.title,
                "source": {
                    "index_url": INDEX_URL,
                    "page_url": ref.page_url,
                    "xlsx_url": xlsx_url,
                },
                "source_surveys": surveys,
                "years": year_result.years,
                "year_axis": year_result.year_axis,
                "dropped": {
                    "rows": year_result.dropped_rows,
                    "columns": year_result.dropped_columns,
                },
                "columns": parsed.columns,
                "row_count": len(parsed.records),
                "records": records,
                "footnotes": parsed.footnotes,
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            written += 1
            print(f"OK  {len(parsed.records)} rows x {len(parsed.columns)} cols")

    print(f"\nWrote {written} tables to {out_path}")
    if skipped_source:
        print(f"{sum(skipped_source.values())} table(s) skipped as non-IPEDS:")
        for name, count in skipped_source.most_common():
            print(f"  - {name}: {count}")
    if skipped_years:
        print(
            f"{len(skipped_years)} table(s) skipped with no data in "
            f"{year_min}-{year_max}: {', '.join(skipped_years)}"
        )
    if total_dropped_rows or total_dropped_cols:
        print(
            f"Dropped {total_dropped_rows} out-of-range row(s) and "
            f"{total_dropped_cols} out-of-range column(s) from retained tables"
        )
    if failures:
        print(f"{len(failures)} table(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
    return written


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapter", type=int, default=3,
                        help="Digest chapter number (3 = Postsecondary Education)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N tables")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Truncate each table's records to N rows")
    parser.add_argument("--out", type=Path, default=Path("eval/digest_tables.jsonl"))
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Minimum seconds between requests")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="Cache fetched pages here to avoid refetching")
    parser.add_argument("--verify-xlsx", action="store_true",
                        help="HEAD-check each Excel URL (adds a request per table)")
    parser.add_argument("--year-min", type=int, default=DEFAULT_YEAR_MIN,
                        help="Earliest IPEDS year the database covers")
    parser.add_argument("--year-max", type=int, default=DEFAULT_YEAR_MAX,
                        help="Latest IPEDS year the database covers")
    parser.add_argument("--all-sources", action="store_true",
                        help="Keep tables sourced from surveys other than IPEDS")
    parser.add_argument("--no-year-filter", action="store_true",
                        help="Keep rows and columns outside the year range")
    args = parser.parse_args(list(argv) if argv is not None else None)

    written = build_dataset(
        out_path=args.out,
        chapter=args.chapter,
        limit=args.limit,
        max_rows=args.max_rows,
        delay=args.delay,
        cache_dir=args.cache_dir,
        verify_xlsx=args.verify_xlsx,
        ipeds_only=not args.all_sources,
        year_min=None if args.no_year_filter else args.year_min,
        year_max=None if args.no_year_filter else args.year_max,
    )
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
