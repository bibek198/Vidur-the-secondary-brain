"""Action tools: the brain does something with what it knows. No LLM writes any of this text.

- recommend_reviewers: who should review a change to a KEP or a code area, and why (read-only)
- draft_decision_brief: a markdown brief built from trace_decision + impact_of, saved as a Draft node
- route_question:       who to ask about a topic; saves a Question node routed to those people
- watch:                follow a KEP, team, component or person; the real-time engine notifies on changes

Actions are composed from the read tools, so their Cypher shows up in the same trace. They only
write small memory nodes (Draft, Question, Watch) to the current graph, always with MERGE on a
deterministic id, so repeating an action changes nothing. Nothing is ever written to GitHub.
"""
import hashlib
import time

from src.tools import read
from src.tools.base import ToolCall

MIN_SCORE = 0.6  # same bar as trace_decision: a one-word title overlap (e.g. "scheduler") is not a match


def _absorb(call: ToolCall, result: dict) -> None:
    """Merge a nested tool result's sources and graph path into this action's result."""
    call.source(result["sources"])
    for n in result["path"]["nodes"]:
        call.path.nodes.setdefault(n["id"], n)
    for e in result["path"]["edges"]:
        call.path.edges.setdefault(e["id"], e)
    call.stale += result["stale_flags"]


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ---------- recommend_reviewers ----------

def recommend_reviewers(kep_or_path: str, limit: int = 5, graph_name: str | None = None) -> dict:
    """Reviewers for a change to a KEP, component or code path: code approvers, KEP approvers and experts."""
    call = ToolCall("recommend_reviewers", graph_name)
    limit = max(1, min(int(limit), 10))
    # a path-like argument ("pkg/scheduler") means code, never a KEP whose title shares a word
    kinds = ["CodeArea", "Component"] if "/" in kep_or_path else ["KEP", "Component", "CodeArea"]
    best, cands = read._resolve_one(call, kep_or_path, kinds, min_score=0.5 if "/" in kep_or_path else MIN_SCORE)
    if best is None:
        call.notes.append("Nothing in the graph matches this. Do not suggest reviewers.")
        return call.result({"query": kep_or_path, "closest": cands[:3]}, found=False)
    people: dict[str, dict] = {}
    # a current approver of the KEP outranks breadth: code points are capped, expertise is capped
    W_KEP_APPROVER, W_CODE, CODE_CAP, EXPERT_CAP = 8, 3, 6, 4

    def add(login, points, reason, kind):
        p = people.setdefault(login, {"login": login, "score": 0, "reasons": [], "code": 0})
        if kind == "code":
            points = min(points, CODE_CAP - p["code"])
            p["code"] += points
        p["score"] += points
        if reason not in p["reasons"]:
            p["reasons"].append(reason)

    authors = set()
    if best["label"] == "KEP":
        authors = {r[0] for r in call.q("MATCH (p:Person)-[:AUTHORED]->(:KEP {id:$k}) RETURN p.login",
                                         {"k": best["key"]}, False)}
        impact = read.impact_of(best["key"], depth=1, graph_name=graph_name)
        _absorb(call, impact)
        for c in impact["data"].get("components", []):
            for login in c["code_approvers"]:
                add(login, W_CODE, f"approves {c['component']} code", "code")
        owners = read.who_owns(best["key"], graph_name=graph_name)
        _absorb(call, owners)
        for ka in owners["data"]["kep_approvers"]:
            for login in ka["approvers"]:
                add(login, W_KEP_APPROVER, f"current approver of {ka['kep']}", "kep")
        topic = best["name"].split(" ", 1)[-1]
    else:
        owners = read.who_owns(best["key"], graph_name=graph_name)
        _absorb(call, owners)
        for area in owners["data"]["code_areas"]:
            for login in area["approvers"]:
                add(login, W_CODE, f"approves {area['area'].split(':')[-1]}", "code")
        topic = best["name"]
    experts = read.who_knows(topic, limit=5, graph_name=graph_name)
    _absorb(call, experts)
    for e in experts["data"].get("people", []):
        add(e["login"], min(e["score"], EXPERT_CAP), f"expert on related KEPs ({', '.join(x['kep'] for x in e['evidence'][:2])})",
            "expert")
    for login in authors:
        people.pop(login, None)
    ranked = sorted(people.values(), key=lambda p: (-p["score"], p["login"]))[:limit]
    if authors:
        call.notes.append(f"Authors excluded (they cannot review their own change): {', '.join(sorted(authors))}.")
    call.notes.append("Ranked by: 8 for a current approver of the KEP, 3 per affected component whose code they approve "
                      "(max 6), plus expertise (max 4).")
    return call.result({"for": best, "reviewers": [{"login": p["login"], "score": round(p["score"], 1),
                                                    "reasons": p["reasons"]} for p in ranked]},
                       found=bool(ranked))


# ---------- draft_decision_brief ----------

def draft_decision_brief(kep: str, graph_name: str | None = None) -> dict:
    """A markdown brief of one decision (status, why, impact, who to tell, sources), saved as a Draft node."""
    call = ToolCall("draft_decision_brief", graph_name)
    trace = read.trace_decision(kep, graph_name=graph_name)
    _absorb(call, trace)
    if not trace["found"]:
        call.notes += trace["notes"]
        return call.result({"query": kep, "closest": trace["data"].get("closest", [])}, found=False)
    d = trace["data"]
    impact = read.impact_of(d["kep"], depth=1, graph_name=graph_name)
    _absorb(call, impact)
    owners = read.who_owns(d["kep"], graph_name=graph_name)
    _absorb(call, owners)
    num = {u: i + 1 for i, u in enumerate(call.sources)}

    def ref(url):
        return f" [{num[url]}]" if url in num else ""

    lines = [f"# Decision brief: {d['kep']} {d['title']}", "",
             f"**Status:** {d['status']}" + (f" (stage {d['stage']})" if d.get("stage") else "")
             + f" · **Owning team:** {d['owning_team']} · **Brief date:** {_today()}"]
    for r in d["replaced_by"]:
        lines.append("")
        lines.append(f"> Replaced by **{r['kep']} {r['title']}** ({r['status']})"
                     + ("" if r["verified"] else " — not yet confirmed by a person"))
    history = "; ".join(f"{h['status']} from {h['from']}{ref(h['source'])}" for h in d["status_history"])
    lines += ["", "## History", history or "No recorded status changes."]
    rat = d.get("rationale")
    if rat:
        lines += ["", "## What and why", rat["summary"] or "", "", rat["rationale"] or ""]
        if rat.get("outcome_reason"):
            lines += ["", f"**Why it was not adopted:** {rat['outcome_reason']}"]
        if rat.get("alternatives"):
            lines += ["", "**Alternatives considered:**"] + [f"- {a}" for a in rat["alternatives"]]
        if rat.get("quotes"):
            lines += ["", "**Evidence:**"] + [f"> \"{q['quote']}\" — {q['by']}{ref(q['url'])}" for q in rat["quotes"][:4]]
        lines += ["", "_The rationale was written by an LLM from these sources; each quote was checked word for word._"]
    comps = impact["data"].get("components", [])
    lines += ["", "## What it affects"]
    lines += [f"- {c['component']} (gates: {', '.join(c['via_gates'])})" for c in comps] or ["- No feature gates declared."]
    lines += [f"- Related: {k['kep']} {k['title']} ({k['status']})" for k in impact["data"].get("related_keps", [])[:5]]
    tell = sorted({x["login"] for x in owners["data"]["leads"]} | set(d["approvers"]))
    lines += ["", "## Who to tell", ", ".join(f"@{x}" for x in tell) or "No current owners recorded."]
    lines += ["", "## Sources"] + [f"{i + 1}. {u}" for i, u in enumerate(call.sources[:25])]
    markdown = "\n".join(lines)

    draft_id = f"brief:{d['kep']}:{_today()}"
    call.q("""MATCH (k:KEP {id:$kep})
              MERGE (d:Draft {id:$id}) SET d.kind = 'decision_brief', d.markdown = $md, d.created_at = $at
              MERGE (d)-[:ABOUT]->(k)""",
           {"kep": d["kep"], "id": draft_id, "md": markdown, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
           False)
    call.notes.append(f"Saved as Draft {draft_id} in the graph. Show the markdown to the user; nothing was posted.")
    return call.result({"draft_id": draft_id, "kep": d["kep"], "markdown": markdown})


# ---------- route_question ----------

def route_question(question: str, topic: str, graph_name: str | None = None) -> dict:
    """Who to ask about a topic (owning team leads and experts); records the question routed to them."""
    call = ToolCall("route_question", graph_name)
    best, cands = read._resolve_one(call, topic, ["KEP", "Component", "CodeArea", "Team", "FeatureGate"], MIN_SCORE)
    if best is None:
        call.notes.append("Nothing in the graph matches this topic. Do not name anyone to ask.")
        return call.result({"topic": topic, "closest": cands[:3]}, found=False)
    owners = read.who_owns(best["key"], graph_name=graph_name)
    _absorb(call, owners)
    experts = read.who_knows(best["name"] if best["label"] != "KEP" else best["name"].split(" ", 1)[-1],
                             limit=3, graph_name=graph_name)
    _absorb(call, experts)
    route = []
    for e in experts["data"].get("people", [])[:2]:
        route.append({"login": e["login"], "reason": f"expert (score {e['score']}) on "
                      + ", ".join(x["kep"] for x in e["evidence"][:2])})
    for lead in owners["data"]["leads"]:
        if lead["role"] == "tech_lead" and lead["login"] not in {r["login"] for r in route}:
            route.append({"login": lead["login"], "reason": f"tech lead of {lead['team']}"})
            break
    qid = "route:" + hashlib.sha256(f"{question}|{best['key']}".encode()).hexdigest()[:12]
    call.q("""MERGE (q:Question {id:$id}) SET q.text = $text, q.kind = 'routed', q.routed_at = coalesce(q.routed_at, $at)
              WITH q UNWIND $route AS r
              MATCH (p:Person {login: r.login}) MERGE (q)-[e:ROUTED_TO]->(p) SET e.reason = r.reason""",
           {"id": qid, "text": question, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "route": route},
           False)
    label_key = {"KEP": "id", "Component": "name", "CodeArea": "id", "Team": "id", "FeatureGate": "name"}[best["label"]]
    call.q(f"MATCH (q:Question {{id:$id}}), (x:{best['label']} {{{label_key}:$key}}) MERGE (q)-[:ASKED_ABOUT]->(x)",
           {"id": qid, "key": best["key"]}, False)
    call.notes.append(f"Recorded as Question {qid}, routed to {', '.join(r['login'] for r in route) or 'nobody'}. "
                      "Nothing was sent to anyone.")
    return call.result({"question_id": qid, "topic": best, "ask": route}, found=bool(route))


# ---------- watch ----------

def watch(entity: str, graph_name: str | None = None) -> dict:
    """Follow a KEP, team, component or person: the real-time engine reports changes to it."""
    call = ToolCall("watch", graph_name)
    best, cands = read._resolve_one(call, entity, ["KEP", "Team", "Component", "Person"], MIN_SCORE)
    if best is None:
        call.notes.append("Nothing in the graph matches this. Nothing is being watched.")
        return call.result({"query": entity, "closest": cands[:3]}, found=False)
    key = {"KEP": "id", "Team": "id", "Component": "name", "Person": "login"}[best["label"]]
    wid = f"watch:{best['label']}:{best['key']}"
    call.q(f"""MATCH (x:{best['label']} {{{key}:$key}})
               MERGE (w:Watch {{id:$id}}) SET w.target = $key, w.created_at = coalesce(w.created_at, $at)
               MERGE (w)-[r:WATCHES]->(x) RETURN w, r, x""",
           {"key": best["key"], "id": wid, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    call.notes.append(f"Now watching {best['label']} {best['key']}. Changes appear in the live feed.")
    return call.result({"watch_id": wid, "target": best})
