"""Read the hand-verified gold labels (eval/gold_decisions.csv) in one normalized form.

The labels are the evaluation's independent truth: code only reads them, never writes them.
- KEP ids may be written as "281" or "KEP-281"; both become "KEP-281".
- Outcomes keep the labeller's words ("removed", "superseded" are literal kep.yaml values), and
  `graph_status` maps them to the status vocabulary the graph uses (src/normalize/common.py).
"""
import csv
from pathlib import Path

GOLD = Path(__file__).resolve().parent / "gold_decisions.csv"
# labeller's outcome -> graph status (same mapping as clean_status in src/normalize/common.py)
TO_GRAPH = {"removed": "withdrawn", "superseded": "replaced", "accepted": "implemented"}


def kep_id(value: str) -> str | None:
    v = (value or "").strip().upper().removeprefix("KEP-").removeprefix("KEP")
    return f"KEP-{int(v)}" if v.isdigit() else None


def load() -> list[dict]:
    rows = []
    if not GOLD.exists():
        return rows
    for r in csv.DictReader(GOLD.open()):
        kep = kep_id(r.get("kep", ""))
        if not kep:
            continue
        outcome = (r.get("outcome") or "").strip().lower()
        rows.append({**r, "kep": kep, "outcome": outcome, "graph_status": TO_GRAPH.get(outcome, outcome),
                     "replaced_by": kep_id(r.get("replaced_by", ""))})
    return rows
