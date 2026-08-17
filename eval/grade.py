"""Score predictions against the eval set built by make_questions.py.

Reads `eval/eval_set.jsonl` and a predictions file, and reports accuracy overall
and per question type. Predictions are JSONL, one object per line, keyed by the
question id:

    {"id": "digest_301.10::lookup::r0002::c03", "answer": 3906914}
    {"id": "digest_303.20::rank::all::c04", "answer": "New York"}

Grading follows the contract recorded on each question:

  * `answer_type: number` -- correct within the question's own `tolerance`
    (1% by default). An agent querying IPEDS correctly still cannot reproduce
    Digest figures exactly, because the Digest publishes provisional releases and
    applies its own universe filters; measured drift was -0.03% on table 301.10
    with the right filters against +0.45% with naive ones, so 1% absorbs the
    former while still failing the latter.
  * `answer_type: label` -- exact match after case, whitespace and punctuation
    normalization. A percentage tolerance is meaningless on a string.

Usage:
    uv run python eval/grade.py --predictions runs/baseline.jsonl
    uv run python eval/grade.py --predictions runs/baseline.jsonl --show-failures 10
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_EVAL_SET = Path("eval/eval_set.jsonl")


def load_jsonl(path: Path) -> List[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def as_number(value: Any) -> Optional[float]:
    """Coerce a prediction to a number, tolerating '$1,234' and '45.6%'."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[,$%\s]", "", str(value))
    try:
        return float(text)
    except ValueError:
        return None


def normalize_label(value: Any) -> str:
    """Lowercase, collapse whitespace, drop punctuation that carries no meaning."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(value).lower())
    return re.sub(r"\s+", " ", text).strip()


def grade_one(question: dict, predicted: Any) -> Tuple[bool, str]:
    """Return (correct, reason). `reason` explains a failure, or is empty."""
    expected = question["answer"]

    if question["answer_type"] == "number":
        got = as_number(predicted)
        if got is None:
            return False, f"not a number: {predicted!r}"
        want = float(expected)
        tolerance = question.get("tolerance", 0.0)
        # Relative to the published figure. A zero expected value has no relative
        # scale, so it must match exactly.
        limit = abs(want) * tolerance
        if abs(got - want) <= limit:
            return True, ""
        off = (got - want) / want if want else float("inf")
        return False, f"{got:,.6g} vs {want:,.6g} ({off:+.2%})"

    if normalize_label(predicted) == normalize_label(expected):
        return True, ""
    return False, f"{predicted!r} vs {expected!r}"


def grade(questions: List[dict], predictions: Dict[str, Any],
          show_failures: int) -> None:
    totals: collections.Counter = collections.Counter()
    correct: collections.Counter = collections.Counter()
    failures: List[str] = []
    missing = 0

    for question in questions:
        qtype = question["question_type"]
        totals[qtype] += 1
        if question["id"] not in predictions:
            missing += 1
            failures.append(f"[{qtype}] {question['id']}: no prediction")
            continue
        ok, reason = grade_one(question, predictions[question["id"]])
        if ok:
            correct[qtype] += 1
        else:
            failures.append(f"[{qtype}] {question['id']}: {reason}")

    total = sum(totals.values())
    hits = sum(correct.values())
    print(f"{hits}/{total} correct  ({hits / total:.1%})" if total else "no questions")
    for qtype in sorted(totals):
        n, c = totals[qtype], correct[qtype]
        print(f"  {qtype:<8} {c:>4}/{n:<4} {c / n:>7.1%}")
    if missing:
        # Called out separately: an unanswered question is not the same failure
        # mode as a wrong answer, and reporting them together hides a broken run.
        print(f"\n{missing} question(s) had no prediction (counted as incorrect)")

    if failures and show_failures:
        print(f"\nfirst {min(show_failures, len(failures))} of {len(failures)} failure(s):")
        for line in failures[:show_failures]:
            print(f"  - {line}")


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--predictions", type=Path, required=True,
                        help="JSONL of {'id': ..., 'answer': ...} objects")
    parser.add_argument("--show-failures", type=int, default=10,
                        help="How many failing questions to print (0 for none)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    questions = load_jsonl(args.eval_set)
    predictions = {p["id"]: p.get("answer") for p in load_jsonl(args.predictions)}
    grade(questions, predictions, args.show_failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
