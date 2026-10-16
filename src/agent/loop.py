"""The agent: Claude picks graph tools, the tools run parameterized Cypher, Claude answers with citations.

Flow for one question:
  1. Claude (Haiku by default) sees the question and the tool catalogue (src/tools/registry.py).
  2. Each tool call runs through call_tool(); Claude sees only `found`, `data`, `notes` and the
     sources, numbered [1], [2], ... across the whole conversation. The graph path, the Cypher
     trace and stale flags stay on the server for the UI.
  3. The final answer goes through the citation guard (src/agent/guard.py): on problems Claude
     rewrites it once; if problems remain, bad citations are stripped and guard = "repaired".
  4. With remember=True the question is stored as a Question node linked to what it touched,
     so the real-time engine can flag the answer as outdated when those facts change.
The LLM never writes Cypher; it can only choose tools and fill in their arguments.
"""
import hashlib
import json
import time

import anthropic

from src.agent import guard
from src.brain import config
from src.brain.db import graph
from src.tools.base import reset_trace
from src.tools.registry import TOOLS, call_tool

MAX_ROUNDS = 6
MAX_RESULT_CHARS = 8000
# USD per million tokens for Haiku 5.5: input, output, cache read, cache write
PRICE = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}

SYSTEM = """You are Decision Brain, an assistant that answers questions about how the Kubernetes project
works as a company: its teams (SIGs), people, code ownership and decisions (KEPs), using only a knowledge graph.

Rules:
1. Answer only from tool results. Call tools first; never answer from memory. If you need a KEP id, a person
   or a component, a tool finds it.
2. If the tools return found=false, say plainly that it is not in the graph and stop. Do not guess, and do not
   describe the closest matches as if they were the answer (you may mention them as "closest matches").
3. A KEP with status withdrawn, rejected, replaced or deferred was NOT adopted. Never call it implemented.
4. If something was replaced, say what replaced it first. If a fact is marked unverified, not yet confirmed,
   stale or LLM-written with failed quotes, say so in a few words.
5. If the question is about a past time, pass as_of to the tool and state the date the answer refers to.
6. Cite every fact with its source number in brackets, like [2]. Use only the numbers given in tool results.
   Never write URLs yourself.
7. Be brief: a direct answer first, then at most 5 short bullet points. Plain language, no filler."""


def _strict_schema(schema: dict) -> dict:
    """Adapt a registry schema for strict tool use:
    - minimum/maximum are not allowed: the bounds move into the description (the tools clamp them anyway)
    - optional arguments become required but nullable: with plain optional fields, strict mode made the model
      drop them (influence never got `team`); null means "not given" and is removed before the call."""
    props, required = {}, set(schema.get("required", []))
    for name, p in schema["properties"].items():
        p = dict(p)
        if "minimum" in p or "maximum" in p:
            bounds = f"{p.pop('minimum', '')}-{p.pop('maximum', '')}"
            p["description"] = (p.get("description", "") + f" ({bounds})").strip()
        if name not in required:
            desc = (p.pop("description", "") + " Use null if not needed.").strip()
            p = {"anyOf": [p, {"type": "null"}], "description": desc}
        props[name] = p
    return {**schema, "properties": props, "required": list(schema["properties"])}


def tool_specs() -> list[dict]:
    return [{"name": name, "description": spec["description"], "input_schema": _strict_schema(spec["schema"]),
             "strict": True} for name, spec in TOOLS.items()]


class Sources:
    """Global numbering of source URLs across all tool calls in one conversation."""

    def __init__(self):
        self.urls: list[str] = []

    def number(self, urls: list[str]) -> list[dict]:
        out = []
        for u in urls:
            if u not in self.urls:
                self.urls.append(u)
            out.append({"n": self.urls.index(u) + 1, "url": u})
        return out


def model_view(result: dict, sources: Sources) -> str:
    """What Claude sees from one tool result: no path, no Cypher."""
    view = {"found": result["found"], "as_of": result["as_of"], "notes": result["notes"],
            "data": result["data"], "sources": sources.number(result["sources"])}
    if result["stale_flags"]:
        view["stale"] = result["stale_flags"]
    text = json.dumps(view, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + ' ..."(truncated)"'


def _cost(usage: dict) -> float:
    return sum(usage[k] * PRICE[k] for k in PRICE) / 1e6


def _add_usage(total: dict, u) -> None:
    total["input"] += u.input_tokens
    total["output"] += u.output_tokens
    total["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
    total["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0


def _merge_paths(results: list[dict]) -> dict:
    nodes, edges = {}, {}
    for r in results:
        for n in r["path"]["nodes"]:
            nodes.setdefault(n["id"], n)
        for e in r["path"]["edges"]:
            edges.setdefault(e["id"], e)
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def ask(question: str, graph_name: str | None = None, model: str | None = None, remember: bool = True,
        client=None) -> dict:
    """Answer one question from the graph. Returns answer, citations, tool calls, path, Cypher and cost."""
    from src.agent import replay
    if client is None and config.DEMO_REPLAY:
        return replay.serve(question, graph_name or config.GRAPH_NAME)
    client = client or anthropic.Anthropic()
    model = model or config.LLM_MODEL
    graph_name = graph_name or config.GRAPH_NAME
    reset_trace()
    started = time.time()
    sources, results, calls = Sources(), [], []
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    messages = [{"role": "user", "content": question}]
    params = {"model": model, "max_tokens": 1200, "tools": tool_specs(), "output_config": {"effort": "low"},
              "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]}

    answer, rounds = "", 0
    while rounds < MAX_ROUNDS:
        rounds += 1
        msg = client.messages.create(messages=messages, **params)
        _add_usage(usage, msg.usage)
        messages.append({"role": "assistant", "content": msg.content})
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            answer = "".join(b.text for b in msg.content if b.type == "text").strip()
            break
        replies = []
        for tu in tool_uses:
            t0 = time.time()
            try:
                args = {k: v for k, v in dict(tu.input).items() if v is not None}  # null = not given
                result = call_tool(tu.name, args, graph_name=graph_name)
                results.append(result)
                content, is_error = model_view(result, sources), False
                found = result["found"]
            except Exception as exc:  # bad arguments or unknown tool: tell the model, keep going
                content, is_error, found = f"Tool error: {exc}", True, None
            calls.append({"name": tu.name, "args": {k: v for k, v in dict(tu.input).items() if v is not None}, "found": found,
                          "ms": round((time.time() - t0) * 1000)})
            replies.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": replies})
    else:
        answer = "I could not finish within the tool-call limit. Please ask a narrower question."

    status = "ok"
    problems = guard.check(answer, sources.urls, results)
    if problems:
        messages.append({"role": "user", "content": "Your answer breaks these rules:\n- " + "\n- ".join(problems)
                         + "\nRewrite the answer following the rules. Do not call tools."})
        msg = client.messages.create(messages=messages, tool_choice={"type": "none"}, **params)
        _add_usage(usage, msg.usage)
        answer = "".join(b.text for b in msg.content if b.type == "text").strip()
        problems = guard.check(answer, sources.urls, results)
        status = "retried"
        if problems:
            answer, status = guard.repair(answer, sources.urls), "repaired"
            if any(guard.is_claim_problem(p) for p in problems):
                answer, status = guard.fallback(results, sources.urls), "fallback"

    cited = sorted({int(n) for n in guard._CITE.findall(answer) if 1 <= int(n) <= len(sources.urls)})
    as_of = sorted({r["as_of"] for r in results if r["as_of"] != "current"})
    out = {
        "question": question, "answer": answer, "graph": graph_name, "model": model,
        "citations": [{"n": n, "url": sources.urls[n - 1]} for n in cited], "sources": sources.urls,
        "tool_calls": calls, "path": _merge_paths(results),
        "cypher_trace": [c for r in results for c in r["cypher_trace"]],
        "as_of": as_of[0] if len(as_of) == 1 else ("current" if not as_of else ", ".join(as_of)),
        "stale_flags": [f for r in results for f in r["stale_flags"]],
        "found": any(r["found"] for r in results) if results else False,
        "guard": status, "guard_problems": problems, "rounds": rounds, "usage": usage,
        "cost_usd": round(_cost(usage), 5), "seconds": round(time.time() - started, 1),
        "asked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out["question_id"] = "q:" + hashlib.sha256(f"{question}|{out['asked_at']}".encode()).hexdigest()[:12]
    if remember:
        store_question(out)
    return out


def store_question(out: dict) -> None:
    """Memory: a Question node linked to the entities its answer touched (Phase 7 flags it when they change)."""
    g = graph(out["graph"])
    g.query("""MERGE (q:Question {id:$id})
               SET q.text = $text, q.answer = $answer, q.asked_at = $at, q.as_of = $as_of, q.model = $model""",
            {"id": out["question_id"], "text": out["question"], "answer": out["answer"], "at": out["asked_at"],
             "as_of": out["as_of"], "model": out["model"]})
    touched = [n["id"] for n in out["path"]["nodes"] if n["label"] in ("KEP", "Team", "Person", "Component")][:25]
    if touched:
        g.query("""MATCH (q:Question {id:$id}) UNWIND $ids AS nid
                   MATCH (x) WHERE id(x) = nid MERGE (q)-[:ASKED_ABOUT]->(x)""",
                {"id": out["question_id"], "ids": touched})
