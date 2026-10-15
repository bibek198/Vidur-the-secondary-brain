"""Graph tools on the real frozen graph (brain_eval, as of 2026-09-15). No LLM involved.

The expected values were checked by hand against GitHub (see docs/project/BUILD_LOG.md, Phase 3/4).
Skipped when brain_eval has not been loaded (python -m src.load.loader).
"""
import pytest

from src.brain.db import client
from src.tools.base import reset_trace
from src.tools.registry import TOOLS, call_tool

pytestmark = pytest.mark.skipif("brain_eval" not in client().list_graphs(), reason="brain_eval not loaded")
G = "brain_eval"


@pytest.fixture(autouse=True)
def _fresh_trace():
    reset_trace()


def run(name, **args):
    return call_tool(name, args, graph_name=G)


def check_shape(r):
    for key in ("tool", "found", "data", "notes", "sources", "path", "cypher_trace", "as_of", "stale_flags"):
        assert key in r
    assert all(s.startswith("http") for s in r["sources"])
    assert all(c["ms"] < 1000 for c in r["cypher_trace"]), [c for c in r["cypher_trace"] if c["ms"] >= 1000]


def test_who_owns_kubelet():
    r = run("who_owns", target="kubelet")
    check_shape(r)
    assert "sig-node" in r["data"]["teams"]
    approvers = {p for a in r["data"]["code_areas"] for p in a["approvers"]}
    assert "mrunalp" in approvers and r["sources"]


def test_trace_withdrawn_kep_is_never_adopted():
    r = run("trace_decision", kep_or_query="KEP-3063")
    check_shape(r)
    assert r["data"]["status"] == "withdrawn"
    assert r["data"]["status"] != "implemented"
    assert any("NOT adopted" in n for n in r["notes"])
    assert "https://github.com/kubernetes/enhancements/pull/4904" in r["sources"]  # the withdrawing PR


def test_trace_history_and_as_of():
    r = run("trace_decision", kep_or_query="KEP-753")
    assert [h["status"] for h in r["data"]["status_history"]] == ["provisional", "implementable", "implemented"]
    assert r["data"]["status"] == "implemented"
    past = run("trace_decision", kep_or_query="KEP-753", as_of="v1.29")
    assert past["data"]["status"] == "implementable" and past["as_of"].startswith("release v1.29")


def test_live_feed_kep_is_pre_change_in_frozen_graph():
    # KEP-2699 becomes withdrawn on 2026-09-30, after the cutoff: the frozen graph must not know yet
    assert run("trace_decision", kep_or_query="KEP-2699")["data"]["status"] == "implementable"


@pytest.mark.parametrize("lure", ["quantum teleport scheduler", "blockchain based pod billing"])
def test_lures_are_not_found(lure):
    r = run("trace_decision", kep_or_query=lure)
    assert r["found"] is False
    assert any("Do not describe it as a decision" in n for n in r["notes"])


def test_name_match_resolves_to_best_title():
    assert run("trace_decision", kep_or_query="graceful node shutdown")["data"]["kep"] == "KEP-2000"
    assert run("trace_decision", kep_or_query="sidecar containers")["data"]["kep"] == "KEP-753"


def test_who_knows_sidecars():
    r = run("who_knows", topic="sidecar containers")
    check_shape(r)
    logins = [p["login"] for p in r["data"]["people"]]
    assert "sergeykanzhelev" in logins[:3] or "mrunalp" in logins[:3]
    assert "KEP-753" in r["data"]["related_keps"]


def test_impact_of_sidecars():
    r = run("impact_of", entity="KEP-753")
    comps = {c["component"] for c in r["data"]["components"]}
    assert {"kubelet", "kube-apiserver"} <= comps
    assert "KEP-4438" in {k["kep"] for k in r["data"]["related_keps"]}


def test_timeline_has_sourced_changes():
    r = run("timeline", kep="KEP-753")
    kinds = [c["kind"] for c in r["data"]["changes"]]
    assert kinds[0] == "kep_created" and "kep_status_changed" in kinds
    assert {"stage": "stable", "release": "v1.33"} in r["data"]["targets"]


def test_what_changed_for_team():
    r = run("what_changed", since="2026-08-01", team="sig-node")
    check_shape(r)
    assert r["found"] and r["data"]["count"] > 0
    assert all(c["date"] >= "2026-08-01" for c in r["data"]["changes"])


def test_connect_person_to_kep_marks_past_links():
    r = run("connect", a="derekwaynecarr", b="KEP-753")
    assert r["data"]["hops"] == 1
    assert r["data"]["steps"][0]["rel"] == "APPROVES" and r["data"]["steps"][0]["until"] == "2023-02-09"


def test_influence_top_reviewers_are_maintainers():
    r = run("influence", metric="pagerank", limit=10)
    assert {"pohly", "tallclair"} & {p["login"] for p in r["data"]["people"]}


def test_registry_rejects_unknown_arguments():
    with pytest.raises(ValueError):
        call_tool("who_owns", {"target": "kubelet", "cypher": "MATCH (n) DELETE n"}, graph_name=G)
    with pytest.raises(ValueError):
        call_tool("drop_graph", {}, graph_name=G)


def test_every_tool_has_description_and_schema():
    for name, spec in TOOLS.items():
        assert len(spec["description"]) > 80 and spec["schema"]["type"] == "object", name
