"""Real-time engine: stream, live handlers and their knock-on effects, git/webhook/poller mapping.

Runs on a throwaway graph (test_rt) and its own stream keys, so brain_eval is never touched.
No network: the webhook and poller are tested through their pure mapping functions and TestClient.
"""
import hashlib
import hmac
import json
import time

import pytest

from src.brain.db import client, graph
from src.realtime import reextract, stream
from src.realtime.events import Event, now_iso
from src.realtime.handlers import apply_event

G = "test_rt"


@pytest.fixture
def g():
    c = client()
    if G in c.list_graphs():
        c.connection.execute_command("GRAPH.DELETE", G)
    stream.reset(G)
    gr = graph(G)
    yield gr
    if G in c.list_graphs():
        c.connection.execute_command("GRAPH.DELETE", G)
    stream.reset(G)


def _kep(gr, kid="KEP-9001", status="implementable", when="2026-01-01T00:00:00Z"):
    ev = Event("git", "kep_created", kid, when, {"title": "Test KEP", "status": status, "path": None,
                                                "owning_sig": "sig-node", "milestones": {}, "approvers": [],
                                                "authors": ["alice"]}, when)
    apply_event(gr, json.loads(ev.to_json()))
    gr.query("""MATCH (k:KEP {id:$k}) MERGE (d:Decision {id:'dec:'+$k}) SET d.stale = false, d.input_hash = 'x'
                MERGE (d)-[:DECIDED]->(k)""", {"k": kid})


def _comment(kid="KEP-9001", body="We decided to withdraw this because nobody maintains it anymore, and the cloud "
             "providers moved to a different design last year.", cid="comment:1",
             kind="comment_added"):
    n = kid.split("-")[1]
    return json.loads(Event("github", kind, cid, now_iso(), {
        "kind": "issue", "thread": f"enhancements#{n}", "thread_label": "Ticket", "thread_number": int(n),
        "thread_url": f"https://github.com/kubernetes/enhancements/issues/{n}", "author": "bob", "body": body,
        "url": f"https://github.com/kubernetes/enhancements/issues/{n}#c"}).to_json())


def counts(gr):
    return (gr.query("MATCH (n) RETURN count(n)").result_set[0][0],
            gr.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0])


def test_stream_round_trip_and_dead_letter(g):
    stream.publish(Event("inject", "kep_title_changed", "KEP-1", now_iso(), {"to": "x"}), G)
    seen = []
    assert stream.consume(G, seen.append, block_ms=100) == 1 and seen[0]["kind"] == "kep_title_changed"
    assert stream.stats(G)["pending"] == 0

    stream.publish(Event("inject", "boom", "KEP-1", now_iso(), {}), G)

    def fail(ev):
        raise RuntimeError("handler failed")
    for _ in range(stream.MAX_TRIES):
        stream.consume(G, fail, block_ms=100)
    assert stream.stats(G)["dead_letters"] == 1 and stream.stats(G)["pending"] == 0


def test_status_change_out_of_order_and_replay(g):
    _kep(g)
    late = json.loads(Event("git", "kep_status_changed", "KEP-9001", "2026-03-01T00:00:00Z",
                            {"from": "implementable", "to": "withdrawn"}).to_json())
    early = json.loads(Event("git", "kep_status_changed", "KEP-9001", "2026-02-01T00:00:00Z",
                             {"from": "implementable", "to": "implemented"}).to_json())
    apply_event(g, late, live=True)
    apply_event(g, early, live=True)          # arrives after the later change
    rows = g.query("""MATCH (:KEP {id:'KEP-9001'})-[e:HAD_STATUS]->(s) RETURN s.name, e.valid_to
                      ORDER BY e.valid_from""").result_set
    assert [r[0] for r in rows] == ["implementable", "implemented", "withdrawn"]
    assert sum(1 for r in rows if r[1] is None) == 1 and rows[-1][1] is None
    before = counts(g)
    apply_event(g, late, live=True)
    apply_event(g, early, live=True)
    assert counts(g) == before                 # replaying changes nothing


def test_comment_marks_stale_flags_answers_and_notifies(g):
    _kep(g)
    g.query("MATCH (k:KEP {id:'KEP-9001'}) MERGE (q:Question {id:'q:1'}) MERGE (q)-[:ASKED_ABOUT]->(k)")
    ps = stream.r().pubsub()
    ps.subscribe(stream.notify_channel(G))
    apply_event(g, _comment(), live=True)
    assert g.query("MATCH (c:Comment {id:'comment:1'})-[:ON]->(:Ticket)<-[:TRACKED_BY]-(:KEP {id:'KEP-9001'}) "
                   "RETURN count(c)").result_set[0][0] == 1
    assert g.query("MATCH (d:Decision {id:'dec:KEP-9001'}) RETURN d.stale").result_set[0][0] is True
    assert stream.r().zscore(reextract.queue_key(G), "KEP-9001") is not None
    assert g.query("MATCH (:Question {id:'q:1'})-[:OUTDATED_BY]->(e:Event) RETURN count(e)").result_set[0][0] >= 1
    assert g.query("MATCH (c:Chunk {id:'chunk:comment:1'}) RETURN count(c)").result_set[0][0] == 1
    notices = []
    deadline = time.time() + 2
    while time.time() < deadline and not notices:
        m = ps.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if m:
            notices.append(json.loads(m["data"]))
    assert notices and notices[0]["kind"] == "comment_added" and "q:1" in notices[0]["outdated_questions"]
    assert g.query("MATCH (m:Meta {key:'freshness'}) RETURN m.lag_ms").result_set[0][0] is not None


def test_edited_comment_that_loses_its_quote_drops_the_evidence(g):
    _kep(g)
    apply_event(g, _comment(), live=False)
    g.query("""MATCH (d:Decision {id:'dec:KEP-9001'}), (c:Comment {id:'comment:1'})
               MERGE (d)-[e:EVIDENCED_BY]->(c) SET e.quote = 'withdraw this because nobody maintains it'""")
    apply_event(g, _comment(body="Never mind, this is still going ahead as planned.", kind="comment_edited"), live=False)
    assert g.query("MATCH (:Decision)-[e:EVIDENCED_BY]->() RETURN count(e)").result_set[0][0] == 0
    assert g.query("MATCH (d:Decision {id:'dec:KEP-9001'}) RETURN d.stale").result_set[0][0] is True


def test_reextract_skips_unchanged_input(g, monkeypatch):
    _kep(g)
    g.query("MATCH (d:Decision) SET d.stale = true")
    import src.extract.prompt as prompt
    monkeypatch.setattr(prompt, "build_input", lambda kid, graph_name=None, commit=None: {
        "kep": kid, "status": "implementable", "text": "t", "sources": {}, "input_hash": "x"})
    reextract.enqueue(G, "KEP-9001", delay_s=0)
    assert reextract.run_due(G) == [{"kep": "KEP-9001", "result": "unchanged"}]
    assert g.query("MATCH (d:Decision) RETURN d.stale").result_set[0][0] is False


# ---------- sources (pure mapping, no network) ----------

def test_diff_kep_gives_status_change():
    from src.normalize.history import diff_kep
    prev = {"status": "implementable", "stage": "alpha", "latest_milestone": "v1.30", "owning_sig": "sig-node",
            "title": "T", "milestones": {"alpha": "v1.30"}, "approvers": ["a"], "superseded_by": [], "replaces": []}
    cur = {**prev, "status": "withdrawn"}
    evs = diff_kep("KEP-1", prev, cur, "2026-09-30T00:00:00Z", {"url": "u"})
    assert [(e.kind, e.payload["to"]) for e in evs] == [("kep_status_changed", "withdrawn")]


def test_poller_maps_github_json():
    from src.realtime.poller import comment_event, thread_events
    c = {"id": 5, "issue_url": "https://api.github.com/repos/kubernetes/enhancements/issues/2699",
         "html_url": "https://github.com/kubernetes/enhancements/issues/2699#issuecomment-5", "user": {"login": "Bob"},
         "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z", "body": "See KEP-753 too"}
    ev = comment_event(c, {"KEP-753"})
    assert ev.kind == "comment_added" and ev.payload["thread"] == "enhancements#2699" and ev.payload["mentions"] == ["KEP-753"]
    assert comment_event({**c, "updated_at": "2026-10-02T00:00:00Z"}, set()).kind == "comment_edited"
    assert comment_event({**c, "user": {"login": "k8s-ci-robot", "type": "Bot"}}, set()) is None
    it = {"number": 7000, "title": "KEP-7000: new", "state": "closed", "user": {"login": "x"},
          "created_at": "2026-10-01T00:00:00Z", "closed_at": "2026-10-03T00:00:00Z",
          "html_url": "https://github.com/kubernetes/enhancements/issues/7000", "labels": [], "body": ""}
    assert [e.kind for e in thread_events(it, set(), "2026-09-15T00:00:00Z")] == ["thread_opened", "thread_closed"]


def test_webhook_signature_and_mapping(monkeypatch):
    from fastapi.testclient import TestClient
    from web import app as web
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(web, "DEMO_GRAPH", G)
    stream.reset(G)
    body = json.dumps({"action": "created", "repository": {"name": "decision-brain-sandbox"},
                       "issue": {"number": 1, "title": "KEP-2699 discussion", "html_url": "https://x/1"},
                       "comment": {"id": 9, "body": "We decided to withdraw KEP-2699.", "user": {"login": "Bibek"},
                                   "created_at": "2026-10-16T00:00:00Z", "html_url": "https://x/1#9"}}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    tc = TestClient(web.app)
    bad = tc.post("/api/webhook/github", content=body, headers={"X-GitHub-Event": "issue_comment",
                                                                 "X-Hub-Signature-256": "sha256=deadbeef"})
    assert bad.status_code == 401
    ok = tc.post("/api/webhook/github", content=body, headers={"X-GitHub-Event": "issue_comment",
                                                                "X-Hub-Signature-256": sig})
    assert ok.status_code == 200 and ok.json()["events"] == ["comment_added comment:9"]
    assert stream.stats(G)["stream_length"] == 1
    stream.reset(G)


def test_webhook_push_diffs_kep_yaml():
    from web.app import push_events
    yaml_before = "title: T\nkep-number: 2699\nstatus: implementable\nstage: alpha\nowning-sig: sig-cloud-provider\n"
    files = {"a1": yaml_before, "b2": yaml_before.replace("implementable", "withdrawn")}
    p = {"before": "a1", "after": "b2", "repository": {"full_name": "me/sandbox"},
         "head_commit": {"timestamp": "2026-10-16T10:00:00Z", "url": "https://github.com/me/sandbox/commit/b2"},
         "commits": [{"added": [], "modified": ["keps/sig-cloud-provider/2699-x/kep.yaml"]}]}
    evs = push_events(p, now_iso(), fetch=lambda sha, path: files[sha])
    assert [(e.kind, e.entity_key, e.payload["to"]) for e in evs] == [("kep_status_changed", "KEP-2699", "withdrawn")]


def test_webhook_accepts_form_encoded_and_smee_wrapped():
    from urllib.parse import quote_plus
    from web.app import verified_payload
    inner = json.dumps({"ref": "refs/heads/main", "note": "a b+c/é"})
    form = ("payload=" + quote_plus(inner)).encode()
    sig = "sha256=" + hmac.new(b"k", form, hashlib.sha256).hexdigest()
    assert verified_payload("k", form, sig)["note"] == "a b+c/é"                                   # direct form post
    assert verified_payload("k", json.dumps({"payload": inner}).encode(), sig)["ref"] == "refs/heads/main"  # via smee
    assert verified_payload("wrong", form, sig) is None
    assert verified_payload("k", json.dumps({"payload": inner + " "}).encode(), sig) is None       # tampered
