"""The Atlas: the skeleton of the whole graph for the UI's Obsidian-style view (GET /api/atlas).

What it holds: teams, KEPs, code areas and the people who lead or approve (plus the most influential
reviewers by PageRank), and the links between them. Comments, reviews, PRs and chunks are left out:
there are over 150k of them, and drawn they would only be noise.

Node ids are the graph's merge keys (KEP-753, a login, sig-node, a code-area id), the same values the
live feed sends in `touched`, so the UI can pulse the nodes a live change touched. KEP status history
is sent along so the UI's time slider can recolour the graph at any date without further queries.
Read-only, no LLM. Usage: python -m src.atlas [graph_name]   (prints the counts)
"""
import json
import sys
from collections import Counter

MAX_PEOPLE = 400
TOP_PAGERANK = 150


def _rows(g, cypher: str, params: dict | None = None) -> list:
    return g.query(cypher, params or {}).result_set


def _code_label(repo: str, path: str) -> str:
    name = (repo or "").split("/")[-1]
    return name if path in (None, "", ".") else f"{name}/{path}"


def build_atlas(g) -> dict:
    nodes, links = {}, []

    def link(source, target, kind):
        if source in nodes and target in nodes:
            links.append({"source": source, "target": target, "type": kind})

    # teams: the registry (kind is set) plus any older team that still owns KEPs or code
    for tid, name, kind, url, retired, owned in _rows(g, """
            MATCH (t:Team)
            WHERE t.kind IS NOT NULL OR (t)-[:OWNS]->() OR (t)-[:HAS_SUBPROJECT]->()
            OPTIONAL MATCH (t)-[:OWNS]->(k:KEP)
            RETURN t.id, t.name, t.kind, t.url, t.retired_ts, count(k)"""):
        nodes[tid] = {"id": tid, "type": "Team", "label": name or tid, "team": tid, "kind": kind,
                      "url": url, "retired": retired is not None, "size": owned}

    for kid, title, status, url, born, owner in _rows(g, """
            MATCH (k:KEP) OPTIONAL MATCH (t:Team)-[:OWNS]->(k)
            RETURN k.id, k.title, k.status, k.url, k.created_ts, t.id"""):
        nodes[kid] = {"id": kid, "type": "KEP", "label": f"{kid} {title or ''}".strip(), "team": owner,
                      "status": status, "url": url, "born_ts": born}
        link(owner, kid, "OWNS")

    for tid, cid, repo, path, url in _rows(g, """
            MATCH (t:Team)-[:HAS_SUBPROJECT]->(:Subproject)-[:OWNS_CODE]->(c:CodeArea)
            RETURN DISTINCT t.id, c.id, c.repo, c.path, c.url"""):
        if cid not in nodes:
            nodes[cid] = {"id": cid, "type": "CodeArea", "label": _code_label(repo, path), "team": tid, "url": url}
        link(tid, cid, "OWNS_CODE")

    # people: current leads and KEP approvers, then the top reviewers by PageRank, at most MAX_PEOPLE
    rank = dict(_rows(g, "CALL algo.pageRank('Person', 'REVIEWED_FOR') YIELD node, score RETURN node.login, score"))
    leads = _rows(g, """MATCH (p:Person)-[l:LEADS]->(t:Team) WHERE l.valid_to IS NULL
                        RETURN p.login, t.id, l.role""")
    approvers = [r[0] for r in _rows(g, """MATCH (p:Person)-[a:APPROVES]->(:KEP) WHERE a.valid_to IS NULL
                                           RETURN DISTINCT p.login""")]
    top = sorted(rank, key=rank.get, reverse=True)[:TOP_PAGERANK]
    # a login that equals another node's key (an account named like a team, e.g. "sig-node") would overwrite it
    people = [p for p in dict.fromkeys([r[0] for r in leads] + approvers + top) if p not in nodes]
    if len(people) > MAX_PEOPLE:
        people = sorted(people, key=lambda p: rank.get(p, 0), reverse=True)[:MAX_PEOPLE]
    for login in people:
        nodes[login] = {"id": login, "type": "Person", "label": login, "url": f"https://github.com/{login}",
                        "size": rank.get(login, 0)}

    p = {"people": people}
    for login, tid, role in leads:
        if login in p["people"]:
            link(login, tid, "LEADS")
            nodes[login]["role"] = role
    for login, kid in _rows(g, """MATCH (p:Person)-[:AUTHORED]->(k:KEP) WHERE p.login IN $people
                                   RETURN p.login, k.id""", p):
        link(login, kid, "AUTHORED")
    for login, kid in _rows(g, """MATCH (p:Person)-[a:APPROVES]->(k:KEP) WHERE p.login IN $people AND a.valid_to IS NULL
                                   RETURN p.login, k.id""", p):
        link(login, kid, "APPROVES")
    for login, cid in _rows(g, """MATCH (p:Person)-[a:APPROVES_CODE]->(c:CodeArea)
                                   WHERE p.login IN $people AND coalesce(a.former, false) = false
                                   RETURN p.login, c.id""", p):
        link(login, cid, "APPROVES_CODE")
    for tid, kid in _rows(g, "MATCH (t:Team)-[:PARTICIPATES_IN]->(k:KEP) RETURN t.id, k.id"):
        if nodes.get(kid, {}).get("team") != tid:   # the owning team is already linked by OWNS
            link(tid, kid, "PARTICIPATES_IN")
    for a, b, kind in _rows(g, """MATCH (a:KEP)-[r:SUPERSEDES|SEE_ALSO]->(b:KEP)
                                  RETURN a.id, b.id, type(r)"""):
        link(a, b, kind)

    # people with no link left in the Atlas would only drift around the edge
    linked = {l["source"] for l in links} | {l["target"] for l in links}
    for login in [k for k, n in nodes.items() if n["type"] == "Person" and k not in linked]:
        del nodes[login]

    # a person's colour is the team they lead, else the team they are linked to most
    by_person = {}
    for l in links:
        team = nodes[l["target"]].get("team")
        if nodes[l["source"]]["type"] == "Person" and team:
            by_person.setdefault(l["source"], Counter())[team] += 10_000 if l["type"] == "LEADS" else 1
    for login, teams in by_person.items():
        nodes[login]["team"] = teams.most_common(1)[0][0]

    degree = Counter()
    for l in links:
        degree[l["source"]] += 1
        degree[l["target"]] += 1
    for n in nodes.values():
        if n["type"] in ("KEP", "CodeArea"):
            n["size"] = degree[n["id"]]

    history = {}
    for kid, ts, status in _rows(g, """MATCH (k:KEP)-[h:HAD_STATUS]->(s:Status)
                                       RETURN k.id, h.valid_from, s.name ORDER BY k.id, h.valid_from"""):
        history.setdefault(kid, []).append([ts, status])
    releases = _rows(g, "MATCH (r:Release) WHERE r.ts IS NOT NULL RETURN r.version, r.ts ORDER BY r.ts")
    snapshot = _rows(g, "OPTIONAL MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff_ts")

    return {"nodes": list(nodes.values()), "links": links, "history": history,
            "releases": [list(r) for r in releases], "cutoff_ts": snapshot[0][0] if snapshot else None,
            "counts": dict(Counter(n["type"] for n in nodes.values())) | {"links": len(links)}}


if __name__ == "__main__":
    from src.brain.db import graph
    out = build_atlas(graph(sys.argv[1] if len(sys.argv) > 1 else "brain_eval"))
    print(json.dumps(out["counts"], indent=1), f"{len(json.dumps(out)) / 1e6:.2f} MB")
