"""Build a graph as of a cutoff date from data/seed/.

Order:
  1. indexes (src/brain/schema.py)
  2. history: replay every git-history event up to the cutoff through the real-time handlers
     (creates KEPs, status/milestone/approver intervals, team leadership intervals)
  3. KEP details for KEPs that exist at the cutoff (reviewers, participating SIGs, feature gates,
     related KEPs, supersession, legacy docs)
  4. teams, subprojects, code areas and code owners (current registry), people's companies
  5. tickets, PRs, comments and reviews created up to the cutoff
  6. text chunks with embeddings (vector index), for chunks whose source exists at the cutoff
  7. decisions: LLM-extracted rationale with quote-checked evidence (data/seed/decisions.jsonl)
     and LLM-proposed supersessions as reviewed in eval/supersession_review.csv
  8. derived: release dates, component -> code area, reviewer -> author weights (REVIEWED_FOR)

Every step is idempotent: loading twice gives identical counts.
Usage: python -m src.load.loader [--graph brain_eval] [--cutoff 2026-09-15 | --cutoff now]
"""
import csv
import gzip
import json
import sys
import time
from pathlib import Path

from src.brain.db import graph
from src.brain.schema import create_indexes
from src.load import upserts
from src.realtime.events import iso_to_ts, now_iso
from src.realtime.handlers import apply_event

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data" / "seed"
MANIFEST = ROOT / "data" / "MANIFEST.json"

# component spellings in kep.yaml -> canonical name
COMPONENT_ALIASES = {"scheduler": "kube-scheduler", "controller-manager": "kube-controller-manager",
                     "csi-external-provisioner": "external-provisioner"}
# canonical component -> code areas (repo:path) that implement it; only existing areas are linked
COMPONENT_CODE = {
    "kubelet": ["kubernetes/kubernetes:pkg/kubelet", "kubernetes/kubernetes:cmd/kubelet"],
    "kube-apiserver": ["kubernetes/kubernetes:cmd/kube-apiserver", "kubernetes/kubernetes:pkg/kubeapiserver",
                       "kubernetes/kubernetes:staging/src/k8s.io/apiserver"],
    "kube-controller-manager": ["kubernetes/kubernetes:cmd/kube-controller-manager",
                                "kubernetes/kubernetes:pkg/controller"],
    "kube-scheduler": ["kubernetes/kubernetes:pkg/scheduler", "kubernetes/kubernetes:cmd/kube-scheduler"],
    "kube-proxy": ["kubernetes/kubernetes:pkg/proxy", "kubernetes/kubernetes:cmd/kube-proxy"],
    "kubectl": ["kubernetes/kubectl:.", "kubernetes/kubernetes:pkg/kubectl",
                "kubernetes/kubernetes:staging/src/k8s.io/kubectl"],
    "cloud-controller-manager": ["kubernetes/kubernetes:cmd/cloud-controller-manager"],
}


def read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:
        for line in fh:
            yield json.loads(line)


def canonical_components(values: list[str]) -> list[str]:
    out = []
    for v in values:
        for part in v.split(","):
            name = part.split("(")[0].strip().lower()
            name = COMPONENT_ALIASES.get(name, name)
            if name and " " not in name and name not in out:
                out.append(name)
    return out


def step(label: str):
    def wrap(fn):
        def inner(*args, **kwargs):
            t0 = time.time()
            result = fn(*args, **kwargs)
            print(f"  {label:34} {result!s:>40}   {time.time() - t0:5.1f}s", flush=True)
            return result
        return inner
    return wrap


@step("history events replayed")
def load_history(g, cutoff_ts: int) -> int:
    n = 0
    for ev in read_jsonl(SEED / "history_events.jsonl"):
        if iso_to_ts(ev["occurred_at"]) > cutoff_ts:
            break  # file is sorted by occurred_at
        n += apply_event(g, ev)
    return n


@step("KEP details")
def load_kep_details(g) -> dict:
    existing = {r[0] for r in g.query("MATCH (k:KEP) RETURN k.id").result_set}
    keps = [k for k in read_jsonl(SEED / "keps.jsonl") if k["id"] in existing]
    people = [{"kep": k["id"], "login": p, "rel": "REVIEWS"} for k in keps for p in k["reviewers"]]
    people += [{"kep": k["id"], "login": p, "rel": "AUTHORED"} for k in keps for p in k["authors"]]
    teams = [{"kep": k["id"], "team": s} for k in keps for s in k["participating_sigs"]]
    gates = [{"kep": k["id"], "gate": f["name"], "components": canonical_components(f["components"])}
             for k in keps for f in k["feature_gates"]]
    links = [{"src": k["id"], "dst": o, "rel": "SEE_ALSO", "source": "yaml"} for k in keps for o in k["see_also"] if o in existing]
    links += [{"src": k["id"], "dst": o, "rel": "SUPERSEDES", "source": "yaml", "evidence_url": k["url"]}
              for k in keps for o in k["replaces"] if o in existing]
    links += [{"src": o, "dst": k["id"], "rel": "SUPERSEDES", "source": "yaml", "evidence_url": k["url"]}
              for k in keps for o in k["superseded_by"] if o in existing]
    docs = [{"kep": k["id"], "url": u, "rel": "REPLACES_DOC"} for k in keps for u in k["replaces_docs"]]
    docs += [{"kep": k["id"], "url": u, "rel": "SEE_ALSO_DOC"} for k in keps for u in k["see_also_docs"]]
    # titles and links from the current kep.yaml (status, milestones and approvers come from history)
    upserts.run_batches(g, """UNWIND $rows AS r MATCH (k:KEP {id: r.id})
                              SET k.title = r.title, k.url = r.url, k.readme_url = r.readme_url,
                                  k.issue_url = r.issue_url, k.path = r.path""", keps)
    return {"keps": len(keps), "people": upserts.kep_people(g, people), "teams": upserts.kep_participants(g, teams),
            "gates": upserts.kep_gates(g, gates), "links": upserts.kep_links(g, links), "docs": upserts.kep_docs(g, docs)}


@step("teams, code ownership")
def load_org(g) -> dict:
    teams = list(read_jsonl(SEED / "teams.jsonl"))
    areas = list(read_jsonl(SEED / "code_areas.jsonl"))
    upserts.teams(g, teams)
    subs = [{"team": t["id"], "id": f"{t['id']}/{s['name']}", "name": s["name"], "description": s["description"]}
            for t in teams for s in t["subprojects"]]
    upserts.subprojects(g, subs)
    upserts.code_areas(g, areas)
    owns = [{"subproject": f"{t['id']}/{s['name']}", "area": a} for t in teams for s in t["subprojects"] for a in s["code_areas"]]
    roles = [{"login": p, "area": a["id"], "role": "APPROVES_CODE", "former": False} for a in areas for p in a["approvers"]]
    roles += [{"login": p, "area": a["id"], "role": "APPROVES_CODE", "former": True} for a in areas
              for p in a["emeritus_approvers"] if p not in a["approvers"]]
    roles += [{"login": p, "area": a["id"], "role": "REVIEWS_CODE", "former": False} for a in areas for p in a["reviewers"]]
    return {"teams": len(teams), "subprojects": len(subs), "areas": len(areas),
            "owns_code": upserts.owns_code(g, owns), "code_roles": upserts.code_roles(g, roles),
            "people": upserts.people(g, list(read_jsonl(SEED / "people.jsonl")))}


@step("tickets, PRs, comments, reviews")
def load_threads(g, cutoff_ts: int) -> dict:
    th = SEED / "threads"
    keep = lambda rows, key: [r for r in rows if r.get(key) and r[key] <= cutoff_ts]
    return {"tickets": upserts.tickets(g, keep(read_jsonl(th / "tickets.jsonl.gz"), "created_ts")),
            "prs": upserts.prs(g, keep(read_jsonl(th / "prs.jsonl.gz"), "created_ts")),
            "comments": upserts.comments(g, keep(read_jsonl(th / "comments.jsonl.gz"), "at_ts")),
            "reviews": upserts.reviews(g, keep(read_jsonl(th / "reviews.jsonl.gz"), "at_ts"))}


@step("chunks with embeddings")
def load_chunks(g, cutoff_ts: int) -> dict:
    """Chunk nodes for vector search; skipped (with a note) if vectors have not been computed yet."""
    from src.load.embed import CHUNKS, load_vectors
    vectors = load_vectors()
    if not vectors:
        return {"skipped": "no data/seed/chunk_vectors.npz (run: python -m src.load.embed all)"}
    rows = []
    for c in read_jsonl(CHUNKS):
        v = vectors.get(c["id"])
        if v is None or (c["at_ts"] and c["at_ts"] > cutoff_ts):
            continue
        rows.append({**c, "embedding": v.astype(float).tolist()})
    return {"chunks": upserts.chunks(g, rows), "without_vector": sum(1 for _ in read_jsonl(CHUNKS)) - len(vectors)}


@step("decisions (LLM rationale)")
def load_decisions(g) -> dict:
    """Decision nodes from data/seed/decisions.jsonl (validated quotes only), plus LLM-proposed
    supersessions from eval/supersession_review.csv: 'yes' rows count as verified by a person,
    blank rows load as unverified (the agent must hedge), 'no' rows are skipped."""
    path = SEED / "decisions.jsonl"
    if not path.exists():
        return {"skipped": "no data/seed/decisions.jsonl (run: python -m src.extract.validate)"}
    rows = list(read_jsonl(path))
    n = upserts.decisions(g, rows)
    review = ROOT / "eval" / "supersession_review.csv"
    claims = [r for r in csv.DictReader(review.open()) if r["verdict"].strip().lower() != "no"] if review.exists() else []
    # the review sheet is the truth for LLM-proposed edges: drop them all, then add back the ones not rejected
    g.query("MATCH ()-[s:SUPERSEDES]->() WHERE s.source = 'llm' DELETE s")
    decided = {r["kep"] for r in rows}
    upserts.run_batches(g, """UNWIND $rows AS r
                              MATCH (a:KEP {id: r.newer}), (b:KEP {id: r.older})
                              MERGE (a)-[s:SUPERSEDES]->(b)
                              SET s.source = coalesce(s.source, 'llm'),
                                  s.verified = CASE WHEN s.source = 'yaml' THEN true ELSE r.yes END""",
                        [{"newer": c["newer"], "older": c["older"], "yes": c["verdict"].strip().lower() == "yes"}
                         for c in claims if c["claimed_in"] in decided])
    return {"decisions": n, "llm_supersessions": len(claims)}


@step("derived edges")
def load_derived(g) -> dict:
    if (SEED / "releases.jsonl").exists():
        upserts.releases(g, list(read_jsonl(SEED / "releases.jsonl")))
    rows = [{"component": c, "area": a} for c, areas in COMPONENT_CODE.items() for a in areas]
    upserts.run_batches(g, """UNWIND $rows AS r
                              MATCH (c:Component {name: r.component}), (a:CodeArea {id: r.area})
                              MERGE (c)-[:IMPLEMENTED_IN]->(a)""", rows)
    # reviewer -> author: formal PR reviews and comments on someone else's ticket or PR
    g.query("MATCH ()-[e:REVIEWED_FOR]->() DELETE e")
    g.query("""MATCH (r:Person)-[:REVIEWED]->(x:PR)<-[:OPENED]-(a:Person) WHERE r <> a
               WITH r, a, count(DISTINCT x) AS w
               MERGE (r)-[e:REVIEWED_FOR]->(a) SET e.weight = w""")
    g.query("""MATCH (r:Person)-[:WROTE]->(:Comment)-[:ON]->(t)<-[:OPENED]-(a:Person) WHERE r <> a
               WITH r, a, count(DISTINCT t) AS w
               MERGE (r)-[e:REVIEWED_FOR]->(a) SET e.weight = coalesce(e.weight, 0) + w""")
    linked = g.query("MATCH (:Component)-[e:IMPLEMENTED_IN]->() RETURN count(e)").result_set[0][0]
    rf = g.query("MATCH ()-[e:REVIEWED_FOR]->() RETURN count(e)").result_set[0][0]
    return {"component_links": linked, "reviewed_for": rf}


def counts(g) -> dict:
    nodes = dict(g.query("MATCH (n) RETURN labels(n)[0] AS l, count(*) ORDER BY l").result_set)
    rels = dict(g.query("MATCH ()-[r]->() RETURN type(r) AS t, count(*) ORDER BY t").result_set)
    return {"nodes": nodes, "relationships": rels}


def load(graph_name: str, cutoff: str) -> dict:
    g = graph(graph_name)
    cutoff_iso = now_iso() if cutoff == "now" else cutoff
    cutoff_ts = iso_to_ts(cutoff_iso)
    print(f"loading graph '{graph_name}' as of {cutoff_iso}")
    create_indexes(graph_name)
    load_history(g, cutoff_ts)
    load_kep_details(g)
    load_org(g)
    load_threads(g, cutoff_ts)
    load_chunks(g, cutoff_ts)
    load_decisions(g)
    load_derived(g)
    g.query("MERGE (m:Meta {key:'snapshot'}) SET m.cutoff = $c, m.cutoff_ts = $ts, m.loaded_at = $now",
            {"c": cutoff_iso, "ts": cutoff_ts, "now": now_iso()})
    return counts(g)


if __name__ == "__main__":
    args = sys.argv[1:]
    name = args[args.index("--graph") + 1] if "--graph" in args else "brain_eval"
    if "--cutoff" in args:
        c = args[args.index("--cutoff") + 1]
        cutoff = c if c == "now" or "T" in c else c + "T00:00:00Z"
    else:
        cutoff = json.loads(MANIFEST.read_text())["live_feed_cutoff"]
    result = load(name, cutoff)
    print(json.dumps(result, indent=1))
