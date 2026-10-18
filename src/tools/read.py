"""The read tools: fixed, parameterized Cypher over the company graph.

The agent picks a tool and fills in arguments; it never writes Cypher. Each tool resolves names
against the graph first, returns found=False instead of guessing, and reports only what the
graph holds. Every tool returns the shape built by src/tools/base.py: ToolCall.result().
"""
import re

from src.tools.base import ToolCall, parse_as_of, snapshot_cutoff, ts_to_date, valid_at

NOT_ADOPTED = {"withdrawn", "rejected", "replaced", "deferred"}
STOPWORDS = set("""a an the of to in on for and or is are was were be been by with about who what when where why how
which does do did it its this that these those from at as into than then there their they them we our you your
i me my know knows owns own owner owners decided decide decision status still valid current currently replaced
change changed changes since affect affects impact kep keps kubernetes k8s feature proposal enhancement""".split())
DECISION_WORDS = ["withdraw", "reject", "decid", "closing", "close this", "supersed", "replace", "abandon",
                  "deprecat", "graduat", "not moving forward", "no longer", "instead"]


# ---------- resolve ----------

def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,40}", text or "")
    return [w for w in words if w.lower() not in STOPWORDS][:8]


def coverage(words: list[str], text: str) -> float:
    """Share of query keywords that appear (by prefix or close spelling) in a candidate's name/title.
    'quantum teleport scheduler' vs 'PreQueueingHint ... Scheduler ...' = 1/3, so it is a weak match."""
    import difflib
    if not words:
        return 0.0
    cand = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text or "")]
    hit = 0
    for w in words:
        w = w.lower()
        stem = w[:max(4, len(w) - 2)]
        if any(c.startswith(stem) or stem.startswith(c[:max(4, len(c) - 2)]) and len(c) >= 4 for c in cand) \
                or difflib.get_close_matches(w, cand, n=1, cutoff=0.82):
            hit += 1
    return hit / len(words)


def _fulltext_query(words: list[str]) -> str:
    terms = []
    for w in words:
        w = w.replace("-", " ").strip()
        for part in w.split():
            terms.append(f"{part}*" if len(part) >= 4 else part)
            if len(part) >= 6:
                terms.append(f"%{part}%")  # typo-tolerant
    return "|".join(dict.fromkeys(terms))


def resolve(text: str, kinds: list[str] | None = None, limit: int = 5, graph_name: str | None = None,
            _call: ToolCall | None = None) -> dict:
    """Find which graph entities a piece of text refers to. kinds: KEP, Team, Person, Component,
    FeatureGate, Subproject, CodeArea (default: all)."""
    call = _call or ToolCall("resolve", graph_name)
    kinds = kinds or ["KEP", "Team", "Person", "Component", "FeatureGate", "Subproject", "CodeArea"]
    text = (text or "").strip()[:300]
    found: dict[str, dict] = {}

    def add(label, key, name, url, score, via):
        k = f"{label}:{key}"
        if k not in found:
            found[k] = {"label": label, "key": key, "name": name, "url": url, "score": round(score, 3), "via": via}
            return
        old = found[k]
        if via.startswith("semantic") != old["via"].startswith("semantic") and min(score, old["score"]) >= 0.3:
            # keyword match and meaning match agree: more confident than either alone
            old["score"] = round(min(0.99, max(score, old["score"]) + 0.1), 3)
            old["via"] = f"{old['via']} + {via}"
        elif score > old["score"]:
            old.update(score=round(score, 3), via=via)

    # 1) exact ids
    for num in re.findall(r"\bKEP[- ]?(\d{1,5})\b|#(\d{2,5})\b", text, re.I):
        n = num[0] or num[1]
        if "KEP" in kinds:
            for k, title, url in call.q("MATCH (k:KEP {id:$id}) RETURN k.id, k.title, k.url", {"id": f"KEP-{int(n)}"}, False):
                add("KEP", k, title, url, 1.0, "exact id")
    for tok in re.findall(r"[a-z0-9][a-z0-9-]{1,38}", text.lower()):
        if "Team" in kinds and tok.startswith(("sig-", "wg-", "committee-")):
            for t, name, url in call.q("MATCH (t:Team {id:$id}) RETURN t.id, t.name, t.url", {"id": tok}, False):
                add("Team", t, name, url, 1.0, "exact id")
        if "Component" in kinds:
            for (c,) in call.q("MATCH (c:Component {name:$n}) RETURN c.name", {"n": tok}, False):
                add("Component", c, c, None, 0.95, "exact name")
    if "Person" in kinds:
        for at, login in re.findall(r"(@?)([A-Za-z0-9][A-Za-z0-9-]{2,38})", text):
            if not at and login.lower() in STOPWORDS:
                continue
            for (p,) in call.q("MATCH (p:Person {login:$l}) RETURN p.login", {"l": login.lower()}, False):
                add("Person", p, p, f"https://github.com/{p}", 0.95 if at else 0.85, "exact login")

    # 2) full-text over names and titles
    words = _keywords(text)
    ft = _fulltext_query(words)
    fields = {"KEP": "n.title", "Team": "n.name", "Subproject": "n.name", "FeatureGate": "n.name",
              "Component": "n.name"}
    if ft:
        for label, name_expr in fields.items():
            if label not in kinds:
                continue
            key = {"KEP": "n.id", "Team": "n.id", "Subproject": "n.id"}.get(label, "n.name")
            rows = call.q(f"""CALL db.idx.fulltext.queryNodes('{label}', $q) YIELD node AS n, score
                              RETURN {key} AS key, {name_expr} AS name, n.url AS url, score
                              ORDER BY score DESC LIMIT 5""", {"q": ft}, False)
            for key_v, name, url, _score in rows:
                cov = coverage(words, f"{key_v} {name}")
                # tie-break: prefer names that are mostly made of the query's words ("Sidecar Containers"
                # over "Restarting sidecar containers during Pod termination")
                name_words = [w for w in _keywords(name or "") if len(w) > 2]
                precision = coverage(name_words, " ".join(words)) if name_words else 0
                add(label, key_v, name, url, 0.3 + 0.55 * cov + 0.05 * precision, f"full-text ({cov:.0%} of keywords)")
    if "CodeArea" in kinds:
        for w in words:
            if "/" in w or w.lower() in ("kubelet", "scheduler", "kubectl", "apiserver", "proxy"):
                rows = call.q("""MATCH (a:CodeArea) WHERE a.path CONTAINS $w
                                 RETURN a.id, a.path, a.url ORDER BY size(a.path) LIMIT 3""", {"w": w.lower()}, False)
                for a, path, url in rows:
                    add("CodeArea", a, path, url, 0.55, "path match")

    # 3) vector search over KEP design text and comments (when chunks are loaded)
    if "KEP" in kinds and len(text) > 3:
        try:
            from src.load.embed import embed_query
            rows = call.q("""CALL db.idx.vector.queryNodes('Chunk', 'embedding', 12, vecf32($v)) YIELD node AS c, score
                             MATCH (c)-[:CHUNK_OF]->(src)
                             OPTIONAL MATCH (src)-[:ON]->(:Ticket)<-[:TRACKED_BY]-(k1:KEP)
                             OPTIONAL MATCH (src)-[:ON]->(:PR)-[:CHANGES]->(k2:KEP)
                             WITH coalesce(CASE WHEN src:KEP THEN src END, k1, k2) AS k, score
                             WHERE k IS NOT NULL
                             RETURN k.id, k.title, k.url, min(score) AS d ORDER BY d LIMIT 5""",
                          {"v": embed_query(text)}, False)
            for k, title, url, dist in rows:
                # measured on this corpus: related topics 0.11-0.22, nonsense 0.24-0.28 (too close to trust
                # alone), so similarity is a supporting signal; on its own only distance <= ~0.15 counts as found
                add("KEP", k, title, url, max(0.0, min(0.75, 0.75 * (1 - (dist - 0.10) / 0.20))),
                    f"semantic (distance {dist:.2f})")
        except Exception as e:  # noqa: BLE001 - no vector index / no chunks yet: full-text only
            call.notes.append(f"vector search unavailable: {str(e)[:80]}")

    # 4) a distinctive word the graph has never seen ("holographic volume driver") means the thing is not
    #    here, however well the other words match: fuzzy candidates are capped below the match bar
    unknown = _unknown_words(call, words)
    if unknown:
        call.notes.append(f"{', '.join(repr(w) for w in unknown)} appears nowhere in the graph, so no candidate "
                          "below is a real match.")
        for r in found.values():
            if not r["via"].startswith("exact"):
                r["score"] = min(r["score"], 0.45)

    ranked = sorted(found.values(), key=lambda r: -r["score"])[:limit]
    if _call is None:
        call.source([r["url"] for r in ranked])
        if not any(r["score"] >= 0.5 for r in ranked):   # same bar as _resolve_one: weak candidates are not a match
            call.notes.append("Nothing in the graph matches this well; the candidates are only distant guesses.")
        return call.result({"query": text, "candidates": ranked}, found=any(r["score"] >= 0.5 for r in ranked))
    return {"query": text, "candidates": ranked}


def _unknown_words(call: ToolCall, words: list[str]) -> list[str]:
    """Query words (5+ letters) with no full-text hit (prefix or close spelling) anywhere: titles, design-doc
    chunks, comments, components or feature gates. Typos still hit through the fuzzy term."""
    unknown = []
    for w in words:
        if len(w) < 5 or not w.isalpha():
            continue
        q = f"{w.lower()}*|%{w.lower()}%"
        hits = 0
        for label in ("KEP", "Chunk", "Comment", "Component", "FeatureGate"):
            hits += call.q(f"""CALL db.idx.fulltext.queryNodes('{label}', $q) YIELD node
                               RETURN count(node)""", {"q": q}, False)[0][0]
            if hits:
                break
        if not hits:
            unknown.append(w)
    return unknown


def _resolve_one(call: ToolCall, text: str, kinds: list[str], min_score: float = 0.5):
    cands = resolve(text, kinds=kinds, _call=call)["candidates"]
    best = cands[0] if cands and cands[0]["score"] >= min_score else None
    return best, cands


# ---------- trace_decision ----------

def trace_decision(kep_or_query: str, as_of: str | None = None, graph_name: str | None = None) -> dict:
    """Status, history, replacement, approvers and evidence for one KEP (or the best text match)."""
    call = ToolCall("trace_decision", graph_name)
    t, call.as_of_label = parse_as_of(call.g, as_of)
    best, cands = _resolve_one(call, kep_or_query, ["KEP"], min_score=0.6)
    if best is None:
        call.notes.append("No KEP in the graph matches this. Do not describe it as a decision that was made.")
        if cands:
            call.notes.append("Closest KEPs: " + "; ".join(f"{c['key']} {c['name']}" for c in cands[:3]))
        return call.result({"query": kep_or_query, "closest": cands[:3]}, found=False)
    if not best["via"].startswith("exact id"):
        call.notes.append(f"Matched by {best['via']}: '{kep_or_query}' -> {best['key']} {best['name']}. "
                          f"Say which KEP you matched; it may not be what the user meant.")
    kid = best["key"]
    p = {"id": kid, "as_of": t}
    k, team = call.q("MATCH (k:KEP {id:$id}) OPTIONAL MATCH (t:Team)-[o:OWNS]->(k) RETURN k, t, o", p)[0][:2]
    kp = k.properties
    history = call.q("""MATCH (k:KEP {id:$id})-[e:HAD_STATUS]->(s:Status)
                        RETURN s.name, e.valid_from, e.valid_to, e.source_url, e, s ORDER BY e.valid_from""", p)
    status_at = [h[0] for h in history if (t is None and h[2] is None) or
                 (t is not None and h[1] <= t and (h[2] is None or h[2] > t))]
    newer = call.q(f"""MATCH (n:KEP)-[s:SUPERSEDES]->(k:KEP {{id:$id}})
                       {"WHERE n.created_ts <= $as_of" if t else ""}
                       RETURN n.id, n.title, n.status, s.verified, s.source, n, s""", p)
    older = call.q("MATCH (k:KEP {id:$id})-[s:SUPERSEDES]->(o:KEP) RETURN o.id, o.title, o.status, o, s", p)
    approvers = call.q(f"""MATCH (pp:Person)-[a:APPROVES]->(k:KEP {{id:$id}}) WHERE {valid_at('a', t)}
                           RETURN pp.login, pp, a""", p)
    authors = call.q("MATCH (pp:Person)-[a:AUTHORED]->(k:KEP {id:$id}) RETURN pp.login, pp, a", p)
    # two steps so FalkorDB starts from the indexed ticket, not from all comments
    tickets = [r[0] for r in call.q("MATCH (k:KEP {id:$id})-[:TRACKED_BY]->(t:Ticket) RETURN t.id", p, False)]
    # the text filter must come after WITH: inside the MATCH it made FalkorDB filter too early (1.6 s vs 5 ms)
    evidence = call.q("""MATCH (t:Ticket) WHERE t.id IN $tickets
                         MATCH (t)<-[:ON]-(c:Comment)
                         WITH c WHERE any(word IN $words WHERE toLower(c.body) CONTAINS word)
                         MATCH (c)<-[:WROTE]-(w:Person)
                         RETURN c.url, w.login, c.at_ts, substring(c.body, 0, 400), c, w
                         ORDER BY c.at_ts DESC LIMIT 4""", {"tickets": tickets, "words": DECISION_WORDS}) if tickets else []
    decision = call.q("""MATCH (d:Decision)-[:DECIDED]->(k:KEP {id:$id})
                         OPTIONAL MATCH (d)-[e:EVIDENCED_BY]->(c:Comment)<-[:WROTE]-(w:Person)
                         RETURN d.summary, d.rationale, d.alternatives, d.confidence, d.stale,
                                collect({url: c.url, by: w.login, quote: e.quote}), d.outcome_reason,
                                d.readme_quotes, d.readme_url, d.verified, d""", p)

    status = status_at[0] if status_at else None
    data = {
        "kep": kid, "title": kp.get("title"), "owning_team": team.properties.get("id") if team else None,
        "status": status, "stage": kp.get("stage"), "latest_milestone": kp.get("latest_milestone"),
        "status_history": [{"status": h[0], "from": ts_to_date(h[1]), "to": ts_to_date(h[2]), "source": h[3]}
                           for h in history],
        "replaced_by": [{"kep": r[0], "title": r[1], "status": r[2], "verified": r[3]} for r in newer],
        "replaces": [{"kep": r[0], "title": r[1], "status": r[2]} for r in older],
        "approvers": [r[0] for r in approvers], "authors": [r[0] for r in authors],
        "evidence_comments": [{"url": e[0], "by": e[1], "date": ts_to_date(e[2]), "excerpt": e[3]} for e in evidence],
        "rationale": None,
    }
    if decision:
        d = decision[0]
        quotes = [q for q in d[5] if q["url"]] + [{"url": d[8], "by": "design doc", "quote": q} for q in d[7] or []]
        data["rationale"] = {"summary": d[0], "rationale": d[1], "outcome_reason": d[6], "alternatives": d[2],
                             "confidence": d[3], "quotes": quotes}
        call.notes.append("The rationale was written by an LLM from the sources; each quote was checked word for "
                          "word against its source." + ("" if d[9] else " Some of its quotes failed that check and "
                                                        "were dropped, so treat it with care."))
        if d[3] == "low":
            call.notes.append("The sources say little about why; say that the reason is not well documented.")
        if d[4]:
            call.stale.append(f"New discussion arrived after the explanation of {kid} was written; it may be out of date "
                              "until it is re-checked.")
        call.source([q["url"] for q in quotes])
    call.source(kp.get("url"), kp.get("readme_url"), kp.get("issue_url"), [h[3] for h in history],
                [e[0] for e in evidence])
    if status is None:
        call.notes.append(f"{kid} has no recorded status {'at ' + call.as_of_label if t else ''}.")
    elif status in NOT_ADOPTED:
        call.notes.append(f"{kid} was NOT adopted: its status is '{status}'. Never describe it as implemented.")
    elif status in ("provisional", "implementable"):
        call.notes.append(f"{kid} is '{status}': agreed or proposed, not yet implemented.")
    if newer:
        unverified = [r[0] for r in newer if not r[3]]
        call.notes.append(f"{kid} is replaced by {', '.join(r[0] for r in newer)}. Say this first."
                          + (f" Not yet confirmed by a person: {', '.join(unverified)}." if unverified else ""))
    if data["rationale"] is None:
        call.notes.append("No extracted rationale yet; use the evidence comments and status history.")
    return call.result(data)


# ---------- who_owns ----------

def who_owns(target: str, as_of: str | None = None, graph_name: str | None = None) -> dict:
    """Owning team(s), current leads and code approvers for a component, code path, KEP, feature gate or team."""
    call = ToolCall("who_owns", graph_name)
    t, call.as_of_label = parse_as_of(call.g, as_of)
    best, cands = _resolve_one(call, target, ["Component", "CodeArea", "KEP", "FeatureGate", "Team", "Subproject"])
    if best is None:
        call.notes.append("Nothing in the graph matches this. Do not guess an owner.")
        return call.result({"query": target, "closest": cands[:3]}, found=False)
    label, key = best["label"], best["key"]
    p = {"key": key, "as_of": t}
    data = {"matched": best, "teams": [], "code_areas": [], "kep_approvers": []}
    if label == "Component":
        rows = call.q("""MATCH (c:Component {name:$key})-[i:IMPLEMENTED_IN]->(a:CodeArea)
                         OPTIONAL MATCH (a)<-[o:OWNS_CODE]-(sp:Subproject)<-[h:HAS_SUBPROJECT]-(tm:Team)
                         OPTIONAL MATCH (pp:Person)-[r:APPROVES_CODE]->(a) WHERE r.former IS NULL
                         RETURN a.id, a.url, collect(DISTINCT tm.id), collect(DISTINCT pp.login)[..10], c, i, a, o, sp, h, tm""", p)
        if not rows:  # component without mapped code: owners of the KEPs that affect it
            rows2 = call.q("""MATCH (tm:Team)-[o:OWNS]->(k:KEP)-[g:GATED_BY]->(f:FeatureGate)-[af:AFFECTS]->(c:Component {name:$key})
                              RETURN tm.id, count(DISTINCT k) AS n ORDER BY n DESC LIMIT 5""", p, False)
            data["teams"] = [r[0] for r in rows2]
            call.notes.append(f"No code area mapped for '{key}'; teams are those owning KEPs that affect it.")
        for a, url, teams, approvers, *_ in rows:
            data["code_areas"].append({"area": a, "url": url, "teams": teams, "approvers": approvers})
            data["teams"] += [x for x in teams if x not in data["teams"]]
    elif label == "CodeArea":
        rows = call.q("""MATCH (a:CodeArea {id:$key})
                         OPTIONAL MATCH (a)<-[o:OWNS_CODE]-(sp:Subproject)<-[h:HAS_SUBPROJECT]-(tm:Team)
                         OPTIONAL MATCH (pp:Person)-[r:APPROVES_CODE]->(a) WHERE r.former IS NULL
                         RETURN a.id, a.url, collect(DISTINCT tm.id), collect(DISTINCT pp.login)[..10], a, o, sp, h, tm""", p)
        for a, url, teams, approvers, *_ in rows:
            data["code_areas"].append({"area": a, "url": url, "teams": teams, "approvers": approvers})
            data["teams"] += teams
    elif label in ("KEP", "FeatureGate"):
        match = "MATCH (k:KEP {id:$key})" if label == "KEP" else "MATCH (k:KEP)-[g:GATED_BY]->(:FeatureGate {name:$key})"
        rows = call.q(f"""{match} MATCH (tm:Team)-[o:OWNS]->(k)
                          OPTIONAL MATCH (pp:Person)-[a:APPROVES]->(k) WHERE {valid_at('a', t)}
                          RETURN k.id, tm.id, collect(DISTINCT pp.login), k, tm, o""", p)
        for kid, team, approvers, *_ in rows:
            data["teams"].append(team)
            data["kep_approvers"].append({"kep": kid, "approvers": approvers})
    elif label in ("Team", "Subproject"):
        tid = key if label == "Team" else key.split("/")[0]
        data["teams"] = [tid]
    data["teams"] = list(dict.fromkeys(x for x in data["teams"] if x))
    leads = call.q(f"""MATCH (pp:Person)-[l:LEADS]->(tm:Team) WHERE tm.id IN $teams AND {valid_at('l', t)}
                       RETURN tm.id, pp.login, l.role, pp, l, tm ORDER BY tm.id, l.role""", {**p, "teams": data["teams"]})
    data["leads"] = [{"team": r[0], "login": r[1], "role": r[2]} for r in leads]
    for tm in data["teams"]:
        call.source(f"https://github.com/kubernetes/community/tree/master/{tm}")
    return call.result(data)


# ---------- who_knows ----------

def who_knows(topic: str, limit: int = 5, graph_name: str | None = None) -> dict:
    """People who know most about a topic, scored from authorship, approvals, reviews and discussion."""
    call = ToolCall("who_knows", graph_name)
    cands = resolve(topic, kinds=["KEP", "Component", "FeatureGate", "Team"], limit=10, _call=call)["candidates"]
    keps = [c["key"] for c in cands if c["label"] == "KEP" and c["score"] >= 0.5][:8]
    comps = [c["key"] for c in cands if c["label"] in ("Component", "FeatureGate")]
    if comps:  # widen with KEPs that affect a matched component or gate
        rows = call.q("""MATCH (k:KEP)-[:GATED_BY]->(f:FeatureGate)-[:AFFECTS]->(c:Component)
                         WHERE c.name IN $c OR f.name IN $c
                         RETURN k.id, count(*) AS n ORDER BY n DESC LIMIT 8""", {"c": comps}, False)
        keps += [r[0] for r in rows if r[0] not in keps]
    if not keps:
        call.notes.append("No decisions in the graph match this topic. Do not name experts.")
        return call.result({"topic": topic, "people": []}, found=False)
    p = {"keps": keps[:12]}
    score: dict[str, dict] = {}

    def bump(login, pts, kid, why):
        s = score.setdefault(login, {"login": login, "score": 0, "evidence": {}})
        s["score"] += pts
        s["evidence"].setdefault(kid, []).append(why)

    weights = {"AUTHORED": 3, "APPROVES": 3, "REVIEWS": 2}
    for login, rel, kid in call.q("""MATCH (pp:Person)-[r:AUTHORED|APPROVES|REVIEWS]->(k:KEP) WHERE k.id IN $keps
                                     RETURN pp.login, type(r), k.id""", p, False):
        bump(login, weights[rel], kid, rel.lower())
    for login, kid, n in call.q("""MATCH (pp:Person)-[:REVIEWED {state:'APPROVED'}]->(:PR)-[:CHANGES]->(k:KEP)
                                   WHERE k.id IN $keps RETURN pp.login, k.id, count(*)""", p, False):
        bump(login, 2, kid, f"approved {n} PR(s)")
    for login, kid, n in call.q("""MATCH (k:KEP)-[:TRACKED_BY]->(:Ticket)<-[:ON]-(:Comment)<-[:WROTE]-(pp:Person)
                                   WHERE k.id IN $keps RETURN pp.login, k.id, count(*)""", p, False):
        bump(login, min(n, 5) * 0.2, kid, f"{n} comment(s)")
    top = sorted(score.values(), key=lambda s: -s["score"])[:limit]
    people = [{"login": s["login"], "score": round(s["score"], 1),
               "evidence": [{"kep": k, "roles": v} for k, v in sorted(s["evidence"].items(), key=lambda kv: -len(kv[1]))[:4]]}
              for s in top]
    call.q("""MATCH (pp:Person)-[r:AUTHORED|APPROVES|REVIEWS]->(k:KEP) WHERE pp.login IN $people AND k.id IN $keps
              RETURN pp, r, k""", {"people": [x["login"] for x in people], "keps": p["keps"]})
    call.notes.append(f"Based on {len(p['keps'])} related KEPs: {', '.join(p['keps'])}. "
                      "Score = 3 per authored/approved KEP, 2 per KEP review or approved PR, small credit per comment.")
    return call.result({"topic": topic, "related_keps": p["keps"], "people": people})


# ---------- impact_of ----------

def impact_of(entity: str, depth: int = 2, graph_name: str | None = None) -> dict:
    """What a KEP (or component/feature gate) affects: components, code, owners and related decisions."""
    call = ToolCall("impact_of", graph_name)
    depth = max(1, min(int(depth), 3))
    best, cands = _resolve_one(call, entity, ["KEP", "Component", "FeatureGate"])
    if best is None:
        call.notes.append("Nothing in the graph matches this.")
        return call.result({"query": entity, "closest": cands[:3]}, found=False)
    p = {"key": best["key"]}
    data = {"matched": best}
    if best["label"] == "KEP":
        comps = call.q("""MATCH (k:KEP {id:$key})-[g:GATED_BY]->(f:FeatureGate)-[a:AFFECTS]->(c:Component)
                          OPTIONAL MATCH (c)-[i:IMPLEMENTED_IN]->(ca:CodeArea)
                          OPTIONAL MATCH (pp:Person)-[r:APPROVES_CODE]->(ca) WHERE r.former IS NULL
                          RETURN f.name, c.name, collect(DISTINCT ca.id), collect(DISTINCT pp.login)[..8], k, g, f, a, c""", p)
        related = call.q(f"""MATCH path=(k:KEP {{id:$key}})-[:SEE_ALSO|SUPERSEDES*1..{depth}]-(o:KEP) WHERE o <> k
                             RETURN o.id, o.title, o.status, min(length(path)) AS hops, path ORDER BY hops, o.id LIMIT 15""", p)
        teams = call.q("""MATCH (tm:Team)-[r:OWNS|PARTICIPATES_IN]->(k:KEP {id:$key}) RETURN tm.id, type(r), tm, r, k""", p)
        by_comp, by_kep = {}, {}
        for gate, comp, areas, approvers, *_ in comps:  # one row per (gate, component): group by component
            c = by_comp.setdefault(comp, {"component": comp, "via_gates": [], "code_areas": areas,
                                          "code_approvers": approvers})
            c["via_gates"].append(gate)
        for kid, title, status, hops, _ in related:     # one row per path: keep the shortest per KEP
            if kid not in by_kep or hops < by_kep[kid]["hops"]:
                by_kep[kid] = {"kep": kid, "title": title, "status": status, "hops": hops}
        data.update({
            "feature_gates": sorted({r[0] for r in comps}),
            "components": list(by_comp.values()),
            "related_keps": sorted(by_kep.values(), key=lambda r: (r["hops"], r["kep"])),
            "teams": [{"team": r[0], "role": "owner" if r[1] == "OWNS" else "participant"} for r in teams],
        })
        if not comps:
            call.notes.append("This KEP declares no feature gates, so no component impact is recorded.")
    else:
        keps = call.q("""MATCH (k:KEP)-[g:GATED_BY]->(f:FeatureGate)-[a:AFFECTS]->(c:Component)
                         WHERE c.name = $key OR f.name = $key
                         RETURN k.id, k.title, k.status, f.name, k, g, f, a, c ORDER BY k.id DESC LIMIT 25""", p)
        data["keps"] = [{"kep": r[0], "title": r[1], "status": r[2], "feature_gate": r[3]} for r in keps]
    return call.result(data)


# ---------- timeline / what_changed ----------

def timeline(kep: str, as_of: str | None = None, graph_name: str | None = None) -> dict:
    """Every recorded change to a KEP, oldest first, plus its state at as_of."""
    call = ToolCall("timeline", graph_name)
    t, call.as_of_label = parse_as_of(call.g, as_of)
    best, cands = _resolve_one(call, kep, ["KEP"], min_score=0.6)
    if best is None:
        return call.result({"query": kep, "closest": cands[:3]}, found=False)
    p = {"id": best["key"], "as_of": t}
    events = call.q(f"""MATCH (e:Event)-[r:TOUCHED]->(k:KEP {{id:$id}}) {"WHERE e.occurred_ts <= $as_of" if t else ""}
                        RETURN e.occurred_at, e.kind, e.summary, e.url, e, r, k ORDER BY e.occurred_ts""", p)
    targets = call.q(f"""MATCH (k:KEP {{id:$id}})-[e:TARGETS]->(r:Release) WHERE {valid_at('e', t)}
                         RETURN e.stage, r.version ORDER BY r.version""", p, False)
    data = {"kep": best["key"], "title": best["name"],
            "changes": [{"date": e[0][:10], "kind": e[1], "summary": e[2], "source": e[3]} for e in events],
            "targets": [{"stage": s, "release": v} for s, v in targets]}
    call.source([e[3] for e in events])
    return call.result(data)


def what_changed(since: str, team: str | None = None, kep: str | None = None, limit: int = 30,
                 graph_name: str | None = None) -> dict:
    """Changes recorded since a date or release, optionally for one team or KEP, newest first."""
    call = ToolCall("what_changed", graph_name)
    t, label = parse_as_of(call.g, since)
    if t is None:
        raise ValueError("since must be a date or release, e.g. 2026-09-01 or v1.34")
    call.as_of_label = f"since {label}"
    limit = max(1, min(int(limit), 100))
    where, p = ["e.occurred_ts >= $since"], {"since": t, "limit": limit}
    if team:
        where.append("((x:KEP AND (:Team {id:$team})-[:OWNS]->(x)) OR (x:Team AND x.id = $team))")
        p["team"] = team.lower()
    if kep:
        best, _ = _resolve_one(call, kep, ["KEP"], 0.6)
        if best is None:
            return call.result({"query": kep}, found=False)
        where.append("x.id = $kep")
        p["kep"] = best["key"]
    rows = call.q(f"""MATCH (e:Event)-[r:TOUCHED]->(x) WHERE {' AND '.join(where)}
                      RETURN e.occurred_at, e.kind, e.summary, e.url, e, r, x ORDER BY e.occurred_ts DESC LIMIT $limit""", p)
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r[1]] = by_kind.get(r[1], 0) + 1
    cutoff = snapshot_cutoff(call.g)
    if cutoff:
        call.notes.append(f"The graph holds changes observed up to {cutoff[:10]} plus any live updates since.")
    call.source([r[3] for r in rows])
    return call.result({"since": label, "count": len(rows), "by_kind": by_kind,
                        "changes": [{"date": r[0][:10], "kind": r[1], "summary": r[2], "source": r[3]} for r in rows]},
                       found=bool(rows))


# ---------- connect / influence ----------

CONNECT_RELS = ["LEADS", "OWNS", "PARTICIPATES_IN", "AUTHORED", "REVIEWS", "APPROVES", "APPROVES_CODE",
                "REVIEWS_CODE", "OWNS_CODE", "HAS_SUBPROJECT", "IMPLEMENTED_IN", "GATED_BY", "AFFECTS",
                "SEE_ALSO", "SUPERSEDES", "TRACKED_BY", "CHANGES", "OPENED", "REVIEWED", "REVIEWED_FOR"]


def connect(a: str, b: str, graph_name: str | None = None) -> dict:
    """Shortest meaningful connection between two entities (people, teams, KEPs, components), at most 4 hops."""
    call = ToolCall("connect", graph_name)
    kinds = ["Person", "KEP", "Team", "Component", "FeatureGate", "CodeArea"]
    ea, ca = _resolve_one(call, a, kinds)
    eb, cb = _resolve_one(call, b, kinds)
    if ea is None or eb is None:
        call.notes.append(f"Could not find {'both ends' if ea is None and eb is None else repr(a if ea is None else b)}.")
        return call.result({"a": ca[:3], "b": cb[:3]}, found=False)
    key = {"Person": "login", "Component": "name", "FeatureGate": "name"}
    qa = f"MATCH (x:{ea['label']} {{{key.get(ea['label'], 'id')}: $a}})"
    qb = f"MATCH (y:{eb['label']} {{{key.get(eb['label'], 'id')}: $b}})"
    rows = call.q(f"""{qa} {qb}
                      CALL algo.SPpaths({{sourceNode: x, targetNode: y, relTypes: $rels, relDirection: 'both', maxLen: 4}})
                      YIELD path RETURN path, length(path) LIMIT 1""", {"a": ea["key"], "b": eb["key"], "rels": CONNECT_RELS})
    if not rows:
        call.notes.append("No connection within 4 hops through ownership, authorship, review or decision links.")
        return call.result({"a": ea, "b": eb, "hops": None, "steps": []}, found=False)
    path = rows[0][0]
    nodes = [call.path.nodes[n.id] for n in path.nodes()]
    steps = []
    for i, e in enumerate(path.edges()):
        step = {"from": nodes[i]["name"], "rel": e.relation, "to": nodes[i + 1]["name"]}
        if e.properties.get("valid_to"):
            step["until"] = ts_to_date(e.properties["valid_to"])  # a past relationship, not a current one
        if e.properties.get("former"):
            step["former"] = True
        steps.append(step)
    return call.result({"a": ea, "b": eb, "hops": rows[0][1], "steps": steps})


def influence(team: str | None = None, metric: str = "pagerank", limit: int = 10, graph_name: str | None = None) -> dict:
    """Most influential reviewers (PageRank) or bridges between groups (betweenness) on the reviewer graph."""
    call = ToolCall("influence", graph_name)
    limit = max(1, min(int(limit), 25))
    if metric not in ("pagerank", "betweenness"):
        metric = "pagerank"
    proc = ("CALL algo.pageRank('Person', 'REVIEWED_FOR') YIELD node, score" if metric == "pagerank" else
            "CALL algo.betweenness({nodeLabels:['Person'], relationshipTypes:['REVIEWED_FOR']}) YIELD node, score")
    if team:
        rows = call.q(f"""{proc}
                          WITH node, score
                          MATCH (node)-[:AUTHORED|APPROVES|REVIEWS]->(:KEP)<-[:OWNS]-(tm:Team {{id:$team}})
                          WITH node, score, tm RETURN DISTINCT node.login, score, node ORDER BY score DESC LIMIT $limit""",
                      {"team": team.lower(), "limit": limit})
    else:
        rows = call.q(f"{proc} RETURN node.login, score, node ORDER BY score DESC LIMIT $limit", {"limit": limit})
    call.notes.append("Computed on the reviewer graph: an edge A->B means A reviewed or commented on B's work."
                      + (" Betweenness finds people who bridge otherwise separate groups." if metric == "betweenness" else ""))
    for r in rows:
        call.source(f"https://github.com/{r[0]}")
    return call.result({"metric": metric, "team": team, "people": [{"login": r[0], "score": round(r[1], 6)} for r in rows]},
                       found=bool(rows))


# ---------- documents and threads: what was written, not only how things are linked ----------

PASSAGE_MAX, BODY_MAX, COMMENT_MAX, SECTION_MAX = 700, 3000, 800, 6000
# words almost every passage contains: they cannot show that a passage is about the question's topic
GENERIC = set("""pod pods container containers node nodes cluster clusters api apis user users support supported
rejected withdrawn approved accepted merged discussed discussion discuss said say says reviewer reviewers comment
comments design doc docs issue issues pr prs""".split())
_THREAD_URL = re.compile(r"github\.com/kubernetes/enhancements/(?:issues|pull)/(\d+)")


def _thread_number(ref: str) -> int | None:
    m = _THREAD_URL.search(ref or "") or re.fullmatch(r"\s*(?:enhancements)?#?(\d{1,6})\s*", ref or "")
    return int(m.group(1)) if m else None


def search(query: str, kep: str | None = None, kind: str | None = None, as_of: str | None = None, limit: int = 8,
           graph_name: str | None = None) -> dict:
    """Find passages in KEP design docs and in issue/PR discussions that talk about something.
    kind: doc | discussion (default: both). kep narrows to one KEP's doc and its issue/PR threads."""
    import numpy as np
    from src.load.embed import embed_query
    call = ToolCall("search", graph_name)
    t, call.as_of_label = parse_as_of(call.g, as_of)
    limit = max(1, min(int(limit or 8), 12))
    kinds = {"doc": ["kep"], "discussion": ["comment"]}.get((kind or "").lower(), ["kep", "comment"])
    words = _keywords(query)
    unknown = _unknown_words(call, words)
    if unknown:   # a distinctive word no document or comment ever uses: the topic is not in the graph
        call.notes.append(f"{', '.join(repr(w) for w in unknown)} appears nowhere in the design docs or "
                          "discussions, so nothing here is about this.")
        return call.result({"query": query, "kep": kep, "passages": []}, found=False)
    topic = [w for w in words if w.lower() not in GENERIC]
    vec = embed_query(query)
    hits: dict[str, dict] = {}

    if kep:
        best, cands = _resolve_one(call, kep, ["KEP"], min_score=0.6)
        if best is None:
            return call.result({"query": query, "kep": kep, "closest": cands[:3]}, found=False)
        kid = best["key"]
        # every chunk of this KEP's doc and of the comments on its tracking issue and PRs, ranked by similarity
        rows = call.q("""MATCH (k:KEP {id:$id})
                         OPTIONAL MATCH (d:Chunk)-[:CHUNK_OF]->(k)
                         WITH k, collect(d) AS docs
                         OPTIONAL MATCH (k)-[:TRACKED_BY]->(t:Ticket)
                         OPTIONAL MATCH (p:PR)-[:CHANGES]->(k)
                         WITH docs, collect(DISTINCT t) + collect(DISTINCT p) AS threads
                         UNWIND threads AS th
                         OPTIONAL MATCH (ch:Chunk)-[:CHUNK_OF]->(c:Comment)-[:ON]->(th)
                         WHERE $as_of IS NULL OR c.at_ts <= $as_of
                         WITH docs, collect(DISTINCT ch) AS talk
                         UNWIND docs + talk AS x
                         RETURN DISTINCT x.id, x.kind, x.text, x.url, x.embedding""", {"id": kid, "as_of": t}, False)
        q = np.asarray(vec, dtype=np.float32)
        for cid, ck, text, url, emb in rows:
            if ck in kinds and emb is not None:
                e = np.asarray(emb, dtype=np.float32)
                dist = 1 - float(q @ e / (np.linalg.norm(q) * np.linalg.norm(e) + 1e-9))
                hits[cid] = {"id": cid, "kind": ck, "text": text, "url": url, "score": dist}
    else:
        kid = None
        rows = call.q("""CALL db.idx.vector.queryNodes('Chunk', 'embedding', 60, vecf32($v)) YIELD node, score
                         RETURN node.id, node.kind, node.text, node.url, score""", {"v": vec}, False)
        for cid, ck, text, url, dist in rows:
            if ck in kinds:
                hits[cid] = {"id": cid, "kind": ck, "text": text, "url": url, "score": float(dist)}
        # exact words matter too (names, flags, error strings): add full-text matches the vectors missed
        if words and "comment" in kinds:
            for cid, body, url in call.q("""CALL db.idx.fulltext.queryNodes('Comment', $q) YIELD node, score
                                            RETURN node.id, node.body, node.url ORDER BY score DESC LIMIT 6""",
                                         {"q": _fulltext_query(words)}, False):
                if coverage(words, body) >= 0.6:
                    hits.setdefault(f"chunk:{cid}", {"id": f"chunk:{cid}", "kind": "comment", "text": body,
                                                     "url": url, "score": 0.5})

    ranked = sorted(hits.values(), key=lambda h: h["score"])
    # who and when: one query for the comment passages, one for the doc passages
    ids = [h["id"] for h in ranked[:limit * 3]]
    meta = {r[0]: r[1:] for r in call.q("""
        UNWIND $ids AS cid MATCH (ch:Chunk {id: cid})-[:CHUNK_OF]->(c:Comment)-[:ON]->(th)
        OPTIONAL MATCH (p:Person)-[:WROTE]->(c)
        RETURN cid, p.login AS who, c.at_ts AS ts, th.id AS place, th.title AS title, th AS node
        UNION
        UNWIND $ids AS cid MATCH (ch:Chunk {id: cid})-[:CHUNK_OF]->(k:KEP)
        RETURN cid, null AS who, k.created_ts AS ts, k.id AS place, k.title AS title, k AS node""", {"ids": ids})}
    passages = []
    for h in ranked:
        who, ts, where, title, _ = meta.get(h["id"], (None, None, None, None, None))
        if t is not None and ts is not None and ts > t:
            continue
        # a design-doc chunk can be just "KEP-N Title — Heading:" with nothing under it
        if h["kind"] == "kep" and len((h["text"] or "").split(":", 1)[-1].strip()) < 60:
            continue
        # similar in meaning is not enough: the passage must share most of the question's distinctive words
        if not kid and len(topic) >= 2 and coverage(topic, h["text"]) < 0.6:
            continue
        passages.append({"text": (h["text"] or "")[:PASSAGE_MAX], "type": "design doc" if h["kind"] == "kep" else "discussion",
                         "where": f"{where} {title or ''}".strip() if where else None, "by": who,
                         "date": ts_to_date(ts) if h["kind"] == "comment" else None, "url": h["url"]})
        if len(passages) >= limit:
            break
    call.source([p["url"] for p in passages])
    if not passages:
        call.notes.append("No passage in the design docs or discussions matches this.")
    else:
        call.notes.append("Passages are matched by meaning and by words; check that each one really answers the "
                          "question before using it, and quote only what it says.")
    return call.result({"query": query, "kep": kid, "passages": passages}, found=bool(passages))


def read_thread(ref: str, as_of: str | None = None, graph_name: str | None = None) -> dict:
    """One issue or pull request in kubernetes/enhancements: what it is, who opened it, and the discussion.
    ref: '#4255', a GitHub issue/PR URL, or a KEP id (its tracking issue)."""
    call = ToolCall("read_thread", graph_name)
    t, call.as_of_label = parse_as_of(call.g, as_of)
    n = _thread_number(ref)
    if n is None:
        best, cands = _resolve_one(call, ref, ["KEP"], min_score=0.6)
        if best is None:
            return call.result({"ref": ref, "closest": cands[:3]}, found=False)
        rows = call.q("MATCH (:KEP {id:$id})-[:TRACKED_BY]->(t:Ticket) RETURN t.number", {"id": best["key"]}, False)
        if not rows:
            call.notes.append(f"{best['key']} has no tracking issue in the graph.")
            return call.result({"ref": ref, "kep": best["key"]}, found=False)
        n = rows[0][0]
    tid = f"enhancements#{n}"
    head = call.q("""MATCH (x) WHERE (x:Ticket OR x:PR) AND x.id = $id
                     OPTIONAL MATCH (a:Person)-[:OPENED]->(x)
                     OPTIONAL MATCH (x)-[:CHANGES]->(k1:KEP)
                     OPTIONAL MATCH (k2:KEP)-[:TRACKED_BY]->(x)
                     RETURN labels(x)[0], x.number, x.title, x.state, x.merged, x.created_ts, x.closed_ts, x.labels,
                            x.body, x.url, a.login, collect(DISTINCT k1.id) + collect(DISTINCT k2.id), x, a""",
                  {"id": tid})
    if not head or (t is not None and (head[0][5] or 0) > t):
        call.notes.append(f"Issue or PR #{n} is not in the graph"
                          + (" at that date." if head else ". The graph holds the threads of the SIGs it covers."))
        return call.result({"ref": ref, "number": n}, found=False)
    label, num, title, state, merged, created, closed, labels, body, url, author, keps = head[0][:12]
    comments = call.q("""MATCH (c:Comment)-[:ON]->(x {id:$id}) WHERE $as_of IS NULL OR c.at_ts <= $as_of
                         OPTIONAL MATCH (p:Person)-[:WROTE]->(c)
                         RETURN p.login, c.at_ts, c.kind, c.body, c.url ORDER BY c.at_ts""",
                      {"id": tid, "as_of": t}, False)
    shown = comments if len(comments) <= 16 else comments[:8] + comments[-8:]
    data = {"thread": tid, "kind": "pull request" if label == "PR" else "issue", "number": num, "title": title,
            "state": ("merged" if merged else state) if label == "PR" else state, "opened_by": author,
            "opened": ts_to_date(created), "closed": ts_to_date(closed) if closed and (t is None or closed <= t) else None,
            "labels": labels or [], "keps": sorted(k for k in keps if k), "body": (body or "")[:BODY_MAX],
            "comments_total": len(comments),
            "comments": [{"by": c[0], "date": ts_to_date(c[1]), "kind": c[2], "text": (c[3] or "")[:COMMENT_MAX],
                          "url": c[4]} for c in shown]}
    if len(comments) > len(shown):
        data["comments_omitted"] = len(comments) - len(shown)
        call.notes.append(f"Showing the first 8 and last 8 of {len(comments)} comments; use search with this KEP "
                          "to find a specific point in the middle.")
    if label == "PR":
        reviews = call.q("""MATCH (p:Person)-[r:REVIEWED]->(:PR {id:$id}) WHERE $as_of IS NULL OR r.at <= $as_of
                            RETURN p.login, r.state, r.at ORDER BY r.at""", {"id": tid, "as_of": t}, False)
        verdicts = {}
        for login, st, _ in reviews:
            verdicts[login] = st   # the reviewer's last verdict counts
        data["reviews"] = {s.lower(): sorted(l for l, v in verdicts.items() if v == s)
                           for s in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED") if s in verdicts.values()}
    call.source(url, [c["url"] for c in data["comments"]])
    return call.result(data)


def _anchor(heading: str) -> str:
    """GitHub's anchor for a markdown heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _sections(text: str) -> list[dict]:
    out, fence = [], False
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,4})\s+(.+?)\s*#*\s*$", line)
        if m:
            out.append({"level": len(m.group(1)), "heading": re.sub(r"[`*_]", "", m.group(2)), "line": i})
    return out


def read_kep(kep: str, section: str | None = None, as_of: str | None = None, graph_name: str | None = None) -> dict:
    """A KEP's design document (README) as of the graph's snapshot: its table of contents, or one section."""
    import difflib
    from src.extract.prompt import ENH, cutoff_commit, readme_at
    call = ToolCall("read_kep", graph_name)
    t, call.as_of_label = parse_as_of(call.g, as_of)
    best, cands = _resolve_one(call, kep, ["KEP"], min_score=0.6)
    if best is None:
        return call.result({"query": kep, "closest": cands[:3]}, found=False)
    row = call.q("MATCH (k:KEP {id:$id}) RETURN k.path, k.readme_url, k.title, k.status, k", {"id": best["key"]})
    path, readme_url, title, status = row[0][:4]
    if not path or not (ENH / ".git").exists():
        call.notes.append("The design document is not available on this server.")
        return call.result({"kep": best["key"]}, found=False)
    when = ts_to_date(t) if t is not None else snapshot_cutoff(call.g)
    try:
        commit = cutoff_commit(f"{when}T00:00:00Z" if when and len(when) == 10 else when)
    except Exception:  # noqa: BLE001 - no commit before that date
        commit = None
    text = readme_at(commit, path) if commit else None
    if not text:
        call.notes.append(f"{best['key']} has no design document at that date.")
        return call.result({"kep": best["key"]}, found=False)
    secs = _sections(text)
    lines = text.splitlines()
    data = {"kep": best["key"], "title": title, "status": status, "document_as_of": (when or "")[:10]}
    if not section:
        data["sections"] = [("  " * (s["level"] - 2) if s["level"] > 2 else "") + s["heading"] for s in secs if s["level"] >= 2]
        data["note"] = "Ask for one section by name to read it."
        call.source(readme_url)
        return call.result(data)
    want = re.sub(r"[^a-z0-9 ]", "", section.lower()).strip()
    names = [re.sub(r"[^a-z0-9 ]", "", s["heading"].lower()).strip() for s in secs]
    idx = next((i for i, nm in enumerate(names) if nm == want), None)
    if idx is None:
        idx = next((i for i, nm in enumerate(names) if want and (nm.startswith(want) or want in nm)), None)
    if idx is None:
        close = difflib.get_close_matches(want, names, n=1, cutoff=0.7)
        idx = names.index(close[0]) if close else None
    if idx is None:
        data["sections"] = [s["heading"] for s in secs if s["level"] >= 2]
        call.notes.append(f"{best['key']}'s document has no section like '{section}'. Its sections are listed.")
        call.source(readme_url)
        return call.result(data, found=False)
    s = secs[idx]
    end = next((x["line"] for x in secs[idx + 1:] if x["level"] <= s["level"]), len(lines))
    body = "\n".join(lines[s["line"] + 1:end]).strip()
    url = f"{readme_url}#{_anchor(s['heading'])}"
    data.update({"section": s["heading"], "text": body[:SECTION_MAX], "url": url})
    if len(body) > SECTION_MAX:
        call.notes.append(f"Section cut at {SECTION_MAX} of {len(body)} characters; the full text is at the source.")
    if not body:
        call.notes.append("This section is empty in the document (only sub-sections or a heading).")
    call.source(url)
    return call.result(data)
