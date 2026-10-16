"""Check every extracted quote against its source and write data/seed/decisions.jsonl.

A quote passes when, after lowercasing, dropping markdown marks and collapsing whitespace, it is
a substring of the source it cites (the design doc excerpt or one comment) and has at least 6 words.
Failed quotes are dropped. A decision is `verified` only when it keeps at least one quote and
none failed. Supersession claims are kept only with a passing quote and an existing KEP; they are
written to eval/supersession_review.csv for a person to confirm before they count as verified.

Usage: python -m src.extract.validate          (prints the pass rates; writes decisions.jsonl)
"""
import csv
import json
import re
from pathlib import Path

from src.brain.db import graph
from src.extract.prompt import build_input, cutoff_commit
from src.extract.run import CACHE

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "seed" / "decisions.jsonl"
REVIEW = ROOT / "eval" / "supersession_review.csv"
_MARKS = str.maketrans({c: " " for c in "`*_>#|"} | {"‘": "'", "’": "'", "“": '"', "”": '"'})


def norm(text: str) -> str:
    return " ".join((text or "").lower().translate(_MARKS).split())


def quote_ok(quote: str, sources: dict, ref: str) -> bool:
    return ref in sources and len(quote.split()) >= 6 and norm(quote) in norm(sources[ref])


def _items(value) -> list[dict]:
    """A list field of the tool output; the model occasionally omits one or sends it as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [v for v in value or [] if isinstance(v, dict)]


def validate_one(row: dict, inp: dict, known_keps: set) -> dict:
    out = {**row["output"], **{f: _items(row["output"].get(f)) for f in ("evidence", "alternatives", "supersession_claims")}}
    evidence = [e | {"ok": quote_ok(e["quote"], inp["sources"], e["ref"])} for e in out["evidence"] if "ref" in e and "quote" in e]
    claims = [c | {"ok": quote_ok(c["quote"], inp["sources"], c["ref"]) and c["kep"] in known_keps
                   and c["kep"] != row["kep"]} for c in out["supersession_claims"] if {"kep", "relation", "ref", "quote"} <= set(c)]
    kept = [e for e in evidence if e["ok"]]
    return {
        "id": f"dec:{row['kep']}", "kep": row["kep"], "outcome": inp["status"],
        "summary": out.get("summary"), "rationale": out.get("rationale"), "outcome_reason": out.get("outcome_reason"),
        "alternatives": [f"{a['option']} — {a['why_not']}" for a in out["alternatives"] if {"option", "why_not"} <= set(a)],
        "evidence": [{"ref": e["ref"], "quote": e["quote"]} for e in kept],
        "supersession_claims": [{k: c[k] for k in ("kep", "relation", "ref", "quote")} for c in claims if c["ok"]],
        "confidence": out.get("confidence"), "verified": bool(kept) and len(kept) == len(evidence),
        "evidence_total": len(evidence), "evidence_passed": len(kept),
        "claims_total": len(claims), "claims_passed": sum(c["ok"] for c in claims),
        "model": row["model"], "input_hash": row["input_hash"], "extracted_at": row["extracted_at"],
    }


def main() -> None:
    g = graph("brain_eval")
    commit = cutoff_commit(g.query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff").result_set[0][0])
    known = {r[0] for r in g.query("MATCH (k:KEP) RETURN k.id").result_set}
    decisions, stale, empty = [], [], []
    for p in sorted(CACHE.glob("KEP-*.json"), key=lambda p: int(p.stem.split("-")[1])):
        row = json.loads(p.read_text())
        if not row["output"]:
            empty.append(row["kep"])
            continue
        inp = build_input(row["kep"], commit=commit)
        if inp is None or inp["input_hash"] != row["input_hash"]:
            stale.append(row["kep"])
            continue
        decisions.append(validate_one(row, inp, known))
    OUT.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in decisions))

    ev_t = sum(d["evidence_total"] for d in decisions)
    ev_p = sum(d["evidence_passed"] for d in decisions)
    print(f"{len(decisions)} decisions -> {OUT.relative_to(ROOT)}; skipped: {len(stale)} stale input, {len(empty)} empty")
    print(f"quotes passed: {ev_p}/{ev_t} ({100 * ev_p / max(ev_t, 1):.1f}%); "
          f"verified decisions: {sum(d['verified'] for d in decisions)}/{len(decisions)}; "
          f"confidence: { {c: sum(d['confidence'] == c for d in decisions) for c in ('high', 'medium', 'low')} }")

    # LLM-proposed supersessions that kep.yaml does not already record: a person must confirm them
    existing = {(r[0], r[1]) for r in g.query(
        "MATCH (a:KEP)-[s:SUPERSEDES]->(b:KEP) WHERE s.source = 'yaml' RETURN a.id, b.id").result_set}
    previous = {}
    if REVIEW.exists():
        previous = {(r["newer"], r["older"]): r for r in csv.DictReader(REVIEW.open())}
    rows = []
    for d in decisions:
        for c in d["supersession_claims"]:
            newer, older = (d["kep"], c["kep"]) if c["relation"] == "replaces" else (c["kep"], d["kep"])
            if (newer, older) in existing or any(r["newer"] == newer and r["older"] == older for r in rows):
                continue
            prev = previous.get((newer, older), {})
            rows.append({"newer": newer, "older": older, "claimed_in": d["kep"], "ref": c["ref"], "quote": c["quote"],
                         "verdict": prev.get("verdict", ""), "note": prev.get("note", "")})
    with REVIEW.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["newer", "older", "claimed_in", "ref", "quote", "verdict", "note"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} new supersession claims -> {REVIEW.relative_to(ROOT)} (verdict: yes/no, filled by a person)")


if __name__ == "__main__":
    main()
