"""Web app: the API for the UI, the GitHub webhook and the live feed.

  GET  /                        the page (web/static)
  POST /api/ask                 {question, graph, mode: graph|baseline|both, as_of?} -> both answers, each with
                                its own status (one failing never hides the other)
  GET  /api/examples            example questions (eval/questions.jsonl)
  GET  /api/atlas?graph_name=   the whole-graph skeleton for the Atlas view (src/atlas.py), cached 5 min
  POST /api/act/{name}          draft_decision_brief | recommend_reviewers | watch (not on the frozen graph)
  POST /api/demo/replay-next    camera fallback: the next real post-cutoff status change, demo graph only
  GET  /api/stream?graph=       Server-Sent Events: live feed notices, "answer outdated", watch alerts
  GET  /api/health?graph=       freshness (last event, lag), stream length, pending, dead letters
  GET  /api/questions/{id}?graph=   a stored question and the events that made its answer outdated
  POST /api/webhook/github      sandbox repo webhook (HMAC SHA-256 verified) -> events on the demo graph

The webhook turns sandbox activity into the same Event kinds as the poller and git diff:
  issues (opened/closed) -> thread events; issue_comment (created/edited) -> comment events;
  push touching keps/**/kep.yaml -> diff of the file before/after the push (shared diff_kep());
  the two versions come from the GitHub contents API.
The worker (python -m src.realtime.worker --graph brain_demo) applies them.
Run: .venv/bin/uvicorn web.app:app --port 8000
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, quote_plus

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from src.brain import config
from src.brain.db import graph
from src.realtime import stream
from src.realtime.events import Event, now_iso

DEMO_GRAPH = os.getenv("DEMO_GRAPH", "brain_demo")
_KEP = re.compile(r"\bKEP[- ]?(\d{1,5})\b", re.I)

STATIC = Path(__file__).resolve().parent / "static"
app = FastAPI(title="Decision Brain")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


GRAPHS = [g for g in os.getenv("GRAPHS", "brain_live,brain_demo,brain_eval").split(",") if g]
FROZEN = {"brain_eval"}   # never written by the API: no memory, no actions, no replayed changes
ACTIONS = {"draft_decision_brief", "recommend_reviewers", "watch"}
assert DEMO_GRAPH not in FROZEN, "the sandbox webhook must never write to the frozen evaluation graph"


def checked_graph(name: str | None) -> str:
    name = name or (GRAPHS[0] if GRAPHS else config.GRAPH_NAME)
    if name not in GRAPHS:
        raise HTTPException(400, f"unknown graph {name}")
    return name


def writable(name: str) -> bool:
    return name not in FROZEN


class Ask(BaseModel):
    question: str
    graph: str | None = None
    mode: str = "graph"            # graph | baseline | both
    as_of: str | None = None       # a release (v1.30) or a date (2024-05-01); separate from the question text


def _run_mode(mode: str, body: Ask, graph_name: str) -> dict:
    """One mode, isolated: any exception becomes {status: "error"} so the other mode's answer survives."""
    from src.agent.loop import ask
    from src.baseline import answer_baseline
    try:
        if mode == "graph":
            out = ask(body.question, graph_name=graph_name, remember=writable(graph_name), as_of=body.as_of)
        else:
            out = answer_baseline(body.question, graph_name, as_of=body.as_of)
        return {"status": "ok", **out}
    except Exception as exc:  # noqa: BLE001 - reported to the UI, never hides the other mode
        return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "mode": mode}


# Public deployment (PUBLIC_MODE=1): recorded answers first, then a per-IP hourly limit and a hard daily
# spend cap for live LLM questions. The graph, the Cypher tools and the live feed are never limited.
PUBLIC = os.getenv("PUBLIC_MODE") == "1"
LIVE_PER_IP_HOUR = int(os.getenv("LIVE_PER_IP_HOUR", "10"))
SPEND_CAP_USD = float(os.getenv("SPEND_CAP_USD", "1.00"))


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _recorded(body: Ask, graph_name: str, mode: str) -> dict | None:
    from src.agent import replay
    hit = replay._load().get(replay.key(body.question, graph_name, mode, body.as_of))
    return {"status": "ok", **hit, "replayed": True} if hit else None


def _live_allowed(ip: str) -> str | None:
    """None if a live question is allowed, else the reason it is not."""
    conn = stream.r()
    day, hour = time.strftime("%Y%m%d"), time.strftime("%Y%m%d%H")
    if float(conn.get(f"brain:spend:{day}") or 0) >= SPEND_CAP_USD:
        return "the daily budget for live questions is used up"
    n = conn.incr(f"brain:ip:{ip}:{hour}")
    conn.expire(f"brain:ip:{ip}:{hour}", 3600)
    if n > LIVE_PER_IP_HOUR:
        return f"the limit of {LIVE_PER_IP_HOUR} live questions per hour is reached"
    return None


@app.post("/api/ask")
async def ask_endpoint(body: Ask, request: Request):
    """Contract: {question_id, request, graph: {status, ...}, baseline: {status, ...}}; each mode fails on its own."""
    if not body.question.strip() or len(body.question) > 500:
        raise HTTPException(400, "question must be 1-500 characters")
    if body.mode not in ("graph", "baseline", "both"):
        raise HTTPException(400, "mode must be graph, baseline or both")
    graph_name = checked_graph(body.graph)
    modes = ["graph", "baseline"] if body.mode == "both" else [body.mode]
    if PUBLIC:
        recorded = {m: _recorded(body, graph_name, m) for m in modes}
        if all(recorded.values()):
            return _assemble(body, graph_name, modes, [recorded[m] for m in modes])
        blocked = _live_allowed(_client_ip(request))
        if blocked:
            msg = {"status": "error", "error": f"Live answers are paused: {blocked}. Try an example question "
                   "(recorded answers) or the Frozen graph."}
            return _assemble(body, graph_name, modes, [recorded[m] or msg for m in modes])
    results = await asyncio.gather(*[asyncio.to_thread(_run_mode, m, body, graph_name) for m in modes])
    if PUBLIC:
        spent = sum(r.get("cost_usd", 0) or 0 for r in results)
        stream.r().incrbyfloat(f"brain:spend:{time.strftime('%Y%m%d')}", spent)
    return _assemble(body, graph_name, modes, results)


def _assemble(body: Ask, graph_name: str, modes: list[str], results: list[dict]) -> dict:
    out = {"request": {"question": body.question, "graph": graph_name, "as_of": body.as_of, "mode": body.mode},
           "graph": {"status": "skipped"}, "baseline": {"status": "skipped"}}
    for m, r in zip(modes, results):
        out[m] = r
    out["question_id"] = out["graph"].get("question_id") or out["baseline"].get("question_id")
    return out


@app.get("/api/examples")
def examples():
    rows = [json.loads(line) for line in (Path(__file__).resolve().parents[1] / "eval" / "questions.jsonl").open()]
    return [{"id": r["id"], "category": r["category"], "question": r["question"]} for r in rows]


ATLAS_TTL_S = 300
_atlas_cache: dict[str, tuple[float, str | None, dict]] = {}


@app.get("/api/atlas")
def atlas(graph_name: str | None = None):
    """Cached per graph for 5 min, and rebuilt at once when the real-time engine has applied a newer change."""
    from src.atlas import build_atlas
    graph_name = checked_graph(graph_name)
    g = graph(graph_name)
    applied = g.query("OPTIONAL MATCH (m:Meta {key:'freshness'}) RETURN m.last_applied_at").result_set[0][0]
    hit = _atlas_cache.get(graph_name)
    if hit and hit[1] == applied and time.time() - hit[0] < ATLAS_TTL_S:
        return hit[2]
    out = build_atlas(g)
    _atlas_cache[graph_name] = (time.time(), applied, out)
    return out


@app.post("/api/act/{name}")
def act(name: str, body: dict, graph_name: str | None = None):
    from src.tools.registry import call_tool
    if name not in ACTIONS:
        raise HTTPException(404, f"unknown action {name}")
    graph_name = checked_graph(graph_name)
    if not writable(graph_name):
        raise HTTPException(403, f"{graph_name} is the frozen evaluation graph: actions are disabled there")
    try:
        out = call_tool(name, body, graph_name=graph_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out.pop("path", None)
    return out


@app.post("/api/demo/replay-next")
def replay_next(graph_name: str | None = None):
    """Camera fallback: put the next real post-cutoff status change on the demo graph's stream (KEP-2699's
    withdrawal first, the demo scenario; then the others in time order). Deterministic event ids: replaying
    an already applied change is a no-op."""
    from src.realtime.inject import feed_events
    graph_name = checked_graph(graph_name or DEMO_GRAPH)
    if graph_name != DEMO_GRAPH:
        raise HTTPException(403, "replayed changes only go to the demo graph")
    g = graph(graph_name)
    cutoff = g.query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff_ts").result_set[0][0]
    changes = [e for e in feed_events(cutoff) if e["kind"] == "kep_status_changed"]
    changes.sort(key=lambda e: (e["entity_key"] != "KEP-2699", e["occurred_at"]))
    pos = g.query("MERGE (m:Meta {key:'demo_replay'}) RETURN coalesce(m.pos, 0)").result_set[0][0]
    if pos >= len(changes):
        return {"published": None, "remaining": 0, "note": "all real post-cutoff status changes were replayed"}
    ev = changes[pos]
    stream.publish(ev, graph_name)
    g.query("MATCH (m:Meta {key:'demo_replay'}) SET m.pos = $p", {"p": pos + 1})
    return {"published": {"kind": ev["kind"], "kep": ev["entity_key"], "from": ev["payload"].get("from"),
                          "to": ev["payload"].get("to"), "url": ev["payload"].get("url"), "occurred_at": ev["occurred_at"]},
            "remaining": len(changes) - pos - 1}


@app.get("/api/health")
def health(graph_name: str | None = None):
    name = checked_graph(graph_name)
    g = graph(name)
    rows = g.query("OPTIONAL MATCH (m:Meta {key:'freshness'}) OPTIONAL MATCH (s:Meta {key:'snapshot'}) "
                   "RETURN m, s").result_set
    fresh, snap = rows[0] if rows else (None, None)
    f = fresh.properties if fresh else {}
    counts = g.query("MATCH (n) RETURN count(n)").result_set[0][0], g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return {"graph": name, "graphs": GRAPHS, "frozen": not writable(name), "now": now_iso(),
            "replay_mode": config.DEMO_REPLAY, "public_mode": PUBLIC, "live_per_ip_hour": LIVE_PER_IP_HOUR,
            # three different moments: when it happened, when the brain received it, when it was written
            "last_change": f.get("last_event_at"), "last_observed": f.get("last_observed_at"),
            "last_applied": f.get("last_applied_at"), "lag_ms": f.get("lag_ms"), "events_applied": f.get("events"),
            "snapshot_cutoff": snap.properties.get("cutoff") if snap else None,
            "nodes": counts[0], "edges": counts[1], "stream": stream.stats(name)}


@app.get("/api/questions/{qid}")
def question(qid: str, graph_name: str | None = None):
    rows = graph(checked_graph(graph_name)).query(
        """MATCH (q:Question {id:$id}) OPTIONAL MATCH (q)-[:OUTDATED_BY]->(e:Event)
           RETURN q.text, q.answer, q.asked_at, collect({summary: e.summary, at: e.occurred_at, url: e.url})""",
        {"id": qid}).result_set
    if not rows:
        raise HTTPException(404, "unknown question")
    text, answer, asked_at, events = rows[0]
    events = [e for e in events if e["summary"]]
    return {"id": qid, "question": text, "answer": answer, "asked_at": asked_at, "outdated": bool(events),
            "outdated_by": events}


@app.get("/api/stream")
async def live_feed(request: Request, graph_name: str | None = None):
    import redis.asyncio as aioredis
    graph_name = checked_graph(graph_name)

    async def events():
        conn = aioredis.Redis(host=config.FALKORDB_HOST, port=config.FALKORDB_PORT,
                              password=config.FALKORDB_PASSWORD, decode_responses=True)
        ps = conn.pubsub()
        await ps.subscribe(stream.notify_channel(graph_name))
        try:
            while not await request.is_disconnected():
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=15)
                if msg:
                    yield {"event": "notice", "data": msg["data"]}
                else:
                    yield {"event": "ping", "data": now_iso()}
        finally:
            await ps.unsubscribe()
            await conn.aclose()

    return EventSourceResponse(events())


# ---------- GitHub webhook (sandbox repo) ----------

def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def verified_payload(secret: str, body: bytes, header: str | None) -> dict | None:
    """The webhook payload if GitHub's signature is valid, else None. Accepts both content types GitHub can send:
    application/json (body = JSON), and form-encoded (body = "payload=<url-encoded JSON>"). smee.io re-sends a
    form-encoded delivery as JSON {"payload": "<JSON string>"}; the signed form body is rebuilt exactly
    ("payload=" + quote_plus(...)), so the signature is still checked over the bytes GitHub signed."""
    if verify_signature(secret, body, header):
        if body.startswith(b"payload="):
            return json.loads(parse_qs(body.decode())["payload"][0])
        return json.loads(body)
    try:
        wrapped = json.loads(body)
    except ValueError:
        return None
    if isinstance(wrapped, dict) and set(wrapped) == {"payload"} and isinstance(wrapped["payload"], str):
        form = ("payload=" + quote_plus(wrapped["payload"])).encode()
        if verify_signature(secret, form, header):
            return json.loads(wrapped["payload"])
    return None


def _keps_in(*texts) -> list[str]:
    return sorted({f"KEP-{int(n)}" for t in texts for n in _KEP.findall(t or "")})


def webhook_events(kind: str, p: dict, observed: str) -> list[Event]:
    """GitHub webhook payload -> Events (pure function, tested without a network)."""
    repo = p.get("repository", {}).get("name", "sandbox")
    if kind == "issue_comment" and p.get("action") in ("created", "edited"):
        c, issue = p["comment"], p["issue"]
        label = "PR" if issue.get("pull_request") else "Ticket"
        payload = {"kind": "issue", "thread": f"{repo}#{issue['number']}", "thread_label": label,
                   "thread_number": issue["number"], "thread_url": issue["html_url"], "thread_title": issue["title"],
                   "author": (c["user"]["login"] or "").lower(), "body": (c.get("body") or "")[:2000],
                   "url": c["html_url"], "mentions": _keps_in(c.get("body"), issue.get("title"))}
        kind_out = "comment_added" if p["action"] == "created" else "comment_edited"
        when = c["created_at"] if p["action"] == "created" else c["updated_at"]
        return [Event("webhook", kind_out, f"comment:{c['id']}", when, payload, observed)]
    if kind == "issues" and p.get("action") in ("opened", "closed", "reopened"):
        it = p["issue"]
        keps = _keps_in(it.get("title"))
        row = {"id": f"{repo}#{it['number']}", "number": it["number"], "title": it["title"], "state": it["state"],
               "author": (it["user"]["login"] or "").lower(), "created_at": it["created_at"], "url": it["html_url"],
               "body": (it.get("body") or "")[:4000], "kep": keps[0] if keps else None, "label": "Ticket"}
        if p["action"] == "closed":
            return [Event("webhook", "thread_closed", row["id"], it["closed_at"], row, observed)]
        return [Event("webhook", "thread_opened", row["id"], it["created_at"], row, observed)]
    return []


def push_events(p: dict, observed: str, fetch=None) -> list[Event]:
    """A push that changed keps/**/kep.yaml: diff the file before and after the push."""
    from src.normalize.history import diff_kep, parse_kep_text
    fetch = fetch or (lambda sha, path: _raw(p["repository"]["full_name"], sha, path))
    paths = {f for c in p.get("commits", []) for f in c.get("added", []) + c.get("modified", [])
             if re.match(r"keps/.+/kep\.yaml$", f)}
    when = (p.get("head_commit") or {}).get("timestamp") or observed
    src = {"commit": p.get("after"), "pr": None, "url": (p.get("head_commit") or {}).get("url")}
    events = []
    for path in sorted(paths):
        rec = parse_kep_text(fetch(p["after"], path), path)
        prev = parse_kep_text(fetch(p["before"], path), path) if set(p.get("before", "0")) != {"0"} else None
        if rec:
            events += [Event("webhook", e.kind, e.entity_key, e.occurred_at, e.payload, observed)
                       for e in diff_kep(rec["id"], prev, rec, when, src, observed)]
    return events


def _raw(full_name: str, sha: str, path: str) -> str | None:
    """A file at a commit, through the contents API (raw.githubusercontent.com took 75 s from here; the API 0.4 s)."""
    headers = {"Accept": "application/vnd.github.raw"}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    r = httpx.get(f"https://api.github.com/repos/{full_name}/contents/{path}", params={"ref": sha},
                  headers=headers, timeout=10)
    return r.text if r.status_code == 200 else None


@app.post("/api/webhook/github")
async def github_webhook(request: Request):
    body = await request.body()
    payload = verified_payload(os.getenv("WEBHOOK_SECRET", ""), body, request.headers.get("X-Hub-Signature-256"))
    if payload is None:
        if os.getenv("WEBHOOK_DEBUG"):   # local debugging only: keep the rejected request (data/raw is gitignored)
            Path("data/raw/logs/webhook_rejected.json").write_text(json.dumps(
                {"headers": dict(request.headers), "body": body.decode("utf-8", "replace")}))
        raise HTTPException(401, "bad or missing signature")
    kind, observed = request.headers.get("X-GitHub-Event", ""), now_iso(ms=True)
    if kind == "ping":
        return {"ok": True, "pong": True}
    if kind == "push":
        events = await asyncio.to_thread(push_events, payload, observed)
    else:
        events = webhook_events(kind, payload, observed)
    for ev in events:
        stream.publish(ev, DEMO_GRAPH)
    return {"ok": True, "graph": DEMO_GRAPH, "events": [f"{e.kind} {e.entity_key}" for e in events]}
