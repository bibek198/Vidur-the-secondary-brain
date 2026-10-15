"""Phase 2 proof: a toy graph with real-shaped Kubernetes facts answers every demo question.

Interval fields (valid_from / valid_to) are epoch seconds; valid_to = null means "still true".
If a demo question needs an awkward query here, the schema is wrong and must change before loading.
"""
import pytest

from src.brain.db import client
from src.brain.schema import create_indexes

T2020, T2023, T2023B, T2025, NOW = 1608163200, 1675900800, 1696464000, 1738627200, 1792000000

TOY = """
CREATE (node:Team {id:'sig-node', name:'Node', kind:'sig'}),
       (sched:Team {id:'sig-scheduling', name:'Scheduling', kind:'sig'}),
       (sp:Subproject {id:'sig-node/kubelet', name:'kubelet'}),
       (ca:CodeArea {id:'kubernetes/kubernetes:pkg/kubelet', path:'pkg/kubelet'}),
       (comp:Component {name:'kubelet'}), (fg:FeatureGate {name:'SidecarContainers'}),
       (sergey:Person {login:'sergeykanzhelev'}), (mrunal:Person {login:'mrunalp'}),
       (derek:Person {login:'derekwaynecarr'}), (old:Person {login:'dchen1107'}),
       (google:Org {name:'Google'}),
       (k753:KEP {id:'KEP-753', title:'Sidecar Containers', status:'implemented'}),
       (k3063:KEP {id:'KEP-3063', title:'dynamic resource allocation', status:'withdrawn'}),
       (k4381:KEP {id:'KEP-4381', title:'DRA structured parameters', status:'implemented'}),
       (k4438:KEP {id:'KEP-4438', title:'Sidecar restart termination', status:'implementable'}),
       (prov:Status {name:'provisional'}), (impl:Status {name:'implementable'}),
       (done:Status {name:'implemented'}), (wd:Status {name:'withdrawn'}),
       (r128:Release {version:'v1.28'}), (r129:Release {version:'v1.29'}), (r133:Release {version:'v1.33'}),
       (t:Ticket {id:'enhancements#753', url:'https://github.com/kubernetes/enhancements/issues/753'}),
       (c1:Comment {id:'comment:1', body:'Withdrawn in favour of structured parameters', url:'https://x/c1'}),
       (ev:Event {event_id:'e1', kind:'kep_status_changed', occurred_ts:$t2025}),
       (node)-[:HAS_SUBPROJECT]->(sp)-[:OWNS_CODE]->(ca), (comp)-[:IMPLEMENTED_IN]->(ca),
       (mrunal)-[:APPROVES_CODE]->(ca), (derek)-[:APPROVES_CODE]->(ca), (old)-[:APPROVES_CODE {former:true}]->(ca),
       (sergey)-[:LEADS {role:'chair', valid_from:$t2020, valid_to:null}]->(node),
       (old)-[:LEADS {role:'chair', valid_from:0, valid_to:$t2023}]->(node),
       (sergey)-[:AFFILIATED_WITH]->(google),
       (node)-[:OWNS]->(k753), (sched)-[:PARTICIPATES_IN]->(k753), (node)-[:OWNS]->(k3063), (node)-[:OWNS]->(k4381),
       (sergey)-[:AUTHORED]->(k753), (mrunal)-[:APPROVES {valid_from:$t2020, valid_to:null}]->(k753),
       (k753)-[:HAD_STATUS {valid_from:$t2020, valid_to:$t2023}]->(prov),
       (k753)-[:HAD_STATUS {valid_from:$t2023, valid_to:$t2025}]->(impl),
       (k753)-[:HAD_STATUS {valid_from:$t2025, valid_to:null}]->(done),
       (k3063)-[:HAD_STATUS {valid_from:$t2023, valid_to:null}]->(wd),
       (k753)-[:TARGETS {stage:'alpha', valid_from:$t2023, valid_to:null}]->(r128),
       (k753)-[:TARGETS {stage:'beta', valid_from:$t2023b, valid_to:null}]->(r129),
       (k753)-[:TARGETS {stage:'stable', valid_from:$t2025, valid_to:null}]->(r133),
       (k753)-[:GATED_BY]->(fg)-[:AFFECTS]->(comp), (k4438)-[:SEE_ALSO]->(k753),
       (k4381)-[:SUPERSEDES {source:'llm', verified:true}]->(k3063),
       (k753)-[:TRACKED_BY]->(t), (sergey)-[:WROTE]->(c1)-[:ON]->(t),
       (ev)-[:TOUCHED]->(k753)
"""


@pytest.fixture(scope="module")
def g():
    graph = client().select_graph("pytest_toy")
    graph.delete() if "pytest_toy" in client().list_graphs() else None
    create_indexes("pytest_toy")
    graph.query(TOY, {"t2020": T2020, "t2023": T2023, "t2023b": T2023B, "t2025": T2025})
    yield graph
    graph.delete()


def rows(g, cypher, **params):
    return g.query(cypher, params).result_set


def test_q1_who_owns_kubelet(g):
    # Component -> CodeArea <- Subproject <- Team, plus current approvers of the code area
    r = rows(g, """MATCH (:Component {name:$c})-[:IMPLEMENTED_IN]->(ca:CodeArea)<-[:OWNS_CODE]-(:Subproject)<-[:HAS_SUBPROJECT]-(t:Team)
                   OPTIONAL MATCH (p:Person)-[a:APPROVES_CODE]->(ca) WHERE a.former IS NULL
                   RETURN t.id, collect(p.login) ORDER BY t.id""", c="kubelet")
    assert r[0][0] == "sig-node" and sorted(r[0][1]) == ["derekwaynecarr", "mrunalp"]


def test_q2_who_knows_sidecars(g):
    r = rows(g, """CALL db.idx.fulltext.queryNodes('KEP', $q) YIELD node AS k
                   MATCH (p:Person)-[rel:AUTHORED|APPROVES|REVIEWS]->(k)
                   RETURN p.login, count(rel) ORDER BY p.login""", q="sidecar")
    assert ["sergeykanzhelev", 1] in [list(x) for x in r]


def test_q3_why_withdrawn_and_what_replaced_it(g):
    r = rows(g, """MATCH (k:KEP {id:$id})-[h:HAD_STATUS]->(s:Status) WHERE h.valid_to IS NULL
                   OPTIONAL MATCH (newer:KEP)-[:SUPERSEDES]->(k)
                   RETURN s.name, newer.id""", id="KEP-3063")
    assert r == [["withdrawn", "KEP-4381"]]


def test_q4_status_as_of_a_date(g):
    q = """MATCH (k:KEP {id:$id})-[h:HAD_STATUS]->(s:Status)
           WHERE h.valid_from <= $t AND (h.valid_to IS NULL OR h.valid_to > $t) RETURN s.name"""
    assert rows(g, q, id="KEP-753", t=T2023B) == [["implementable"]]
    assert rows(g, q, id="KEP-753", t=NOW) == [["implemented"]]


def test_q5_impact_of_a_kep(g):
    r = rows(g, """MATCH (k:KEP {id:$id})-[:GATED_BY]->(:FeatureGate)-[:AFFECTS]->(c:Component)
                   OPTIONAL MATCH (c)-[:IMPLEMENTED_IN]->(:CodeArea)<-[a:APPROVES_CODE]-(p:Person) WHERE a.former IS NULL
                   OPTIONAL MATCH (other:KEP)-[:SEE_ALSO]->(k)
                   RETURN c.name, collect(DISTINCT p.login), collect(DISTINCT other.id)""", id="KEP-753")
    assert r[0][0] == "kubelet" and "mrunalp" in r[0][1] and r[0][2] == ["KEP-4438"]


def test_q6_who_leads_now_vs_then(g):
    q = """MATCH (p:Person)-[l:LEADS]->(:Team {id:$team})
           WHERE l.valid_from <= $t AND (l.valid_to IS NULL OR l.valid_to > $t) RETURN p.login ORDER BY p.login"""
    assert rows(g, q, team="sig-node", t=NOW) == [["sergeykanzhelev"]]
    assert rows(g, q, team="sig-node", t=T2020 + 1) == [["dchen1107"], ["sergeykanzhelev"]]


def test_q7_what_changed_since(g):
    r = rows(g, """MATCH (e:Event)-[:TOUCHED]->(k:KEP)<-[:OWNS]-(:Team {id:$team})
                   WHERE e.occurred_ts >= $since RETURN e.kind, k.id""", team="sig-node", since=T2023)
    assert r == [["kep_status_changed", "KEP-753"]]


def test_q8_releases_for_a_kep(g):
    r = rows(g, """MATCH (k:KEP {id:$id})-[t:TARGETS]->(r:Release) WHERE t.valid_to IS NULL
                   RETURN t.stage, r.version ORDER BY r.version""", id="KEP-753")
    assert r == [["alpha", "v1.28"], ["beta", "v1.29"], ["stable", "v1.33"]]


def test_q9_evidence_trail_from_kep_to_comment_author(g):
    r = rows(g, """MATCH (k:KEP {id:$id})-[:TRACKED_BY]->(:Ticket)<-[:ON]-(c:Comment)<-[:WROTE]-(p:Person)
                   RETURN p.login, c.url""", id="KEP-753")
    assert r == [["sergeykanzhelev", "https://x/c1"]]


def test_q10_connection_between_person_and_kep(g):
    # FalkorDB's shortestPath is directed-only and only allowed in WITH/RETURN; algo.SPpaths
    # supports relDirection 'both', which "how is X connected to Y" needs.
    r = rows(g, """MATCH (a:Person {login:$a}), (b:KEP {id:$b})
                   CALL algo.SPpaths({sourceNode: a, targetNode: b, relDirection: 'both', maxLen: 4})
                   YIELD path RETURN length(path), [n IN nodes(path) | coalesce(n.login, n.id, n.name)]""",
             a="derekwaynecarr", b="KEP-753")
    assert r and r[0][0] <= 4 and r[0][1][0] == "derekwaynecarr" and r[0][1][-1] == "KEP-753"
