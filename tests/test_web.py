"""Phase 8: the ask contract, the vector-only baseline, temporal enforcement, isolation and the page itself.

No network: a scripted fake Claude stands in for the model (as in tests/test_agent.py). Graph-backed tests
run on brain_eval read-only, or on a throwaway copy (test_web) that is deleted afterwards.
"""
import json
import threading
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from src.agent import guard, loop, replay
from src.brain.db import client, graph
from src.realtime import stream

HAS_GRAPH = "brain_eval" in client().list_graphs()
needs_graph = pytest.mark.skipif(not HAS_GRAPH, reason="brain_eval not loaded")


def text(t):
    return NS(type="text", text=t)


def tool(name, **args):
    return NS(type="tool_use", id=f"tu_{name}", name=name, input=args)


class FakeClaude:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return NS(content=content, usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0))


def counts(name):
    g = graph(name)
    return g.query("MATCH (n) RETURN count(n)").result_set[0][0], g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]


# ---------- the vector-only baseline ----------

@needs_graph
def test_baseline_is_vector_only():
    from src.baseline import answer_baseline
    fake = FakeClaude([text("KEP-3063 was withdrawn [1].")])
    out = answer_baseline("Why was classic DRA withdrawn?", "brain_eval", client=fake)
    assert out["tool_calls"] == [] and out["mode"] == "baseline"
    assert len(out["cypher_trace"]) == 1 and "db.idx.vector.queryNodes" in out["cypher_trace"][0]["cypher"]
    assert all(s.startswith("https://") for s in out["sources"]) and out["evidence"]
    assert "tools" not in fake.calls[0]                            # the model gets no tools at all


@needs_graph
def test_baseline_as_of_filters_and_discloses():
    from src.baseline import DISCLOSURE, answer_baseline, cutoff
    ts, label = cutoff("v1.24")
    out = answer_baseline("swap memory on linux nodes", "brain_eval", as_of="v1.24", client=FakeClaude([text("x [1]")]))
    dated = [e for e in out["evidence"] if e["date"]]
    assert all(e["date"] <= label.split("(")[1][:10] for e in dated)
    assert out["answer"].endswith(DISCLOSURE) and out["as_of_requested"] == "v1.24"


@needs_graph
def test_baseline_guard_reports_without_repairing():
    from src.baseline import answer_baseline
    out = answer_baseline("sidecar containers", "brain_eval", client=FakeClaude([text("Bad cite [99]. See https://example.com")]))
    assert "[99]" in out["answer"] and "example.com" in out["answer"]       # untouched
    assert out["guard"] == "report-only" and len(out["guard_problems"]) == 2


# ---------- the temporal contract in the agent ----------

@needs_graph
def test_as_of_is_enforced_on_historical_tools_and_flagged_on_others():
    fake = FakeClaude([tool("trace_decision", kep_or_query="KEP-753"), tool("who_knows", topic="sidecar containers")],
                      [text("As of release v1.29 KEP-753 was implementable [1]; experts listed are from the current graph.")])
    out = loop.ask("Was KEP-753 adopted?", graph_name="brain_eval", remember=False, client=fake, as_of="v1.29")
    calls = {c["name"]: c["args"] for c in out["tool_calls"]}
    assert calls["trace_decision"]["as_of"] == "v1.29"             # the model omitted it; the loop added it
    assert "as_of" not in calls["who_knows"]
    tool_msgs = fake.calls[1]["messages"][2]["content"]
    assert "CURRENT graph" in tool_msgs[1]["content"]              # the non-historical tool was flagged
    assert out["as_of_requested"] == "v1.29" and out["as_of_applied"][0].startswith("trace_decision: release v1.29")
    assert "(Answer as of v1.29.)" in fake.calls[0]["messages"][0]["content"]


def test_guard_requires_current_disclosure_for_non_historical_tools():
    results = [{"tool": "who_knows", "found": True, "data": {}, "historical": False}]
    assert guard.check("Experts are alice and bob.", [], results, as_of="v1.29")
    assert guard.check("Experts in the current graph are alice and bob.", [], results, as_of="v1.29") == []
    assert guard.check("Experts are alice and bob.", [], results) == []          # no cutoff requested


# ---------- the API ----------

@pytest.fixture
def web(monkeypatch):
    from web import app as web_app
    return web_app


def test_page_and_static_are_served(web):
    tc = TestClient(web.app)
    assert tc.get("/").status_code == 200 and "Decision Brain" in tc.get("/").text
    assert tc.get("/static/app.js").status_code == 200
    assert tc.get("/api/examples").json()[0]["question"]


def test_modes_fail_independently(web, monkeypatch):
    import src.agent.loop as agent_loop
    import src.baseline as baseline

    def boom(*a, **k):
        raise RuntimeError("graph agent exploded")
    monkeypatch.setattr(agent_loop, "ask", boom)
    monkeypatch.setattr(baseline, "answer_baseline", lambda q, g, as_of=None: {"answer": "vector answer", "question_id": "b:1"})
    tc = TestClient(web.app)
    r = tc.post("/api/ask", json={"question": "q", "graph": "brain_eval", "mode": "both"}).json()
    assert r["graph"]["status"] == "error" and "exploded" in r["graph"]["error"]
    assert r["baseline"]["status"] == "ok" and r["baseline"]["answer"] == "vector answer"

    monkeypatch.setattr(agent_loop, "ask", lambda *a, **k: {"answer": "graph answer", "question_id": "q:1"})
    monkeypatch.setattr(baseline, "answer_baseline", boom)
    r = tc.post("/api/ask", json={"question": "q", "graph": "brain_eval", "mode": "both", "as_of": "v1.30"}).json()
    assert r["graph"]["status"] == "ok" and r["baseline"]["status"] == "error"
    assert r["request"]["as_of"] == "v1.30" and r["request"]["question"] == "q"     # the question is not rewritten


def test_bad_requests_are_rejected(web):
    tc = TestClient(web.app)
    assert tc.post("/api/ask", json={"question": "q", "graph": "some_other_graph"}).status_code == 400
    assert tc.post("/api/ask", json={"question": "q", "mode": "magic"}).status_code == 400
    assert tc.post("/api/act/drop_everything", json={}).status_code == 404


@needs_graph
def test_frozen_graph_is_never_written(web, monkeypatch):
    import src.agent.loop as agent_loop
    real_ask = agent_loop.ask
    seen = {}

    def spy(question, graph_name=None, remember=True, as_of=None, **k):
        seen["remember"] = remember
        return real_ask(question, graph_name=graph_name, remember=remember, as_of=as_of,
                        client=FakeClaude([tool("who_owns", target="kubelet")], [text("SIG Node [1].")]))
    monkeypatch.setattr(agent_loop, "ask", spy)
    before = counts("brain_eval")
    tc = TestClient(web.app)
    assert tc.post("/api/ask", json={"question": "Who owns the kubelet?", "graph": "brain_eval"}).json()["graph"]["status"] == "ok"
    assert seen["remember"] is False
    assert tc.post("/api/act/watch?graph_name=brain_eval", json={"entity": "KEP-753"}).status_code == 403
    assert tc.post("/api/demo/replay-next?graph_name=brain_eval").status_code == 403
    assert counts("brain_eval") == before


def test_replay_next_needs_the_demo_token_when_one_is_set(web, monkeypatch):
    monkeypatch.setattr(web, "DEMO_TOKEN", "s3cret")
    tc = TestClient(web.app)
    denied = tc.post("/api/demo/replay-next?graph_name=brain_demo")
    assert denied.status_code == 403 and "demo token" in denied.json()["detail"]
    wrong = tc.post("/api/demo/replay-next?graph_name=brain_demo", headers={"X-Demo-Token": "nope"})
    assert wrong.status_code == 403
    # the right token passes the token check (brain_eval is then refused for being frozen, so nothing is written)
    ok = tc.post("/api/demo/replay-next?graph_name=brain_eval", headers={"X-Demo-Token": "s3cret"})
    assert ok.status_code == 403 and "demo graph" in ok.json()["detail"]


# ---------- demo graph: replay-next and the outdated-answer lifecycle (throwaway copy) ----------

@pytest.fixture
def demo_copy(web, monkeypatch):
    c = client()
    name = "test_web"
    if name in c.list_graphs():
        c.connection.execute_command("GRAPH.DELETE", name)
    stream.reset(name)
    c.connection.execute_command("GRAPH.COPY", "brain_eval", name)
    monkeypatch.setattr(web, "DEMO_GRAPH", name)
    monkeypatch.setattr(web, "GRAPHS", web.GRAPHS + [name])
    yield name
    c.connection.execute_command("GRAPH.DELETE", name)
    stream.reset(name)


@pytest.mark.skipif(not HAS_GRAPH, reason="brain_eval not loaded")
@pytest.mark.slow
def test_replay_next_and_outdated_lifecycle(web, demo_copy, monkeypatch):
    from src.realtime.handlers import apply_event
    g = graph(demo_copy)
    first = loop.ask("Is KEP-2699 still planned?", graph_name=demo_copy, remember=True,
                     client=FakeClaude([tool("trace_decision", kep_or_query="KEP-2699")], [text("It is implementable [1].")]))
    tc = TestClient(web.app)
    r1 = tc.post(f"/api/demo/replay-next?graph_name={demo_copy}").json()
    assert r1["published"]["kep"] == "KEP-2699" and r1["published"]["to"] == "withdrawn"
    applied = []
    stream.consume(demo_copy, lambda ev: applied.append(apply_event(g, ev, live=True)), block_ms=100)
    assert applied == [True]
    q = tc.get(f"/api/questions/{first['question_id']}?graph_name={demo_copy}").json()
    assert q["outdated"] and "withdrawn" in q["outdated_by"][0]["summary"]
    again = loop.ask("Is KEP-2699 still planned?", graph_name=demo_copy, remember=True,
                     client=FakeClaude([tool("trace_decision", kep_or_query="KEP-2699")], [text("It was withdrawn [1].")]))
    assert again["question_id"] != first["question_id"]
    assert not tc.get(f"/api/questions/{again['question_id']}?graph_name={demo_copy}").json()["outdated"]
    before = counts(demo_copy)
    r2 = tc.post(f"/api/demo/replay-next?graph_name={demo_copy}").json()
    assert r2["published"]["kep"] != "KEP-2699"                    # the cursor moved on; KEP-2699 is not re-sent
    stream.r().delete(stream.stream_key(demo_copy))
    assert counts(demo_copy) == before


# ---------- replay recording is safe under concurrency ----------

def test_concurrent_replay_records_keep_every_row(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "ANSWERS", tmp_path / "answers.jsonl")
    threads = [threading.Thread(target=replay.record, args=({"question": f"q{i}", "graph": "g", "answer": "a",
                                                             "mode": "graph" if i % 2 else "baseline"},)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rows = [json.loads(line) for line in (tmp_path / "answers.jsonl").open()]
    assert len(rows) == 20 and {r["question"] for r in rows} == {f"q{i}" for i in range(20)}


# ---------- public deployment limits ----------

def test_public_mode_serves_recorded_then_limits_live(web, monkeypatch, tmp_path):
    import src.agent.loop as agent_loop
    monkeypatch.setattr(replay, "ANSWERS", tmp_path / "answers.jsonl")
    replay.record({"question": "Who owns the kubelet?", "graph": "brain_eval", "answer": "SIG Node [1].", "mode": "graph"})
    monkeypatch.setattr(web, "PUBLIC", True)
    monkeypatch.setattr(web, "LIVE_PER_IP_HOUR", 1)
    calls = []
    monkeypatch.setattr(agent_loop, "ask", lambda *a, **k: calls.append(1) or {"answer": "live", "question_id": "q:x", "cost_usd": 0.001})
    stream.r().delete(*stream.r().keys("brain:ip:203.0.113.9:*") or ["_none"])
    tc = TestClient(web.app)
    h = {"X-Forwarded-For": "203.0.113.9"}
    r = tc.post("/api/ask", json={"question": "who owns the KUBELET", "graph": "brain_eval"}, headers=h).json()
    assert r["graph"]["replayed"] and calls == []                                   # recorded: no live call
    assert tc.post("/api/ask", json={"question": "new question", "graph": "brain_eval"}, headers=h).json()["graph"]["answer"] == "live"
    blocked = tc.post("/api/ask", json={"question": "another one", "graph": "brain_eval"}, headers=h).json()
    assert blocked["graph"]["status"] == "error" and "per hour" in blocked["graph"]["error"] and len(calls) == 1
    stream.r().delete(*stream.r().keys("brain:ip:203.0.113.9:*") or ["_none"])
