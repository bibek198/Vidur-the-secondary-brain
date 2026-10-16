"""Tool-choice eval: does the agent pick the right tool, and is it honest about made-up things?

For each question in eval/questions.jsonl (split dev or holdout) it runs the real agent with
remember=False (the eval graph stays unchanged) and records:
- tool choice: at least one expected tool was called
- lure honesty (lure questions): every tool said found=false and the answer says it is not in the graph
- guard status (ok / retried / repaired / fallback) and cost
Action questions write Draft/Question/Watch nodes; these are removed after the run.
Results go to eval/results/tool_choice_<split>.jsonl and a summary is printed.

Usage: python eval/tool_choice.py --split dev [--record] [--only A1,B2]
  --record also saves each answer for replay mode (data/seed/replay/answers.jsonl).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent import guard, replay  # noqa: E402
from src.agent.loop import ask  # noqa: E402
from src.brain.db import graph  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "eval" / "questions.jsonl"
RESULTS = ROOT / "eval" / "results"
GRAPH = "brain_eval"


def run(split: str, record: bool, only: set | None) -> None:
    qs = [q for q in map(json.loads, QUESTIONS.open()) if q["split"] == split and (not only or q["id"] in only)]
    RESULTS.mkdir(exist_ok=True)
    rows = []
    try:
        for q in qs:
            out = ask(q["question"], graph_name=GRAPH, remember=False)
            called = [c["name"] for c in out["tool_calls"]]
            row = {"id": q["id"], "category": q["category"], "question": q["question"], "called": called,
                   "args": [c["args"] for c in out["tool_calls"]],
                   "tool_ok": bool(set(called) & set(q["expected_tools"])), "guard": out["guard"],
                   "cost_usd": out["cost_usd"], "seconds": out["seconds"], "answer": out["answer"]}
            if q.get("lure"):
                said_not_found = bool(guard._NOT_FOUND.search(out["answer"]))
                row["lure_honest"] = said_not_found and not any(c["found"] for c in out["tool_calls"])
            rows.append(row)
            mark = "ok " if row["tool_ok"] else "BAD"
            print(f"{mark} {q['id']:3} {','.join(called) or '-':45} guard={out['guard']:8} ${out['cost_usd']:.4f}"
                  + (f" lure_honest={row['lure_honest']}" if q.get("lure") else ""))
            if record:
                replay.record(out)
    finally:
        graph(GRAPH).query("MATCH (n) WHERE n:Draft OR n:Watch OR n:Question DETACH DELETE n")
    path = RESULTS / f"tool_choice_{split}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    lures = [r for r in rows if "lure_honest" in r]
    print(f"\n{split}: tool choice {sum(r['tool_ok'] for r in rows)}/{len(rows)}"
          f" ({100 * sum(r['tool_ok'] for r in rows) / max(len(rows), 1):.0f}%), "
          f"lures honest {sum(r['lure_honest'] for r in lures)}/{len(lures)}, "
          f"guard not ok {sum(r['guard'] != 'ok' for r in rows)}, cost ${sum(r['cost_usd'] for r in rows):.3f} -> {path.relative_to(ROOT)}")


if __name__ == "__main__":
    args = sys.argv[1:]
    split = args[args.index("--split") + 1] if "--split" in args else "dev"
    only = set(args[args.index("--only") + 1].split(",")) if "--only" in args else None
    run(split, "--record" in args, only)
