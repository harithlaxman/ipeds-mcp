"""Turn the scraped Digest tables into an eval set of questions and answers.

Reads `eval/digest_tables.jsonl` (produced by get_dataset.py) and writes
`eval/eval_set.jsonl`: ~500 natural-language questions, each with an answer taken
straight from the published Digest figure.

Two question types:

  * `lookup` -- one cell. "How many male students were enrolled at public
    degree-granting 4-year institutions in fall 2021?" -> 3,906,914.
  * `rank`   -- a group of sibling rows, asked as multiple choice. "Which of the
    following had the highest ...?" The options are listed in the question, so
    the answer does not depend on the agent guessing our category set.

Answers are never generated. They are read out of the table, and every record
carries the coordinates it came from so it can be re-derived (see --check).

The wording is paraphrased by Azure OpenAI from a structured descriptor. The
model is given the cell's *coordinates only* and never its value, so a leaked
answer is impossible by construction rather than by instruction. Pass --no-llm
to build the whole set from templates with no API access.

Two rules keep the set honest:

  * a row is only usable if (section, ancestors, row label) identifies it
    uniquely within its table -- 27% of Digest rows repeat a block of labels
    with no section heading to tell the copies apart;
  * a `rank` question is only kept if the top two options differ by more than
    twice the grading tolerance, so the ranking cannot flip inside the slack we
    already allow on numbers.

Config (from environment / a local .env), same as postprocess.py:
    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT

Usage:
    uv run python eval/make_questions.py --no-llm --limit 20
    uv run python eval/make_questions.py
    uv run python eval/make_questions.py --check
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# get_azure_client lives in the repo root, one level up from eval/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The year grammar and the missing-data vocabulary are properties of the Digest
# corpus, decided by the scraper that writes it. Importing them keeps the two
# halves of that contract from drifting: get_dataset.filter_by_year decides which
# years survive, and this module decides how they are worded.
from get_dataset import MISSING_MARKERS, YEAR_RE, parse_years  # noqa: E402

DEFAULT_IN = Path("eval/digest_tables.jsonl")
DEFAULT_OUT = Path("eval/eval_set.jsonl")

# An agent querying IPEDS correctly still will not reproduce Digest figures
# exactly: the Digest publishes provisional releases and applies its own universe
# filters (measured -0.03% on table 301.10 with the right filters, +0.45% with
# naive ones). Numeric answers are therefore graded within 1%.
TOLERANCE = 0.01
# A ranking must not be decidable by that slack. If the leader beats the runner-up
# by gap g and each value may drift +/-1%, the worst case closes the gap by ~2% of
# magnitude, so only g > 2*TOLERANCE survives.
RANK_MARGIN = 2 * TOLERANCE

QUOTAS = {"lookup": 400, "rank": 100}
# No single table may supply more than this, so a 12,000-cell table does not
# crowd out the rest of the corpus.
PER_TABLE_CAP = 5
# Candidates kept per type per table before selection. The draw happens over the
# cells themselves, so a 12,000-cell table costs ten Candidate objects, not
# twelve thousand built and thrown away. This leaves ~6x what we need.
CANDIDATES_PER_TABLE = 10

MIN_RANK_OPTIONS = 3
MAX_RANK_OPTIONS = 5

# How much of a question's distinguishing wording a paraphrase must keep. Below
# this it is ambiguous about which cell it is asking for, and gets rejected.
QUALIFIER_THRESHOLD = 0.7
# Attempts at a usable paraphrase before falling back to the template.
PARAPHRASE_ATTEMPTS = 2

# Rows that aggregate their siblings would win any "which is largest" question
# trivially, so they are not offered as ranking options. State tables spell the
# aggregate 'United States' rather than 'Total', which is just as unfair to ask.
TOTAL_ROW_RE = re.compile(
    r"^\s*(total|all\b|subtotal|united states|u\.?s\.?\b|50 states|national\b)",
    re.IGNORECASE,
)
# A label that is nothing but a year, e.g. '2019-20'. Ranking a set of these asks
# which year was highest -- a trend question, which this eval set does not cover.
YEAR_LABEL_RE = re.compile(r"^\s*(?:19|20)\d{2}(?:\s*-\s*(?:\d{2}|\d{4}))?\s*$")
# Cells carrying these mean "no value"; they are not answers. get_dataset.py
# writes the markers, so it owns the vocabulary -- an entry added there must not
# have to be added here too, or it becomes a "valid" answer. The empty string is
# a consumer-side concern and stays local.
NO_VALUE = MISSING_MARKERS | {""}

# Words too common to prove a paraphrase kept a qualifier.
STOPWORDS = {
    "and", "the", "for", "with", "from", "that", "this", "was", "were", "are",
    "of", "in", "by", "to", "or", "a", "an", "as", "at", "on", "its",
}

SYSTEM_PROMPT = (
    "You turn a structured description of one cell in an NCES Digest of Education "
    "Statistics table into a single natural question. Preserve EVERY qualifier the "
    "descriptor gives (level of institution, control, sex, race, year, and so on) -- "
    "dropping one makes the question ambiguous. Do not invent numbers. Do not ask for "
    "anything the descriptor does not name. Do not mention table numbers. Output the "
    "question and nothing else."
)


# --------------------------------------------------------------------------- #
# Stage A: index cells
# --------------------------------------------------------------------------- #


def row_ancestors(records: Sequence[dict]) -> List[Tuple[str, ...]]:
    """Resolve each row's ancestor chain from its indentation.

    Digest tables encode row hierarchy as leading non-breaking spaces, which
    get_dataset.py counted into `_indent`. A stack walk turns that back into a
    chain: pop while the top is at least as deep as the current row, and what
    remains is the row's ancestry.

    The Digest indents grand-total rows *more* than the rows they summarize
    (301.10 opens each section with 'Total' at _indent 2 above rows at 0). That
    resolves correctly here anyway, because the stack is empty at that point.
    """
    out: List[Tuple[str, ...]] = []
    stack: List[Tuple[int, str]] = []
    for rec in records:
        indent = rec.get("_indent") or 0
        while stack and stack[-1][0] >= indent:
            stack.pop()
        out.append(tuple(label for _, label in stack))
        stack.append((indent, rec.get("_row_label") or ""))
    return out


@dataclass
class Cell:
    table: dict
    row_index: int
    col_index: int
    section: str
    ancestors: Tuple[str, ...]
    row_label: str
    column: str
    value: Any

    @property
    def is_number(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)


def usable_value(value: Any) -> bool:
    """True when a cell holds an answer rather than a missing-data marker."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and value.strip() not in NO_VALUE


def index_cells(table: dict) -> List[Cell]:
    """Return every answerable cell whose row is uniquely identifiable.

    A question points at a cell by (section, ancestors, row label, column). Rows
    whose key is not unique inside their table are dropped outright: 27% of the
    corpus repeats a block of row labels once per category with no section
    heading to distinguish the copies, and a question built on one of those has
    more than one correct answer. Dropping them is cheap -- ~110k cells across
    166 tables survive, and we need 500.
    """
    records = table["records"]
    chains = row_ancestors(records)
    keys = [
        (rec.get("_section") or "", chain, rec.get("_row_label") or "")
        for rec, chain in zip(records, chains)
    ]
    counts = collections.Counter(keys)

    cells: List[Cell] = []
    for row_index, (rec, key) in enumerate(zip(records, keys)):
        section, ancestors, row_label = key
        if not row_label or counts[key] > 1:
            continue
        for col_index, column in enumerate(table["columns"]):
            if col_index == 0:  # the stub column repeats the row label
                continue
            value = rec.get(column)
            if not usable_value(value):
                continue
            cells.append(
                Cell(table, row_index, col_index, section, ancestors,
                     row_label, column, value)
            )
    return cells


# --------------------------------------------------------------------------- #
# Labels, years, units
# --------------------------------------------------------------------------- #


def cell_years(cell: Cell) -> List[int]:
    """Years the cell refers to, most specific label first."""
    for text in (cell.column, cell.row_label, cell.section, " ".join(cell.ancestors)):
        years = parse_years(text)
        if years:
            return sorted(set(years))
    return sorted(set(cell.table.get("years") or []))


def topic(table: dict) -> str:
    """The table's subject, without its 'Table 301.10.' prefix or trailing years."""
    title = re.sub(r"^Table\s+[\d.]+\.\s*", "", table["title"])
    # Digest titles end in ': Fall 2021 and 2020-21'; the year is carried separately.
    return title.rsplit(":", 1)[0].strip() if ":" in title else title.strip()


def unhyphenate(text: str) -> str:
    """Repair words the Digest broke across a column edge ('Non- profit').

    237 headers carry these. The space is closed up but the hyphen is kept, which
    is exactly right for the common compounds ('Non-profit', 'For-profit') and
    merely inelegant for the rest ('insti-tutions'); dropping the hyphen instead
    would turn 'For- profit' into 'Forprofit'. Telling the two apart needs a word
    list, which is not worth it for a cosmetic fix.
    """
    return re.sub(r"(\w)-\s+(?=[a-z])", r"\1-", text or "")


def column_path(column: str, drop_years: bool = False) -> List[str]:
    """Split a column's header hierarchy ('Private | Nonprofit') into its parts.

    With drop_years, segments that are only a year are removed: the year is stated
    separately in every question, and leaving it in produces 'under 2020 > Total
    in 2020'. A column that is *nothing but* a year yields an empty path, and the
    caller phrases the question without a column clause at all.
    """
    parts = [unhyphenate(p.strip()) for p in column.split("|") if p.strip()]
    if drop_years:
        return [p for p in parts if not YEAR_LABEL_RE.match(p)]
    return parts


def year_label(column: str, years: Sequence[int]) -> str:
    """How to name the year in a question.

    Prefers the column's own wording ('2012-13') over the bare start year, since
    the Digest's academic-year labels are what a reader recognizes.
    """
    for part in reversed([p.strip() for p in (column or "").split("|")]):
        if YEAR_LABEL_RE.match(part):
            return part
    return "-".join(str(y) for y in years[:2])


def row_path(cell: Cell) -> str:
    return " > ".join([*cell.ancestors, cell.row_label])


def column_context(cell: Cell) -> Tuple[List[int], str, str]:
    """The years a cell covers, how to name them, and its column clause.

    Both question types need all three, and they must agree: the year is dropped
    from the column path precisely because it is stated separately.
    """
    years = cell_years(cell)
    year_text = year_label(cell.column, years)
    col_text = " > ".join(column_path(cell.column, drop_years=bool(year_text)))
    return years, year_text, col_text


def infer_unit(cell: Cell) -> str:
    text = f"{cell.column} {cell.table['title']}".lower()
    if "percent" in text or "%" in text:
        return "percent"
    if "dollar" in text or "salary" in text or "tuition" in text:
        return "dollars"
    if "ratio" in text:
        return "ratio"
    return "count"


# --------------------------------------------------------------------------- #
# Stage B: candidates
# --------------------------------------------------------------------------- #


@dataclass
class Candidate:
    qid: str
    qtype: str
    table: dict
    answer: Any
    answer_type: str
    unit: str
    years: List[int]
    descriptor: str
    template: str
    provenance: dict
    required_tokens: List[str]
    margin: Optional[float] = None
    options: List[str] = field(default_factory=list)


def _describe(lines: Sequence[Tuple[str, Any]]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in lines if v not in (None, "", []))


def lookup_candidate(cell: Cell) -> Candidate:
    table = cell.table
    subject = topic(table)
    unit = infer_unit(cell)
    years, year_text, col_text = column_context(cell)
    row_text = unhyphenate(row_path(cell))

    descriptor = _describe([
        ("question_type", "lookup"),
        ("table", subject),
        ("section", cell.section),
        ("row", row_text),
        ("column", col_text),
        ("year", year_text),
        ("unit", unit),
    ])

    where = f" ({cell.section})" if cell.section else ""
    template = (
        f"In NCES data on {subject.lower()}{where}, what figure is reported "
        f"for {row_text}"
        + (f" under {col_text}" if col_text else "")
        + (f" in {year_text}?" if year_text else "?")
    )

    return Candidate(
        qid=f"digest_{table['table_number']}::lookup::r{cell.row_index:04d}::c{cell.col_index:02d}",
        qtype="lookup",
        table=table,
        answer=cell.value,
        answer_type="number" if cell.is_number else "label",
        unit=unit,
        years=years,
        descriptor=descriptor,
        template=template,
        provenance={
            "cells": [{
                "section": cell.section,
                "ancestors": list(cell.ancestors),
                "row_label": cell.row_label,
                "column": cell.column,
                "value": cell.value,
            }]
        },
        required_tokens=distinctive_tokens(row_text) + distinctive_tokens(col_text),
    )


def rank_groups(cells: Sequence[Cell]) -> Dict[Tuple[int, str, Tuple[str, ...]], List[Cell]]:
    """Sibling rows that share a column, section and ancestry -- one per ranking."""
    groups: Dict[Tuple[int, str, Tuple[str, ...]], List[Cell]] = collections.defaultdict(list)
    for cell in cells:
        if not cell.is_number or TOTAL_ROW_RE.match(cell.row_label):
            continue
        groups[(cell.col_index, cell.section, cell.ancestors)].append(cell)
    return groups


def rank_group(members: Sequence[Cell]) -> Optional[List[Cell]]:
    """Members sorted high to low, or None if the group cannot be ranked fairly.

    Both the generator and --check go through here. Splitting the rules between
    the two would let the verifier quietly accept a group the generator would
    now reject, which is exactly the drift the check exists to catch.
    """
    if len(members) < MIN_RANK_OPTIONS:
        return None
    # Rows labelled only by year make this "which year was highest", i.e. a
    # trend question. Out of scope for this eval set.
    if any(YEAR_LABEL_RE.match(c.row_label) for c in members):
        return None
    ranked = sorted(members, key=lambda c: -c.value)
    if ranked[0].value <= 0:
        return None
    if (ranked[0].value - ranked[1].value) / abs(ranked[0].value) <= RANK_MARGIN:
        return None
    return ranked


def rank_candidates(table: dict, cells: Sequence[Cell], rng: random.Random) -> List[Candidate]:
    """Multiple-choice 'which is largest' questions over sibling rows.

    Options are listed in the question rather than left implicit: asked open-ended,
    'which category was largest' depends on the agent reconstructing exactly our
    category set, and a defensible answer at a different granularity would be
    marked wrong for the wrong reason.
    """
    out: List[Candidate] = []
    for (col_index, section, ancestors), members in rank_groups(cells).items():
        ranked = rank_group(members)
        if ranked is None:
            continue
        top = ranked[0]
        margin = (top.value - ranked[1].value) / abs(top.value)

        # Shuffle so the answer is not always the first option offered.
        shown = ranked[:MAX_RANK_OPTIONS]
        rng.shuffle(shown)
        options = [unhyphenate(c.row_label) for c in shown]

        subject = topic(table)
        unit = infer_unit(top)
        column = top.column
        years, year_text, col_text = column_context(top)

        descriptor = _describe([
            ("question_type", "rank (multiple choice, ask which option is highest)"),
            ("table", subject),
            ("section", section),
            ("group", " > ".join(ancestors) if ancestors else None),
            ("column", col_text),
            ("year", year_text),
            ("unit", unit),
            ("options", "; ".join(options)),
        ])
        where = f" ({section})" if section else ""
        measure = col_text or unit.replace("count", "figure")
        template = (
            f"In NCES data on {subject.lower()}{where}, which of the following "
            f"had the highest {measure}"
            + (f" in {year_text}" if year_text else "")
            + f": {', '.join(options[:-1])}, or {options[-1]}?"
        )

        slug = re.sub(r"[^a-z0-9]+", "-", (section or "all").lower()).strip("-")[:24]
        anc_slug = re.sub(r"[^a-z0-9]+", "-", ("-".join(ancestors)).lower()).strip("-")[:24]
        out.append(Candidate(
            qid=f"digest_{table['table_number']}::rank::c{col_index:02d}::{slug or 'all'}"
                + (f"::{anc_slug}" if anc_slug else ""),
            qtype="rank",
            table=table,
            # Must match one of `options` verbatim, so it gets the same cleanup.
            answer=unhyphenate(top.row_label),
            answer_type="label",
            unit=unit,
            years=years,
            descriptor=descriptor,
            template=template,
            provenance={
                "column": column,
                "section": section,
                "ancestors": list(ancestors),
                "candidates": [[c.row_label, c.value] for c in ranked[:MAX_RANK_OPTIONS]],
            },
            required_tokens=[t for opt in options for t in distinctive_tokens(opt)[:2]],
            margin=round(margin, 4),
            options=options,
        ))
    return out


def build_candidates(tables: Sequence[dict], rng: random.Random) -> List[Candidate]:
    """Bounded generation: sample a few candidates per type per table, not all."""
    candidates: List[Candidate] = []
    for table in tables:
        cells = index_cells(table)
        if not cells:
            continue
        # Draw order matters: rank_candidates consumes `rng` while shuffling its
        # options, so it has to run before the two samples, and the lookup sample
        # before the rank one. Sampling the cells rather than finished Candidates
        # draws the same indices from the same state -- a given --seed still picks
        # the same questions -- without building 12,000 objects to discard 11,990.
        ranks = rank_candidates(table, cells, rng)
        picked = rng.sample(cells, min(CANDIDATES_PER_TABLE, len(cells)))
        candidates.extend(lookup_candidate(cell) for cell in picked)
        candidates.extend(rng.sample(ranks, min(CANDIDATES_PER_TABLE, len(ranks))))
    return candidates


# --------------------------------------------------------------------------- #
# Stage C: stratified sample
# --------------------------------------------------------------------------- #


def select(candidates: Sequence[Candidate], rng: random.Random,
           quotas: Dict[str, int], cap: int) -> List[Candidate]:
    """Fill each type's quota, never taking more than `cap` from one table."""
    chosen: List[Candidate] = []
    per_table: collections.Counter = collections.Counter()
    for qtype, quota in quotas.items():
        pool = [c for c in candidates if c.qtype == qtype]
        rng.shuffle(pool)
        taken = 0
        for cand in pool:
            if taken >= quota:
                break
            table_number = cand.table["table_number"]
            if per_table[table_number] >= cap:
                continue
            chosen.append(cand)
            per_table[table_number] += 1
            taken += 1
    return chosen


# --------------------------------------------------------------------------- #
# Stages D & E: paraphrase, then check it kept the question intact
# --------------------------------------------------------------------------- #


def distinctive_tokens(text: str) -> List[str]:
    """Words specific enough that losing one would change the question."""
    words = re.split(r"[^A-Za-z0-9\-]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


def _normalize(text: str) -> str:
    return re.sub(r"[,\s]+", " ", (text or "").lower())


def leaks_answer(question: str, answer: Any, years: Sequence[int] = ()) -> bool:
    """True if the question states its own answer.

    Compares whole numeric tokens by value rather than searching a digits-only
    string: the latter reports a leak for every small answer, since the '0' of an
    answer of 0 appears inside '2021'. Comparing values rather than their text
    also means no formatting choice can hide a leak. Years the question is
    entitled to mention are excluded, so a question about 2021 whose answer
    happens to be 2021 is not flagged.
    """
    if not isinstance(answer, (int, float)) or isinstance(answer, bool):
        return False
    allowed = {float(y) for y in years}
    for token in re.findall(r"-?\d[\d,]*(?:\.\d+)?", question):
        try:
            value = float(token.replace(",", ""))
        except ValueError:  # a lone '-' or a stray separator
            continue
        if value not in allowed and value == float(answer):
            return True
    return False


def qualifiers_kept(question: str, tokens: Sequence[str], years: Sequence[int],
                    threshold: float = QUALIFIER_THRESHOLD) -> bool:
    """True when the paraphrase still pins down which cell it is asking about.

    Matching is prefix-based so ordinary rewording survives ('Males' -> 'male',
    'institutions' -> 'institution'), and a few tokens may go missing before a
    question is rejected -- an LLM legitimately compresses 'Degree-granting
    institutions > Public' into 'public degree-granting institutions'.

    Tokens are deduplicated first: rank questions repeat words across their
    options, and counting a word once per occurrence would let a paraphrase that
    dropped several distinct qualifiers still clear the threshold.

    The year is not negotiable in the same way. Dropped, it leaves a question
    that half the corpus answers, so it is required outright rather than folded
    into the same ratio.
    """
    haystack = _normalize(question)
    wanted = list(dict.fromkeys(tokens))
    if wanted:
        kept = sum(
            1 for t in wanted
            if (t[:-1] if len(t) > 3 and t.endswith("s") else t) in haystack
        )
        if kept / len(wanted) < threshold:
            return False
    return not years or any(str(y) in haystack for y in years)


def paraphrase(client, deployment: str, descriptor: str) -> str:
    """Reword one descriptor.

    No temperature is sent: the newer Azure deployments reject anything but their
    default, and determinism is not what keeps this honest anyway -- the descriptor
    is stored on every record, and Stage E re-checks whatever wording comes back.
    """
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": descriptor},
        ],
    )
    content = resp.choices[0].message.content
    return (content or "").strip().strip('"')


class ParaphraseFailed(RuntimeError):
    """The model could not be reached -- a config problem, not a bad question."""


def word_question(cand: Candidate, client, deployment: str) -> Tuple[str, str]:
    """Return (question, phrasing), falling back to the template if the model's
    wording does not survive Stage E.

    An API *error* is not a fallback case: it means the whole run would silently
    produce 500 templated questions off a bad endpoint or api-version. Those
    propagate so the caller can stop.
    """
    if client is None:
        return cand.template, "template"
    for _ in range(PARAPHRASE_ATTEMPTS):
        try:
            question = paraphrase(client, deployment, cand.descriptor)
        except Exception as exc:  # noqa: BLE001 - re-raised as ParaphraseFailed
            raise ParaphraseFailed(str(exc)) from exc
        if not question:
            continue
        if leaks_answer(question, cand.answer, cand.years):
            continue
        if not qualifiers_kept(question, cand.required_tokens, cand.years):
            continue
        return question, "llm"
    return cand.template, "template"


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def to_record(cand: Candidate, question: str, phrasing: str) -> dict:
    table = cand.table
    record = {
        "id": cand.qid,
        "question": question,
        "answer": cand.answer,
        "answer_type": cand.answer_type,
        "tolerance": TOLERANCE if cand.answer_type == "number" else 0,
        "question_type": cand.qtype,
        "unit": cand.unit,
        "years": cand.years,
        "phrasing": phrasing,
        "descriptor": cand.descriptor,
        "provenance": {
            "table_number": table["table_number"],
            "digest_year": table.get("digest_year"),
            "title": table["title"],
            "page_url": (table.get("source") or {}).get("page_url"),
            **cand.provenance,
        },
    }
    if cand.margin is not None:
        record["margin"] = cand.margin
    if cand.options:
        record["options"] = cand.options
    return record


def load_tables(path: Path) -> List[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_existing(path: Path) -> Dict[str, dict]:
    """Records already written, so an interrupted run resumes instead of restarting."""
    if not path.exists():
        return {}
    return {rec["id"]: rec for rec in load_tables(path)}


def build(in_path: Path, out_path: Path, seed: int, limit: Optional[int],
          use_llm: bool, deployment: Optional[str], concurrency: int = 1) -> int:
    tables = load_tables(in_path)
    rng = random.Random(seed)
    candidates = build_candidates(tables, rng)

    quotas = dict(QUOTAS)
    if limit:
        scale = limit / sum(quotas.values())
        quotas = {k: max(1, round(v * scale)) for k, v in quotas.items()}
    chosen = select(candidates, rng, quotas, PER_TABLE_CAP)

    pool_counts = collections.Counter(c.qtype for c in candidates)
    print(f"candidate pool: {dict(pool_counts)}  ({len(tables)} tables read)")

    client = None
    if use_llm:
        from postprocess import get_azure_client

        client = get_azure_client()

    existing = load_existing(out_path)
    if existing:
        print(f"resuming: {len(existing)} record(s) already in {out_path}")

    records: List[dict] = []
    phrasing_counts: collections.Counter = collections.Counter()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    todo: List[Candidate] = []
    for cand in chosen:
        done = existing.get(cand.qid)
        if done is None:
            todo.append(cand)
        else:
            records.append(done)
            phrasing_counts[done.get("phrasing", "?")] += 1

    # Append as we go so a rate limit or Ctrl-C keeps everything already earned.
    lock = threading.Lock()
    with out_path.open("a", encoding="utf-8") as fh:

        def run(cand: Candidate) -> dict:
            question, phrasing = word_question(cand, client, deployment or "")
            record = to_record(cand, question, phrasing)
            with lock:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                records.append(record)
                phrasing_counts[phrasing] += 1
            return record

        try:
            if client is None or concurrency <= 1:
                for cand in todo:
                    run(cand)
            else:
                # The wording calls are independent and each takes seconds, so the
                # run is entirely latency-bound without this.
                from tqdm import tqdm

                # Not a `with` block: its __exit__ waits for the whole queue, so
                # a bad api-version discovered on question 5 would still bill the
                # other 495 before the error surfaced.
                pool = ThreadPoolExecutor(max_workers=concurrency)
                try:
                    futures = [pool.submit(run, cand) for cand in todo]
                    for future in tqdm(as_completed(futures), total=len(futures),
                                       desc="questions"):
                        future.result()
                finally:
                    pool.shutdown(wait=True, cancel_futures=True)
        except ParaphraseFailed as exc:
            raise SystemExit(
                f"Azure OpenAI call failed: {exc}\n"
                "Check AZURE_OPENAI_ENDPOINT / _KEY / _DEPLOYMENT, and note that "
                "AZURE_OPENAI_API_VERSION must be an Azure API version such as "
                "2024-10-21 -- a model version date (e.g. 2025-08-07) returns 404. "
                f"Records written so far are kept in {out_path}; re-running resumes. "
                "Use --no-llm to build the set from templates instead."
            ) from exc

    by_type = collections.Counter(r["question_type"] for r in records)
    by_table = collections.Counter(r["provenance"]["table_number"] for r in records)
    print(f"\nWrote {len(records)} questions to {out_path}")
    print(f"  by type    : {dict(by_type)}")
    print(f"  by phrasing: {dict(phrasing_counts)}")
    print(f"  tables      : {len(by_table)} distinct, "
          f"max {max(by_table.values()) if by_table else 0} per table")
    return len(records)


# --------------------------------------------------------------------------- #
# --check: re-derive every answer from the source tables
# --------------------------------------------------------------------------- #


def check(in_path: Path, out_path: Path) -> int:
    """Verify each question still matches the table it was built from.

    This is the load-bearing check: it re-resolves the stored coordinates against
    digest_tables.jsonl and recomputes the answer, so a question that drifted from
    its source (or was never anchored to one) fails here rather than silently
    grading an agent against the wrong number.
    """
    tables = {t["table_number"]: t for t in load_tables(in_path)}
    records = list(load_existing(out_path).values())
    failures: List[str] = []
    # Up to PER_TABLE_CAP questions share a table, and indexing one means walking
    # every row it has.
    indexed: Dict[str, List[Cell]] = {}

    for rec in records:
        prov = rec["provenance"]
        table = tables.get(prov["table_number"])
        if table is None:
            failures.append(f"{rec['id']}: table not in source")
            continue
        cells = indexed.setdefault(prov["table_number"], index_cells(table))

        if rec["question_type"] == "lookup":
            want = prov["cells"][0]
            hits = [
                c for c in cells
                if c.section == want["section"]
                and list(c.ancestors) == want["ancestors"]
                and c.row_label == want["row_label"]
                and c.column == want["column"]
            ]
            if len(hits) != 1:
                failures.append(f"{rec['id']}: {len(hits)} rows match its coordinates")
            elif hits[0].value != rec["answer"]:
                failures.append(f"{rec['id']}: {hits[0].value!r} != {rec['answer']!r}")
        else:
            members = [
                c for c in cells
                if c.column == prov["column"]
                and c.section == prov["section"]
                and list(c.ancestors) == prov["ancestors"]
                and c.is_number
                and not TOTAL_ROW_RE.match(c.row_label)
            ]
            # Re-applies every eligibility rule, not just the ones this question
            # happened to be built with: a group that would no longer be offered
            # is a question that should no longer be asked.
            ranked = rank_group(members) if members else None
            if ranked is None:
                failures.append(f"{rec['id']}: ranking group no longer qualifies")
            elif unhyphenate(ranked[0].row_label) != rec["answer"]:
                failures.append(f"{rec['id']}: top is {ranked[0].row_label!r}, "
                                f"recorded {rec['answer']!r}")
            elif rec.get("options") and rec["answer"] not in rec["options"]:
                failures.append(f"{rec['id']}: answer is not among the options offered")

        if leaks_answer(rec["question"], rec["answer"], rec.get("years") or []):
            failures.append(f"{rec['id']}: question contains its own answer")

    by_table = collections.Counter(r["provenance"]["table_number"] for r in records)
    print(f"checked {len(records)} questions over {len(by_table)} tables")
    print(f"  by type     : {dict(collections.Counter(r['question_type'] for r in records))}")
    print(f"  by phrasing : {dict(collections.Counter(r.get('phrasing') for r in records))}")
    print(f"  per-table max: {max(by_table.values()) if by_table else 0} (cap {PER_TABLE_CAP})")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures[:20]:
            print(f"  - {f}")
        return 1
    print("\nall answers re-derive from the source tables; no leaks; margins hold.")
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", type=Path, default=DEFAULT_IN,
                        help="Scraped Digest tables from get_dataset.py")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=0,
                        help="RNG seed; the same seed selects the same questions")
    parser.add_argument("--limit", type=int, default=None,
                        help="Build a smaller set, keeping the type proportions")
    parser.add_argument("--no-llm", action="store_true",
                        help="Word questions from templates, with no API calls")
    parser.add_argument("--deployment", default=None,
                        help="Azure OpenAI deployment (defaults to $AZURE_OPENAI_DEPLOYMENT)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Parallel paraphrase calls; the run is latency-bound")
    parser.add_argument("--api-version", default=None,
                        help="Azure API version, e.g. 2024-10-21 (overrides "
                             "$AZURE_OPENAI_API_VERSION)")
    parser.add_argument("--check", action="store_true",
                        help="Re-derive every answer from the source tables and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check:
        return check(args.in_path, args.out)

    deployment = args.deployment
    if not args.no_llm:
        import os

        from dotenv import load_dotenv

        load_dotenv()
        if args.api_version:
            # get_azure_client() reads this from the environment, so set it here
            # rather than duplicating the client builder.
            os.environ["AZURE_OPENAI_API_VERSION"] = args.api_version
        deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        if not deployment:
            raise SystemExit(
                "AZURE_OPENAI_DEPLOYMENT is not set. Add it to a .env file (see "
                ".env.example), pass --deployment, or use --no-llm."
            )

    written = build(args.in_path, args.out, args.seed, args.limit,
                    use_llm=not args.no_llm, deployment=deployment,
                    concurrency=args.concurrency)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
