"""History/real-time handlers: replays are no-ops, intervals stay consistent, order does not matter."""
import pytest

from src.brain.db import client
from src.brain.schema import create_indexes
from src.realtime.events import Event
from src.realtime.handlers import apply_event

GRAPH = "pytest_handlers"


def ev(kind, key, when, **payload):
    return Event("git", kind, key, when, payload, when).__dict__


CREATED = ev("kep_created", "KEP-9", "2023-01-01T00:00:00Z", title="Test KEP", status="provisional",
             owning_sig="sig-node", path="keps/sig-node/9-test", milestones={"alpha": "v1.27"},
             approvers=["alice"], authors=["bob"])
TO_IMPL = ev("kep_status_changed", "KEP-9", "2024-01-01T00:00:00Z", **{"from": "provisional", "to": "implementable"})
TO_DONE = ev("kep_status_changed", "KEP-9", "2025-01-01T00:00:00Z", **{"from": "implementable", "to": "implemented"})
MILESTONE = ev("kep_milestone_changed", "KEP-9", "2024-06-01T00:00:00Z", stage="alpha", **{"from": "v1.27", "to": "v1.28"})
LEAD_ON = ev("team_lead_added", "sig-node", "2022-01-01T00:00:00Z", login="carol", role="chair")
LEAD_OFF = ev("team_lead_removed", "sig-node", "2024-03-01T00:00:00Z", login="carol", role="chair")


@pytest.fixture()
def g():
    graph = client().select_graph(GRAPH)
    if GRAPH in client().list_graphs():
        graph.delete()
    create_indexes(GRAPH)
    yield graph
    graph.delete()


def statuses(g):
    return g.query("""MATCH (:KEP {id:'KEP-9'})-[e:HAD_STATUS]->(s) RETURN s.name, e.valid_from, e.valid_to
                      ORDER BY e.valid_from""").result_set


def snapshot(g):
    nodes = g.query("MATCH (n) RETURN count(n)").result_set[0][0]
    rels = g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return nodes, rels, statuses(g)


def test_status_intervals_chain(g):
    for e in (CREATED, TO_IMPL, TO_DONE):
        apply_event(g, e)
    s = statuses(g)
    assert [r[0] for r in s] == ["provisional", "implementable", "implemented"]
    assert s[0][2] == s[1][1] and s[1][2] == s[2][1] and s[2][2] is None
    assert g.query("MATCH (k:KEP {id:'KEP-9'}) RETURN k.status").result_set == [["implemented"]]


def test_replay_is_a_no_op(g):
    for e in (CREATED, TO_IMPL, TO_DONE, MILESTONE, LEAD_ON, LEAD_OFF):
        apply_event(g, e)
    first = snapshot(g)
    for e in (CREATED, TO_IMPL, TO_DONE, MILESTONE, LEAD_ON, LEAD_OFF):
        apply_event(g, e)
    assert snapshot(g) == first


def test_out_of_order_gives_same_result(g):
    for e in (CREATED, TO_IMPL, TO_DONE):
        apply_event(g, e)
    in_order = statuses(g)
    g.delete()
    create_indexes(GRAPH)
    for e in (CREATED, TO_DONE, TO_IMPL):  # the middle change arrives last
        apply_event(g, e)
    assert statuses(g) == in_order


def test_milestone_and_leadership_intervals(g):
    for e in (CREATED, MILESTONE, LEAD_ON, LEAD_OFF):
        apply_event(g, e)
    t = g.query("""MATCH (:KEP {id:'KEP-9'})-[e:TARGETS {stage:'alpha'}]->(r) RETURN r.version, e.valid_to IS NULL
                   ORDER BY e.valid_from""").result_set
    assert t == [["v1.27", False], ["v1.28", True]]
    lead = g.query("MATCH (:Person {login:'carol'})-[l:LEADS]->(:Team {id:'sig-node'}) RETURN l.valid_to IS NULL").result_set
    assert lead == [[False]]


def test_every_event_is_recorded_and_linked(g):
    for e in (CREATED, TO_IMPL):
        apply_event(g, e)
    rows = g.query("MATCH (e:Event)-[:TOUCHED]->(k:KEP {id:'KEP-9'}) RETURN e.kind, e.summary ORDER BY e.occurred_ts").result_set
    assert rows[0][0] == "kep_created" and rows[1][1] == "KEP-9 status: provisional → implementable"
