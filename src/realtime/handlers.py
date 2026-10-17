"""Event handlers: apply one change Event to the graph.

The same handlers replay the git history during bulk loading and apply live changes in
the real-time worker, so there is exactly one write path. Each handler only calls the
idempotent functions in src/load/upserts.py, so applying an event twice changes nothing,
and interval logic in set_interval() keeps out-of-order events correct.

A handler may return the entities it touched as (label, key_property, value) tuples; the
Event node is linked to each. With live=True (the worker), src/realtime/react.py then runs
the knock-on effects: outdated answers, watch alerts, re-extraction, freshness, UI notices.
"""
import re

from src.load import upserts
from src.realtime.events import iso_to_ts

_KEP_MENTION = re.compile(r"\bKEP[- ]?(\d{1,5})\b", re.I)


def _src(ev) -> str | None:
    return ev["payload"].get("url")


def on_kep_created(g, ev):
    p, kep, t = ev["payload"], ev["entity_key"], ev["ts"]
    number, path = int(kep.split("-")[1]), p.get("path")
    gh = "https://github.com/kubernetes/enhancements"
    upserts.kep_nodes(g, [{"id": kep, "number": number, "title": p.get("title"), "path": path,
                           "url": f"{gh}/tree/master/{path}" if path else None,
                           "readme_url": f"{gh}/blob/master/{path}/README.md" if path else None,
                           "issue_url": f"{gh}/issues/{number}", "created_ts": t, "owning_sig": p.get("owning_sig")}])
    g.query("MATCH (k:KEP {id:$id}) SET k.stage = $stage, k.latest_milestone = $lm",
            {"id": kep, "stage": p.get("stage"), "lm": p.get("latest_milestone")})
    if p.get("status"):
        _status(g, kep, p["status"], t, _src(ev))
    for stage, release in (p.get("milestones") or {}).items():
        upserts.set_interval(g, kep, "TARGETS", "Release", "version", release, t, {"stage": stage}, _src(ev))
    for login in p.get("approvers") or []:
        upserts.approver_interval(g, kep, login, t, added=True)
    if p.get("authors"):
        upserts.kep_people(g, [{"kep": kep, "login": a, "rel": "AUTHORED"} for a in p["authors"]])


def _status(g, kep, status, t, src):
    upserts.set_interval(g, kep, "HAD_STATUS", "Status", "name", status, t, None, src)
    # KEP.status mirrors the currently open interval (convenience for simple filters)
    g.query("""MATCH (k:KEP {id:$id}) OPTIONAL MATCH (k)-[e:HAD_STATUS]->(s:Status) WHERE e.valid_to IS NULL
               WITH k, s ORDER BY s.name LIMIT 1 SET k.status = s.name""", {"id": kep})


def on_kep_status_changed(g, ev):
    _status(g, ev["entity_key"], ev["payload"]["to"], ev["ts"], _src(ev))


def on_kep_milestone_changed(g, ev):
    p = ev["payload"]
    upserts.set_interval(g, ev["entity_key"], "TARGETS", "Release", "version", p.get("to"), ev["ts"],
                         {"stage": p["stage"]}, _src(ev))


def _set_prop(prop):
    def handler(g, ev):
        g.query(f"MATCH (k:KEP {{id:$id}}) SET k.{prop} = $v", {"id": ev["entity_key"], "v": ev["payload"].get("to")})
    return handler


def on_kep_owning_sig_changed(g, ev):
    g.query("""MATCH (k:KEP {id:$id}) OPTIONAL MATCH (:Team)-[o:OWNS]->(k) DELETE o""", {"id": ev["entity_key"]})
    g.query("""MATCH (k:KEP {id:$id}) MERGE (t:Team {id:$team}) MERGE (t)-[:OWNS]->(k)""",
            {"id": ev["entity_key"], "team": ev["payload"]["to"]})


def on_kep_approvers_changed(g, ev):
    p = ev["payload"]
    for login in p.get("added") or []:
        upserts.approver_interval(g, ev["entity_key"], login, ev["ts"], added=True)
    for login in p.get("removed") or []:
        upserts.approver_interval(g, ev["entity_key"], login, ev["ts"], added=False)


def on_kep_supersession(g, ev):
    """kep_replaces_changed: the KEP replaces others; kep_superseded_by_changed: others replace it."""
    kep, added = ev["entity_key"], ev["payload"].get("added") or []
    if ev["kind"] == "kep_replaces_changed":
        rows = [{"src": kep, "dst": other, "rel": "SUPERSEDES", "source": "yaml", "evidence_url": _src(ev)} for other in added]
    else:
        rows = [{"src": other, "dst": kep, "rel": "SUPERSEDES", "source": "yaml", "evidence_url": _src(ev)} for other in added]
    upserts.kep_links(g, rows)


def on_kep_file_removed(g, ev):
    g.query("MATCH (k:KEP {id:$id}) SET k.removed_ts = $t", {"id": ev["entity_key"], "t": ev["ts"]})


def on_team_created(g, ev):
    g.query("MERGE (t:Team {id:$id}) SET t.name = coalesce(t.name, $name), t.created_ts = coalesce(t.created_ts, $t)",
            {"id": ev["entity_key"], "name": ev["payload"].get("name"), "t": ev["ts"]})


def on_team_retired(g, ev):
    g.query("MATCH (t:Team {id:$id}) SET t.retired_ts = $t", {"id": ev["entity_key"], "t": ev["ts"]})


def on_team_lead_added(g, ev):
    p = ev["payload"]
    upserts.leads_interval(g, [{"login": p["login"], "team": ev["entity_key"], "role": p["role"], "t": ev["ts"]}])


def on_team_lead_removed(g, ev):
    p = ev["payload"]
    upserts.close_leads(g, [{"login": p["login"], "team": ev["entity_key"], "role": p["role"], "t": ev["ts"]}])


def on_subproject_changed(g, ev):
    sp_id = f"{ev['entity_key']}/{ev['payload']['subproject']}"
    if ev["kind"] == "subproject_added":
        g.query("""MERGE (t:Team {id:$team}) MERGE (s:Subproject {id:$id})
                   SET s.name = coalesce(s.name, $name), s.added_ts = coalesce(s.added_ts, $t)
                   MERGE (t)-[:HAS_SUBPROJECT]->(s)""",
                {"team": ev["entity_key"], "id": sp_id, "name": ev["payload"]["subproject"], "t": ev["ts"]})
    else:
        g.query("MATCH (s:Subproject {id:$id}) SET s.removed_ts = $t", {"id": sp_id, "t": ev["ts"]})


# ---------- threads and comments (poller, webhook, catch-up feed) ----------

def on_thread_opened(g, ev):
    """payload: a normalized Ticket or PR row (src/normalize/threads.py shape) plus `label`."""
    row = ev["payload"]
    if row.get("label") == "PR":
        upserts.prs(g, [{**row, "keps": row.get("keps") or []}])
    else:
        upserts.tickets(g, [row])
    keps = row.get("keps") or ([row["kep"]] if row.get("kep") else [])
    return [(row.get("label") or "Ticket", "id", row["id"])] + [("KEP", "id", k) for k in keps]


def on_thread_closed(g, ev):
    p = ev["payload"]
    g.query(f"MATCH (t:{'PR' if p.get('label') == 'PR' else 'Ticket'} {{id:$id}}) SET t.state = 'closed', t.closed_ts = $t",
            {"id": ev["entity_key"], "t": ev["ts"]})
    return [(p.get("label") or "Ticket", "id", ev["entity_key"])] + [("KEP", "id", k) for k in related_keps(g, ev["entity_key"])]


def _ensure_thread(g, p):
    """A comment can arrive before (or without) its thread: create the thread minimally."""
    label = "PR" if p.get("thread_label") == "PR" else "Ticket"
    g.query(f"""MERGE (t:{label} {{id:$id}})
                SET t.number = coalesce(t.number, $number), t.url = coalesce(t.url, $url),
                    t.title = coalesce(t.title, $title)""",
            {"id": p["thread"], "number": p.get("thread_number"), "url": p.get("thread_url"),
             "title": p.get("thread_title")})
    # convention in kubernetes/enhancements: issue #N is the tracking issue of KEP-N
    if label == "Ticket" and p["thread"].startswith("enhancements#"):
        g.query("""MATCH (t:Ticket {id:$id}), (k:KEP {id:$kep}) MERGE (k)-[:TRACKED_BY]->(t)""",
                {"id": p["thread"], "kep": f"KEP-{p['thread'].split('#')[1]}"})


def related_keps(g, thread_or_comment: str) -> list[str]:
    """KEPs a thread or comment is about: tracking issue, PRs that change the KEP, and KEP mentions."""
    label = "Comment" if thread_or_comment.split(":")[0] in ("comment", "rcomment", "review") else None
    if label is None:   # a thread id: Ticket or PR (labeled lookups use the index; unlabeled scans every node)
        label = "PR" if g.query("MATCH (x:PR {id:$id}) RETURN count(x)", {"id": thread_or_comment}).result_set[0][0] else "Ticket"
    rows = g.query(f"""MATCH (x:{label} {{id:$id}})
                      OPTIONAL MATCH (x)-[:ON]->(t)
                      WITH x, coalesce(t, x) AS thread
                      OPTIONAL MATCH (thread)<-[:TRACKED_BY]-(k1:KEP)
                      OPTIONAL MATCH (thread)-[:CHANGES]->(k2:KEP)
                      OPTIONAL MATCH (x)-[:MENTIONS]->(k3:KEP)
                      RETURN collect(DISTINCT k1.id) + collect(DISTINCT k2.id) + collect(DISTINCT k3.id)""",
                   {"id": thread_or_comment}).result_set
    return sorted({k for k in (rows[0][0] if rows else []) if k})


def on_comment(g, ev):
    """comment_added / comment_edited. payload: normalized comment row + thread_label/number/url/title."""
    p = ev["payload"]
    _ensure_thread(g, p)
    mentions = sorted({f"KEP-{int(n)}" for n in _KEP_MENTION.findall(p.get("body") or "")} | set(p.get("mentions") or []))
    upserts.comments(g, [{"id": ev["entity_key"], "kind": p.get("kind", "issue"), "thread": p["thread"],
                          "author": p["author"], "at_ts": p.get("at_ts") or ev["ts"], "url": p.get("url"),
                          "body": p.get("body") or "", "path": p.get("path"), "mentions": mentions}])
    keps = related_keps(g, ev["entity_key"])
    if ev["kind"] == "comment_edited":
        _drop_lost_quotes(g, ev["entity_key"], p.get("body") or "")
    # new discussion on a decided KEP: its rationale may no longer hold until re-extracted
    g.query("MATCH (d:Decision)-[:DECIDED]->(k:KEP) WHERE k.id IN $keps SET d.stale = true", {"keps": keps})
    return [("Comment", "id", ev["entity_key"])] + [("KEP", "id", k) for k in keps]


def _drop_lost_quotes(g, comment_id: str, body: str) -> None:
    from src.extract.validate import norm
    for quote, in g.query("MATCH (:Decision)-[e:EVIDENCED_BY]->(:Comment {id:$id}) RETURN e.quote",
                          {"id": comment_id}).result_set:
        if norm(quote) not in norm(body):
            g.query("""MATCH (d:Decision)-[e:EVIDENCED_BY {quote:$q}]->(:Comment {id:$id})
                       DELETE e SET d.stale = true, d.verified = false""", {"id": comment_id, "q": quote})


HANDLERS = {
    "kep_created": on_kep_created,
    "kep_status_changed": on_kep_status_changed,
    "kep_milestone_changed": on_kep_milestone_changed,
    "kep_stage_changed": _set_prop("stage"),
    "kep_latest_milestone_changed": _set_prop("latest_milestone"),
    "kep_title_changed": _set_prop("title"),
    "kep_owning_sig_changed": on_kep_owning_sig_changed,
    "kep_approvers_changed": on_kep_approvers_changed,
    "kep_replaces_changed": on_kep_supersession,
    "kep_superseded_by_changed": on_kep_supersession,
    "kep_file_removed": on_kep_file_removed,
    "team_created": on_team_created,
    "team_retired": on_team_retired,
    "team_lead_added": on_team_lead_added,
    "team_lead_removed": on_team_lead_removed,
    "subproject_added": on_subproject_changed,
    "subproject_removed": on_subproject_changed,
    "thread_opened": on_thread_opened,
    "thread_closed": on_thread_closed,
    "comment_added": on_comment,
    "comment_edited": on_comment,
}

_ENTITY = {"kep_": ("KEP", "id"), "team_": ("Team", "id"), "subproject_": ("Team", "id")}


def apply_event(g, ev: dict, live: bool = False) -> bool:
    """Apply one event dict (Event.to_json shape). Returns False if the kind has no handler.
    live=True (the worker) also runs the knock-on effects in src/realtime/react.py."""
    ev = {**ev, "ts": iso_to_ts(ev["occurred_at"])}
    handler = HANDLERS.get(ev["kind"])
    if handler is None:
        return False
    touched = handler(g, ev)
    if not touched:
        label, key = next(v for prefix, v in _ENTITY.items() if ev["kind"].startswith(prefix))
        touched = [(label, key, ev["entity_key"])]
    summary = summarize(ev)
    for label, key, value in touched:
        upserts.event_node(g, {**ev, "summary": summary}, label, key, value)
    if live:
        from src.realtime import react
        react.after_event(g, ev, summary, touched)
    return True


def summarize(ev: dict) -> str:
    """One readable line per event, shown in the UI feed and returned by what_changed."""
    p, k = ev["payload"], ev["entity_key"]
    if "from" in p or "to" in p:
        what = ev["kind"].removeprefix("kep_").removesuffix("_changed").replace("_", " ")
        stage = f" ({p['stage']})" if p.get("stage") else ""
        return f"{k} {what}{stage}: {p.get('from')} → {p.get('to')}"
    if ev["kind"] in ("team_lead_added", "team_lead_removed"):
        verb = "became" if ev["kind"].endswith("added") else "stepped down as"
        return f"{p['login']} {verb} {p['role'].replace('_', ' ')} of {k}"
    if ev["kind"] == "kep_created":
        return f"{k} created: {p.get('title')} ({p.get('status')})"
    if ev["kind"] in ("comment_added", "comment_edited"):
        verb = "commented" if ev["kind"] == "comment_added" else "edited a comment"
        body = " ".join((p.get("body") or "").split())
        return f"@{p.get('author')} {verb} on {p.get('thread')}: {body[:90]}{'…' if len(body) > 90 else ''}"
    if ev["kind"] in ("thread_opened", "thread_closed"):
        what = "PR" if p.get("label") == "PR" else "issue"
        return f"{what} {k} {ev['kind'].split('_')[1]}: {p.get('title') or ''}".strip()
    if ev["kind"] in ("subproject_added", "subproject_removed"):
        return f"{k} subproject {p.get('subproject')} {ev['kind'].split('_')[1]}"
    if "added" in p or "removed" in p:
        return f"{k} {ev['kind'].removeprefix('kep_').replace('_', ' ')}: +{p.get('added')} -{p.get('removed')}"
    return f"{k} {ev['kind'].replace('_', ' ')}"
