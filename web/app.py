"""Web app: the API for the UI, the GitHub webhook and the live feed.

  POST /api/ask                 {question, graph?}   -> agent answer (src/agent/loop.py)
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
from pathlib import Path
from urllib.parse import parse_qs, quote_plus

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from src.brain import config
from src.brain.db import graph
from src.realtime import stream
from src.realtime.events import Event, now_iso

DEMO_GRAPH = os.getenv("DEMO_GRAPH", "brain_demo")
_KEP = re.compile(r"\bKEP[- ]?(\d{1,5})\b", re.I)

app = FastAPI(title="Decision Brain")


class Ask(BaseModel):
    question: str
    graph: str | None = None


@app.post("/api/ask")
async def ask_endpoint(body: Ask):
    from src.agent.loop import ask
    if len(body.question) > 500:
        raise HTTPException(400, "question too long")
    return await run_in_threadpool(ask, body.question, body.graph or config.GRAPH_NAME)


@app.get("/api/health")
def health(graph_name: str = DEMO_GRAPH):
    name = graph_name
    rows = graph(name).query("OPTIONAL MATCH (m:Meta {key:'freshness'}) OPTIONAL MATCH (s:Meta {key:'snapshot'}) "
                             "RETURN m, s").result_set
    fresh, snap = rows[0] if rows else (None, None)
    return {"graph": name, "now": now_iso(), "freshness": fresh.properties if fresh else None,
            "snapshot": snap.properties if snap else None, "stream": stream.stats(name)}


@app.get("/api/questions/{qid}")
def question(qid: str, graph_name: str = DEMO_GRAPH):
    rows = graph(graph_name).query(
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
async def live_feed(request: Request, graph_name: str = DEMO_GRAPH):
    import redis.asyncio as aioredis

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
