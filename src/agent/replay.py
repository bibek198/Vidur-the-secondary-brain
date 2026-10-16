"""Replay mode: recorded answers, so the demo works with no API key (DEMO_REPLAY=1 or no key).

Answers are recorded with `record(out)` (eval/tool_choice.py --record does this for the example
questions) into data/seed/replay/answers.jsonl, keyed by the normalized question and the graph.
Replayed answers are marked `replayed: true` and keep their original time, path and Cypher.
The graph itself, the tools and the live feed are always real; only the LLM step is replayed.
"""
import difflib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "data" / "seed" / "replay" / "answers.jsonl"


def key(question: str, graph_name: str) -> str:
    return f"{graph_name}|{' '.join(re.sub(r'[^a-z0-9.-]+', ' ', question.lower()).split())}"


def _load() -> dict:
    if not ANSWERS.exists():
        return {}
    return {r["key"]: r for r in map(json.loads, ANSWERS.open())}


def record(out: dict) -> None:
    rows = _load()
    k = key(out["question"], out["graph"])
    rows[k] = {**out, "key": k}
    ANSWERS.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows.values()))


def recorded_questions(graph_name: str) -> list[str]:
    return [r["question"] for r in _load().values() if r["graph"] == graph_name]


def serve(question: str, graph_name: str) -> dict:
    rows = _load()
    hit = rows.get(key(question, graph_name))
    if hit:
        return {**hit, "replayed": True}
    known = recorded_questions(graph_name)
    closest = difflib.get_close_matches(question, known, n=3, cutoff=0.0)
    return {"question": question, "answer": "Live questions are off in replay mode (no API key or DEMO_REPLAY=1). "
            "Try one of the recorded questions.", "replayed": True, "found": False, "closest_recorded": closest,
            "graph": graph_name, "citations": [], "sources": [], "tool_calls": [], "path": {"nodes": [], "edges": []},
            "cypher_trace": [], "as_of": "current", "stale_flags": [], "guard": "ok", "cost_usd": 0.0}
