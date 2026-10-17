"""Agent loop, citation guard, replay, action tools and MCP server. No network: a scripted fake
client stands in for Claude, so these tests check our code, not the model."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from src.agent import guard, loop, replay
from src.brain import config
from src.brain.db import client as db_client
from src.tools.base import reset_trace
from src.tools.registry import TOOLS, call_tool

HAS_GRAPH = "brain_eval" in db_client().list_graphs()
needs_graph = pytest.mark.skipif(not HAS_GRAPH, reason="brain_eval not loaded")
G = "brain_eval"


# ---------- fake Claude ----------

def text(t):
    return NS(type="text", text=t)


def tool(name, **args):
    return NS(type="tool_use", id=f"tu_{name}", name=name, input=args)


class FakeClaude:
    """Returns the scripted responses in order (the last one repeats)."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return NS(content=content, usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0))


# ---------- guard (pure) ----------

SOURCES = ["https://github.com/kubernetes/enhancements/issues/3063", "https://github.com/kubernetes/enhancements/pull/4904"]
WITHDRAWN = [{"tool": "trace_decision", "found": True, "data": {"kep": "KEP-3063", "status": "withdrawn"}}]


def test_guard_accepts_a_clean_answer():
    assert guard.check("KEP-3063 was withdrawn and is not adopted [1][2].", SOURCES, WITHDRAWN) == []


def test_guard_catches_bad_citations_and_foreign_urls():
    problems = guard.check("It was withdrawn [3]. See https://example.com/blog.", SOURCES, WITHDRAWN)
    assert any("[3]" in p for p in problems) and any("example.com" in p for p in problems)
    assert "example.com" not in guard.repair("It was withdrawn [3]. See https://example.com/blog.", SOURCES)
    assert "[3]" not in guard.repair("It was withdrawn [3] [1].", SOURCES)


def test_guard_catches_not_adopted_called_implemented():
    assert guard.check("KEP-3063 was implemented in 1.32 [1].", SOURCES, WITHDRAWN)
    assert guard.check("KEP-3063 was not implemented; it was withdrawn [1].", SOURCES, WITHDRAWN) == []


def test_guard_requires_not_found_wording_when_nothing_matched():
    results = [{"tool": "trace_decision", "found": False, "data": {}}]
    assert guard.check("The quantum scheduler was approved by alice.", [], results)
    assert guard.check("There is no such decision in the graph.", [], results) == []


# ---------- loop (fake Claude, real tools) ----------

@needs_graph
def test_loop_runs_tools_and_numbers_sources():
    fake = FakeClaude([tool("who_owns", target="kubelet")], [text("SIG Node owns the kubelet [1].")])
    out = loop.ask("Who owns the kubelet?", graph_name=G, remember=False, client=fake)
    assert out["answer"] == "SIG Node owns the kubelet [1]." and out["guard"] == "ok"
    assert [c["name"] for c in out["tool_calls"]] == ["who_owns"]
    assert out["citations"][0]["n"] == 1 and out["citations"][0]["url"].startswith("https://")
    assert out["path"]["nodes"] and out["cypher_trace"]
    tool_result = fake.calls[1]["messages"][2]["content"][0]["content"]                 # user turn with the result
    assert '"sources": [{"n": 1' in tool_result and "cypher" not in tool_result   # the model never sees Cypher


@needs_graph
def test_loop_retries_then_repairs_a_bad_answer():
    bad_cite = [text("KEP-3063 was withdrawn [99]. https://example.com")]
    fake = FakeClaude([tool("trace_decision", kep_or_query="KEP-3063")], bad_cite, bad_cite)
    out = loop.ask("Is KEP-3063 implemented?", graph_name=G, remember=False, client=fake)
    assert out["guard"] == "repaired" and "example.com" not in out["answer"] and "[99]" not in out["answer"]
    assert "Do not call tools" in fake.calls[2]["messages"][-1]["content"]          # the retry got the problems

    bad = [text("KEP-3063 was implemented [1].")]                                   # a false claim survives the retry
    fake = FakeClaude([tool("trace_decision", kep_or_query="KEP-3063")], bad, bad)
    out = loop.ask("Is KEP-3063 implemented?", graph_name=G, remember=False, client=fake)
    assert out["guard"] == "fallback" and "withdrawn" in out["answer"] and "not adopted" in out["answer"]
    assert "was implemented" not in out["answer"]

    good = [text("No. KEP-3063 was withdrawn and not adopted [1].")]
    fake = FakeClaude([tool("trace_decision", kep_or_query="KEP-3063")], bad, good)
    out = loop.ask("Is KEP-3063 implemented?", graph_name=G, remember=False, client=fake)
    assert out["guard"] == "retried" and out["guard_problems"] == []


@needs_graph
def test_loop_stops_after_max_rounds_and_survives_bad_tools():
    fake = FakeClaude([tool("drop_graph")])
    out = loop.ask("anything", graph_name=G, remember=False, client=fake)
    assert out["rounds"] == loop.MAX_ROUNDS and "tool-call limit" in out["answer"]
    assert all(c["found"] is None for c in out["tool_calls"])                      # errors reported, not raised


@needs_graph
def test_memory_links_question_to_touched_entities():
    fake = FakeClaude([tool("who_owns", target="kubelet")], [text("SIG Node [1].")])
    out = loop.ask("Who owns the kubelet?", graph_name=G, remember=True, client=fake)
    g = db_client().select_graph(G)
    try:
        rows = g.query("MATCH (q:Question {id:$id})-[:ASKED_ABOUT]->(x) RETURN labels(x)[0]",
                       {"id": out["question_id"]}).result_set
        assert rows and {"Team"} & {r[0] for r in rows}
    finally:
        g.query("MATCH (q:Question {id:$id}) DETACH DELETE q", {"id": out["question_id"]})


# ---------- replay ----------

def test_replay_serves_recorded_answers_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "ANSWERS", tmp_path / "answers.jsonl")
    replay.record({"question": "Who owns the kubelet?", "graph": G, "answer": "SIG Node [1]."})
    monkeypatch.setattr(config, "DEMO_REPLAY", True)
    out = loop.ask("who owns the KUBELET", graph_name=G)
    assert out["replayed"] and out["answer"] == "SIG Node [1]."
    miss = loop.ask("Something never recorded", graph_name=G)
    assert miss["replayed"] and "replay mode" in miss["answer"] and miss["closest_recorded"]


# ---------- action tools (real graph; memory nodes are removed afterwards) ----------

@pytest.fixture
def clean_memory():
    yield
    db_client().select_graph(G).query("MATCH (n) WHERE n:Draft OR n:Watch OR n:Question DETACH DELETE n")


@needs_graph
def test_recommend_reviewers_excludes_authors_and_gives_reasons():
    reset_trace()
    r = call_tool("recommend_reviewers", {"kep_or_path": "KEP-753"}, graph_name=G)
    logins = [p["login"] for p in r["data"]["reviewers"]]
    assert r["found"] and logins and all(p["reasons"] for p in r["data"]["reviewers"])
    authors = {x[0] for x in db_client().select_graph(G).query(
        "MATCH (p:Person)-[:AUTHORED]->(:KEP {id:'KEP-753'}) RETURN p.login").result_set}
    assert not authors & set(logins)
    path = call_tool("recommend_reviewers", {"kep_or_path": "pkg/scheduler"}, graph_name=G)
    assert path["data"]["for"]["label"] in ("CodeArea", "Component")                 # a path is code, not a KEP


@needs_graph
def test_actions_write_once_and_cite_sources(clean_memory):
    g = db_client().select_graph(G)
    for _ in range(2):
        brief = call_tool("draft_decision_brief", {"kep": "KEP-753"}, graph_name=G)
        call_tool("watch", {"entity": "KEP-753"}, graph_name=G)
        routed = call_tool("route_question", {"question": "Can sidecars restart?", "topic": "sidecar containers"},
                           graph_name=G)
    assert brief["data"]["markdown"].startswith("# Decision brief: KEP-753") and brief["sources"]
    assert routed["data"]["ask"] and routed["sources"]
    counts = g.query("MATCH (n) WHERE n:Draft OR n:Watch OR n:Question RETURN labels(n)[0], count(n)").result_set
    assert dict(counts) == {"Draft": 1, "Watch": 1, "Question": 1}                   # MERGE: repeating changes nothing


@needs_graph
def test_actions_say_not_found_for_unknown_things(clean_memory):
    assert call_tool("watch", {"entity": "quantum teleport scheduler"}, graph_name=G)["found"] is False


# ---------- MCP ----------

def test_mcp_server_lists_every_registry_tool():
    from src.mcp_server import server
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == set(TOOLS)


@needs_graph
def test_current_kep_approvers_rank_above_code_breadth():
    r = call_tool("recommend_reviewers", {"kep_or_path": "KEP-4381"}, graph_name=G)
    approvers = {x[0] for x in db_client().select_graph(G).query(
        "MATCH (p:Person)-[a:APPROVES]->(:KEP {id:'KEP-4381'}) WHERE a.valid_to IS NULL RETURN p.login").result_set}
    ranked = [p["login"] for p in r["data"]["reviewers"]]
    top = [x for x in ranked if x in approvers]
    assert top and ranked.index(top[0]) == 0, ranked
