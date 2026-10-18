"""Document and thread tools (search, read_thread, read_kep) on the frozen graph brain_eval. No LLM involved."""
import pytest

from src.brain.db import client
from src.extract.prompt import ENH
from src.tools.base import reset_trace
from src.tools.registry import call_tool

needs_graph = pytest.mark.skipif("brain_eval" not in client().list_graphs(), reason="brain_eval not loaded")
needs_clone = pytest.mark.skipif(not (ENH / ".git").exists(), reason="kubernetes/enhancements clone not present")


def run(name, **args):
    reset_trace()
    return call_tool(name, args, graph_name="brain_eval")


def cites_everything(r):
    urls = [p["url"] for p in r["data"].get("passages", [])] + [c["url"] for c in r["data"].get("comments", [])]
    assert all(u in r["sources"] for u in urls)


@needs_graph
def test_read_thread_pr_with_reviews():
    r = run("read_thread", ref="#4255")
    assert r["found"] and r["data"]["kind"] == "pull request" and r["data"]["keps"] == ["KEP-753"]
    assert r["data"]["opened_by"] and r["data"]["comments"] and "approved" in r["data"]["reviews"]
    assert r["data"]["comments_total"] >= len(r["data"]["comments"])
    cites_everything(r)


@needs_graph
def test_read_thread_by_kep_and_url_and_missing():
    assert run("read_thread", ref="KEP-3063")["data"]["thread"] == "enhancements#3063"
    assert run("read_thread", ref="https://github.com/kubernetes/enhancements/pull/4255")["data"]["number"] == 4255
    missing = run("read_thread", ref="#999999")
    assert not missing["found"] and "not in the graph" in missing["notes"][0]


@needs_graph
def test_read_thread_as_of_hides_later_comments():
    from src.tools.read import read_thread
    now = run("read_thread", ref="KEP-753")
    then = read_thread("KEP-753", as_of="2020-01-01", graph_name="brain_eval")
    assert then["found"] and then["data"]["comments_total"] < now["data"]["comments_total"]
    assert all(c["date"] < "2020-01-01" for c in then["data"]["comments"])


@needs_graph
def test_search_finds_and_cites_passages():
    r = run("search", query="why sidecar containers need restartPolicy Always")
    assert r["found"] and 1 <= len(r["data"]["passages"]) <= 12
    cites_everything(r)
    scoped = run("search", query="graceful termination order", kep="KEP-753", kind="discussion")
    assert scoped["found"] and all(p["type"] == "discussion" for p in scoped["data"]["passages"])


@needs_graph
@pytest.mark.parametrize("lure", ["quantum teleportation of pods", "holographic volume driver",
                                  "blockchain-based pod billing rejected"])
def test_search_lures_are_not_found(lure):
    r = run("search", query=lure)
    assert not r["found"] and not r["data"]["passages"]


@needs_graph
@needs_clone
def test_read_kep_sections():
    toc = run("read_kep", kep="KEP-753")
    assert toc["found"] and any("Non-Goals" in s for s in toc["data"]["sections"])
    sec = run("read_kep", kep="sidecar containers", section="non-goals")
    assert sec["found"] and sec["data"]["kep"] == "KEP-753" and sec["data"]["text"]
    assert sec["data"]["url"].endswith("#non-goals") and sec["data"]["url"] in sec["sources"]
    bad = run("read_kep", kep="KEP-753", section="quantum stuff")
    assert not bad["found"] and bad["data"]["sections"]


def test_strict_tool_schemas_stay_within_the_union_limit():
    """The API rejects strict tool sets with more than 16 nullable/union parameters in total."""
    from src.agent.loop import tool_specs
    unions = sum(1 for spec in tool_specs() for p in spec["input_schema"]["properties"].values() if "anyOf" in p)
    assert unions <= 16, unions


@needs_graph
def test_read_path_is_read_only():
    """Tools query through ro_query: the database itself refuses a write from a read tool."""
    from src.brain.db import graph
    from src.tools.base import run
    with pytest.raises(Exception, match="read-only"):
        run(graph("brain_eval"), "CREATE (:Probe {x: 1})")
    assert graph("brain_eval").query("MATCH (p:Probe) RETURN count(p)").result_set[0][0] == 0


def test_name_guard_flags_invented_kep_login_and_team():
    from src.agent import guard
    res = [{"tool": "trace_decision", "found": True, "notes": [], "path": {"nodes": []},
            "data": {"kep": "KEP-753", "team": "sig-node", "approvers": ["mrunalp"]}}]
    assert guard.invented_names("KEP-753 is owned by sig-node; @mrunalp approved it.", res) == []
    assert guard.invented_names("Replaced by KEP-4438, approved by @thockin in sig-scheduling.", res) == \
        ["@thockin", "KEP-4438", "sig-scheduling"]
    assert guard.invented_names("KEP-99999 is not in the graph.", res, question="Status of KEP-99999?") == []
    problems = guard.check("It was replaced by KEP-4438 [1].", ["u"], res)
    assert problems and "not in any tool result" in problems[0]
    fixed = guard.repair("KEP-753 is implemented [1]. It was replaced by KEP-4438. Owned by sig-node.", ["u"], res)
    assert "KEP-4438" not in fixed and "KEP-753 is implemented [1]." in fixed and "sig-node" in fixed


@needs_graph
def test_recall_on_a_graph_without_memory_says_so():
    r = run("recall", entity="KEP-753")          # brain_eval is frozen: it never stores questions
    assert not r["found"] and "Nothing was asked" in r["notes"][0]
    assert not run("recall", entity="holographic volume driver")["found"]
