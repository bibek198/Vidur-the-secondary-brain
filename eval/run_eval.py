"""Run the 50 evaluation questions through the graph agent and the vector-only baseline, and score both.

Both modes get the same question and the same cutoff (`as_of` for category G) on the frozen graph
brain_eval, with the same model. Scoring is by fixed, transparent rules (below), identical for both modes;
an answer that abstains on a real question counts as wrong. Raw answers, evidence and scores are saved,
and eval/hand_check.csv lists every lure (D) and supersession (E) answer plus 30% of the rest for a
person to check the rule-based verdicts.

Usage: python eval/run_eval.py [--only B1,D2] [--workers 4]
"""
import csv
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.guard import _ADOPTED, _NEGATION  # noqa: E402
from src.agent.loop import ask  # noqa: E402
from src.baseline import answer_baseline  # noqa: E402

NOT_ADOPTED = {"withdrawn", "rejected", "replaced", "deferred"}
HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "eval_questions.jsonl"
RESULTS = HERE / "results"
GRAPH = "brain_eval"
SYNONYMS = {"implemented": ["implemented", "generally available", "graduated to stable", "graduated to ga",
                            "reached ga", "reached stable"],
            "withdrawn": ["withdrawn", "withdrew"], "replaced": ["replaced", "superseded"],
            "rejected": ["rejected"], "deferred": ["deferred"], "implementable": ["implementable"],
            "provisional": ["provisional"]}
ABSTAIN = re.compile(r"not in the (knowledge )?graph|no (kep|record|such|decision|information|data|match)|"
                     r"(do|does|did)(n't| not) (contain|mention|say|exist|appear|answer|give)|can(no|')t (find|say|tell)|"
                     r"could(n't| not) find|nothing (in|about)|not (found|recorded)|has no |unknown|no evidence", re.I)


def has_status(text: str, status: str) -> bool:
    low = text.lower()
    return any(w in low for w in SYNONYMS.get(status, [status]))


def score(q: dict, answer: str) -> tuple[bool, str]:
    """(correct, reason). The same rules for both modes."""
    a = answer or ""
    low = a.lower()
    rule = q["score"]
    if rule == "abstain":
        return bool(ABSTAIN.search(a)), "must say it is not in the record"
    if rule == "false_premise":
        affirms = any(_ADOPTED.search(s) and not _NEGATION.search(s) for s in re.split(r"(?<=[.!?])\s+", a))
        negates = bool(re.search(r"\b(never|not (adopted|removed|completed|shipped|graduated|carried|implemented|ga|generally)"
                                 r"|did not|didn't|was not|wasn't|has not|hasn't)\b", low))
        ok = (has_status(a, q["expected_status"]) or negates) and not affirms
        return ok, f"must correct the premise (status {q['expected_status']} or a clear negation), no adoption claim"
    if rule == "status":
        if q["category"] == "B" and q["expected_status"] in NOT_ADOPTED:   # "was it adopted?": a clear "no" is right
            not_adopted = bool(re.search(r"\bnot (been )?adopted\b|\bwas(n't| not) adopted\b|\bnever adopted\b", low))
            return has_status(a, q["expected_status"]) or not_adopted, \
                f"must state status {q['expected_status']} or that it was not adopted"
        return has_status(a, q["expected_status"]), f"must state status {q['expected_status']}"
    if rule == "mentions_kep":
        nums = [k.split("-")[1] for k in q["expected_kep"]]
        return any(re.search(rf"\b(KEP-)?{n}\b", a) for n in nums), f"must name {' or '.join(q['expected_kep'])}"
    if rule == "mentions_sig":
        forms = {s.lower() for sig in q["expected_sig"] for s in (sig, sig.replace("-", " "))}
        return any(f in low for f in forms), f"must name {q['expected_sig']}"
    if rule == "components":
        found = [c for c in q["expected_components"] if c.lower() in low]
        return len(found) / len(q["expected_components"]) >= 0.5, f"must name half of {q['expected_components']}"
    if rule == "mentions_person":
        return any(re.search(rf"(?<![\w-]){re.escape(p)}(?![\w-])", low) for p in q["expected_people"]), \
            "must name an author, approver or reviewer"
    raise ValueError(rule)


def run_one(q: dict) -> dict:
    row = {"id": q["id"], "category": q["category"], "question": q["question"], "as_of": q.get("as_of")}
    for mode, fn in (("graph", lambda: ask(q["question"], graph_name=GRAPH, remember=False, as_of=q.get("as_of"))),
                     ("baseline", lambda: answer_baseline(q["question"], GRAPH, as_of=q.get("as_of")))):
        t0 = time.time()
        try:
            out = fn()
            ok, why = score(q, out["answer"])
            cite_bad = [p for p in out.get("guard_problems", []) if "citation" in p or "URL" in p]
            row[mode] = {"answer": out["answer"], "correct": ok, "rule": why, "abstained": bool(ABSTAIN.search(out["answer"])),
                         "citations": len(out.get("citations", [])), "citation_problems": cite_bad,
                         "guard": out.get("guard"), "tools": [c["name"] for c in out.get("tool_calls", [])],
                         "evidence": out.get("evidence", [])[:12], "cost_usd": out.get("cost_usd", 0),
                         "seconds": round(time.time() - t0, 1)}
        except Exception as exc:  # noqa: BLE001 - a failed answer is scored wrong, never dropped
            row[mode] = {"answer": "", "correct": False, "error": f"{type(exc).__name__}: {exc}",
                         "seconds": round(time.time() - t0, 1), "cost_usd": 0, "citations": 0, "citation_problems": []}
    print(f"{q['id']:3} graph={'OK ' if row['graph']['correct'] else 'BAD'} baseline={'OK ' if row['baseline']['correct'] else 'BAD'}",
          flush=True)
    return row


def summarize(rows: list[dict]) -> dict:
    out = {}
    for cat in sorted({r["category"] for r in rows}) + ["all"]:
        sel = [r for r in rows if cat == "all" or r["category"] == cat]
        out[cat] = {m: {"correct": sum(r[m]["correct"] for r in sel), "n": len(sel),
                        "cost_usd": round(sum(r[m]["cost_usd"] for r in sel), 4),
                        "median_s": sorted(r[m]["seconds"] for r in sel)[len(sel) // 2],
                        "citation_problems": sum(bool(r[m]["citation_problems"]) for r in sel)}
                    for m in ("graph", "baseline")}
    return out


def rescore(path: Path) -> None:
    """Re-apply the scoring rules to saved answers (after a rule change; no model calls)."""
    qs = {q["id"]: q for q in map(json.loads, QUESTIONS.open())}
    rows = list(map(json.loads, path.open()))
    for r in rows:
        for m in ("graph", "baseline"):
            r[m]["correct"], r[m]["rule"] = score(qs[r["id"]], r[m]["answer"]) if r[m]["answer"] else (False, "no answer")
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    summary = summarize(rows)
    path.with_name(path.stem + "_summary.json").write_text(json.dumps(summary, indent=1))
    for cat, s in summary.items():
        print(f"{cat:4} graph {s['graph']['correct']}/{s['graph']['n']}  baseline {s['baseline']['correct']}/{s['baseline']['n']}")


def main(argv: list[str]) -> None:
    if "--rescore" in argv:
        return rescore(Path(argv[argv.index("--rescore") + 1]))
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    workers = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else 4
    qs = [q for q in map(json.loads, QUESTIONS.open()) if not only or q["id"] in only]
    with ThreadPoolExecutor(workers) as pool:
        rows = sorted(pool.map(run_one, qs), key=lambda r: (r["category"], int(r["id"][1:])))
    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    (RESULTS / f"eval_{stamp}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    summary = summarize(rows)
    (RESULTS / f"eval_{stamp}_summary.json").write_text(json.dumps(summary, indent=1))
    rng = random.Random(1)
    pick = [r for r in rows if r["category"] in "DE"] + rng.sample([r for r in rows if r["category"] not in "DE"],
                                                                     k=round(0.3 * sum(r["category"] not in "DE" for r in rows)))
    with (HERE / "hand_check.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "question", "mode", "answer", "rule", "rule_verdict", "your_verdict (right/wrong)", "note"])
        for r in pick:
            for m in ("graph", "baseline"):
                w.writerow([r["id"], r["question"], m, r[m]["answer"], r[m].get("rule", ""),
                            "right" if r[m]["correct"] else "wrong", "", ""])
    print(json.dumps(summary["all"], indent=1))
    for cat, s in summary.items():
        print(f"{cat:4} graph {s['graph']['correct']}/{s['graph']['n']}  baseline {s['baseline']['correct']}/{s['baseline']['n']}")
    print(f"-> eval/results/eval_{stamp}.jsonl, eval/hand_check.csv ({len(pick)} questions x 2 modes)")


if __name__ == "__main__":
    main(sys.argv[1:])
