"""Replay mode: recorded answers, so the demo works with no API key (DEMO_REPLAY=1 or no key).

Answers are recorded with `record(out)` (eval/tool_choice.py --record does this for the example
questions) into data/seed/replay/answers.jsonl, keyed by the normalized question and the graph.
Replayed answers are marked `replayed: true` and keep their original time, path and Cypher.
The graph itself, the tools and the live feed are always real; only the LLM step is replayed.
"""
import difflib
import json
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "data" / "seed" / "replay" / "answers.jsonl"


def key(question: str, graph_name: str, mode: str = "graph", as_of: str | None = None) -> str:
    q = " ".join(re.sub(r"[^a-z0-9.-]+", " ", question.lower()).split())
    return f"{'' if mode == 'graph' else mode + '|'}{graph_name}|{q}" + (f"|as_of={as_of}" if as_of else "")


def _load() -> dict:
    if not ANSWERS.exists():
        return {}
    return {r["key"]: r for r in map(json.loads, ANSWERS.open())}


_LOCK = threading.Lock()


def record(out: dict) -> None:
    """Save one answer (graph or baseline). Locked and atomic: concurrent recorders cannot lose rows."""
    k = key(out["question"], out["graph"], out.get("mode", "graph"), out.get("as_of_requested"))
    with _LOCK:
        rows = _load()
        rows[k] = {**out, "key": k}
        ANSWERS.parent.mkdir(parents=True, exist_ok=True)
        tmp = ANSWERS.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows.values()))
        os.replace(tmp, ANSWERS)


def recorded_questions(graph_name: str) -> list[str]:
    return [r["question"] for r in _load().values() if r["graph"] == graph_name and r.get("mode", "graph") == "graph"]


def serve(question: str, graph_name: str, mode: str = "graph", as_of: str | None = None) -> dict:
    rows = _load()
    hit = rows.get(key(question, graph_name, mode, as_of))
    if hit:
        return {**hit, "replayed": True}
    known = recorded_questions(graph_name)
    closest = difflib.get_close_matches(question, known, n=3, cutoff=0.0)
    return {"question": question, "answer": "Live questions are off in replay mode (no API key or DEMO_REPLAY=1). "
            "Try one of the recorded questions.", "replayed": True, "found": False, "closest_recorded": closest,
            "graph": graph_name, "citations": [], "sources": [], "tool_calls": [], "path": {"nodes": [], "edges": []},
            "cypher_trace": [], "as_of": "current", "stale_flags": [], "guard": "ok", "cost_usd": 0.0}
