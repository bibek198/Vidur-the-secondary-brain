"""Re-extract a decision's rationale after new discussion (the LLM part of the real-time engine).

- enqueue(): a handler marked the KEP's Decision stale; schedule it 30 s later in the sorted set
  brain:reextract:<graph>. More comments within 30 s push the time back (debounce), so a burst of
  comments costs one call.
- run_due(): for each due KEP, rebuild the extraction input from the graph. If it hashes the same
  as the stored Decision, nothing relevant changed: just clear `stale`. Otherwise call Haiku (strict
  tool, same prompt as the batch), check the quotes with validate_one(), and MERGE the new Decision.
- No API key or DEMO_REPLAY=1: the Decision stays stale (the agent says it is being re-checked).
- A spend cap (REEXTRACT_BUDGET_USD, default $0.50) stops live calls if something loops.
Live re-extractions are written to the graph only; data/seed stays the frozen evaluation input.
The worker runs this in a background thread, so an LLM call never delays applying events.

Usage (backlog after a catch-up): python -m src.realtime.reextract --graph brain_live --stale [--limit N]
"""
import os
import time

from src.brain import config
from src.brain.db import graph
from src.realtime import stream

DEBOUNCE_S = 30
MAX_AGE_S = 48 * 3600   # discussion older than this (a catch-up) is not re-extracted automatically
BUDGET_USD = float(os.getenv("REEXTRACT_BUDGET_USD", "0.50"))
PRICE_IN, PRICE_OUT = 0.10, 0.50   # Haiku 5.5, USD per million tokens (direct calls)


def queue_key(graph_name: str) -> str:
    return f"brain:reextract:{graph_name}"


def enqueue(graph_name: str, kep: str, delay_s: int = DEBOUNCE_S) -> None:
    stream.r().zadd(queue_key(graph_name), {kep: time.time() + delay_s})


def spent() -> float:
    return float(stream.r().get("brain:reextract:spent_usd") or 0)


def _readme_commit(g) -> str | None:
    """Live graphs read design docs at the latest fetched commit; frozen copies at their cutoff."""
    rows = g.query("MATCH (m:Meta {key:'snapshot'}) RETURN m.readme_commit").result_set
    return rows[0][0] if rows and rows[0][0] else None


def run_due(graph_name: str, client=None, now: float | None = None) -> list[dict]:
    from src.extract.prompt import build_input, request_params
    from src.extract.validate import validate_one
    from src.load import upserts
    conn, g = stream.r(), graph(graph_name)
    due = conn.zrangebyscore(queue_key(graph_name), 0, now or time.time())
    done = []
    for kep in due:
        conn.zrem(queue_key(graph_name), kep)
        inp = build_input(kep, graph_name=graph_name, commit=_readme_commit(g))   # None -> the graph's cutoff
        old = g.query("MATCH (d:Decision)-[:DECIDED]->(:KEP {id:$k}) RETURN d.input_hash", {"k": kep}).result_set
        if inp is None:
            continue
        if old and old[0][0] == inp["input_hash"]:
            g.query("MATCH (d:Decision)-[:DECIDED]->(:KEP {id:$k}) SET d.stale = false", {"k": kep})
            done.append({"kep": kep, "result": "unchanged"})
            continue
        if client is None and (config.DEMO_REPLAY or spent() >= BUDGET_USD):
            done.append({"kep": kep, "result": "left stale (no key, replay mode or budget reached)"})
            continue
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        msg = client.messages.create(**request_params(inp, config.LLM_MODEL))
        cost = (msg.usage.input_tokens * PRICE_IN + msg.usage.output_tokens * PRICE_OUT) / 1e6
        conn.incrbyfloat("brain:reextract:spent_usd", cost)
        tool_use = next((b for b in msg.content if b.type == "tool_use"), None)
        if tool_use is None:
            continue
        known = {r[0] for r in g.query("MATCH (k:KEP) RETURN k.id").result_set}
        row = {"kep": kep, "output": dict(tool_use.input), "model": msg.model, "input_hash": inp["input_hash"],
               "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        decision = validate_one(row, inp, known)
        g.query("MATCH (d:Decision {id:$id})-[e:EVIDENCED_BY]->() DELETE e", {"id": decision["id"]})
        upserts.decisions(g, [decision])
        stream.notify(graph_name, {"type": "rationale_updated", "kep": kep, "verified": decision["verified"],
                                   "cost_usd": round(cost, 5)})
        done.append({"kep": kep, "result": "re-extracted", "verified": decision["verified"], "cost_usd": round(cost, 5)})
    return done


def enqueue_stale(graph_name: str, limit: int | None = None) -> int:
    """Queue every stale Decision now (backlog after a catch-up), newest discussion first."""
    rows = graph(graph_name).query("""MATCH (d:Decision {stale:true})-[:DECIDED]->(k:KEP)
                                      RETURN k.id ORDER BY k.number DESC""").result_set
    keps = [r[0] for r in rows][:limit]
    for kep in keps:
        enqueue(graph_name, kep, delay_s=0)
    return len(keps)


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    name = argv[argv.index("--graph") + 1] if "--graph" in argv else "brain_live"
    if "--stale" in argv:
        print(f"queued {enqueue_stale(name, int(argv[argv.index('--limit') + 1]) if '--limit' in argv else None)}")
    print(run_due(name), f"spent so far ${spent():.3f}")
