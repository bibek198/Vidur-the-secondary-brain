"""The vector-only baseline: the same question, the same FalkorDB, the same LLM, with the graph switched off.

It is the comparison the UI shows next to every graph answer and the ablation Phase 9 measures.
What it may use, by construction:
- ONE query: the vector index over Chunk (design-doc passages and comments), top 8 by similarity.
- ONE Haiku call: answer from those numbered passages only, citing [n] (same model and answer rules).
What it may NOT use: tools, traversal, Decision nodes, status history, intervals. A test checks that the
trace holds exactly the one vector query.

With as_of, only comment chunks dated at or before the cutoff are kept. Design-doc chunks carry no date
(they are the docs as fetched), so the answer always ends with a fixed disclosure that vector search
cannot reconstruct past status. The citation guard runs in report-only mode: the baseline answer is
never repaired, so it is a clean measurement.
"""
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from src.agent import guard
from src.agent.loop import PRICE, _add_usage, _cost
from src.brain import config
from src.brain.db import graph
from src.tools.base import reset_trace, run, trace, ts_to_date

SEED = Path(__file__).resolve().parents[1] / "data" / "seed"

TOP_K, FETCH_K = 8, 40
DISCLOSURE = ("Note: vector search cannot reconstruct past status; design-doc passages may be newer than "
              "the requested date.")

SYSTEM = """You answer questions about the Kubernetes project (its teams, people, code ownership and decisions/KEPs)
using ONLY the numbered passages given. Rules:
1. If the passages do not answer the question, say plainly that they do not contain the answer. Do not guess.
2. Cite every fact with its passage number in brackets, like [2]. Never write URLs yourself.
3. Be brief: a direct answer first, then at most 5 short bullet points."""


def cutoff(as_of: str) -> tuple[int, str]:
    """Release or date -> (epoch seconds, label), from data/seed/releases.jsonl, not from the graph, so the
    baseline's only graph query is the vector search."""
    m = re.fullmatch(r"v?(1\.\d+)(?:\.\d+)?", as_of.strip())
    if m:
        for r in map(json.loads, (SEED / "releases.jsonl").open()):
            if r["version"] == f"v{m.group(1)}":
                return r["ts"], f"release v{m.group(1)} ({r['date'][:10]})"
        raise ValueError(f"unknown release {as_of}")
    dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()), dt.strftime("%Y-%m-%d")


def retrieve(g, question: str, as_of_ts: int | None) -> list[dict]:
    from src.load.embed import embed_query
    rows = run(g, f"""CALL db.idx.vector.queryNodes('Chunk', 'embedding', {FETCH_K}, vecf32($v)) YIELD node, score
                      WHERE $as_of IS NULL OR node.at_ts IS NULL OR node.at_ts <= $as_of
                      RETURN node.id, node.kind, node.text, node.url, node.at_ts, score
                      ORDER BY score LIMIT {TOP_K}""",
               {"v": embed_query(question), "as_of": as_of_ts}, "baseline").result_set
    return [{"id": r[0], "type": "design doc passage" if r[1] == "kep" else "comment", "text": r[2], "url": r[3],
             "date": ts_to_date(r[4]), "distance": round(r[5], 3)} for r in rows]


def answer_baseline(question: str, graph_name: str | None = None, as_of: str | None = None, client=None,
                    model: str | None = None) -> dict:
    graph_name = graph_name or config.GRAPH_NAME
    if client is None and config.DEMO_REPLAY:
        from src.agent import replay
        return replay.serve(question, graph_name, mode="baseline", as_of=as_of)
    g = graph(graph_name)
    reset_trace()
    started = time.time()
    as_of_ts, as_of_label = cutoff(as_of) if as_of else (None, "current")
    passages = retrieve(g, question, as_of_ts)
    sources = list(dict.fromkeys(p["url"] for p in passages if p["url"]))
    numbered = "\n\n".join(f"[{sources.index(p['url']) + 1}] ({p['type']}{', ' + p['date'] if p['date'] else ''}) "
                           f"{p['text']}" for p in passages if p["url"])
    user = f"Question: {question}" + (f"\nAnswer as of {as_of_label}." if as_of else "") + f"\n\nPassages:\n{numbered}"
    client = client or anthropic.Anthropic()
    model = model or config.LLM_MODEL
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    msg = client.messages.create(model=model, max_tokens=800, system=SYSTEM, messages=[{"role": "user", "content": user}])
    _add_usage(usage, msg.usage)
    answer = "".join(b.text for b in msg.content if b.type == "text").strip()
    if as_of:
        answer += "\n\n" + DISCLOSURE
    # report only, never repaired; names count as known when a retrieved passage or the question contains them
    problems = guard.check(answer, sources, [{"data": passages}], question=question)
    cited = sorted({int(n) for n in guard._CITE.findall(answer) if 1 <= int(n) <= len(sources)})
    asked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "mode": "baseline", "question": question, "graph": graph_name, "model": model, "answer": answer,
        "citations": [{"n": n, "url": sources[n - 1]} for n in cited], "sources": sources,
        "evidence": [{k: p[k] for k in ("id", "type", "url", "date", "distance")} for p in passages],
        "tool_calls": [], "path": {"nodes": [], "edges": []}, "cypher_trace": list(trace()),
        "as_of_requested": as_of, "as_of_applied": [as_of_label] if as_of else [], "data_as_of": None,
        "guard": "report-only", "guard_problems": problems, "usage": usage, "cost_usd": round(_cost(usage), 5),
        "seconds": round(time.time() - started, 1), "asked_at": asked_at,
        "question_id": "b:" + uuid.uuid4().hex[:12],
        "found": bool(passages),
    }


__all__ = ["answer_baseline", "retrieve", "DISCLOSURE", "PRICE"]
