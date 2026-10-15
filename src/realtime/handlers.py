"""Event handlers: apply one change Event to the graph.

The same handlers replay the git history during bulk loading and apply live changes in
the real-time worker, so there is exactly one write path. Each handler only calls the
idempotent functions in src/load/upserts.py, so applying an event twice changes nothing,
and interval logic in set_interval() keeps out-of-order events correct.
"""
from src.load import upserts
from src.realtime.events import iso_to_ts


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
}

_ENTITY = {"kep_": ("KEP", "id"), "team_": ("Team", "id"), "subproject_": ("Team", "id")}


def apply_event(g, ev: dict) -> bool:
    """Apply one event dict (Event.to_json shape). Returns False if the kind has no handler."""
    ev = {**ev, "ts": iso_to_ts(ev["occurred_at"])}
    handler = HANDLERS.get(ev["kind"])
    if handler is None:
        return False
    handler(g, ev)
    label, key = next(v for prefix, v in _ENTITY.items() if ev["kind"].startswith(prefix))
    upserts.event_node(g, {**ev, "summary": summarize(ev)}, label, key, ev["entity_key"])
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
    if "added" in p or "removed" in p:
        return f"{k} {ev['kind'].removeprefix('kep_').replace('_', ' ')}: +{p.get('added')} -{p.get('removed')}"
    return f"{k} {ev['kind'].replace('_', ' ')}"
