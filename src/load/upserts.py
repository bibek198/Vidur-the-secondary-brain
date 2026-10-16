"""Idempotent batch writes. The bulk loader and the real-time handlers both use these.

Every function takes a FalkorDB graph and a list of row dicts, and runs one parameterized
UNWIND ... MERGE query per batch of 500 rows. MERGE on the keys from src/brain/schema.py
means running any of them twice changes nothing.
"""
from typing import Iterable

BATCH = 500


def run_batches(g, query: str, rows: Iterable[dict], batch: int = BATCH) -> int:
    rows = list(rows)
    for i in range(0, len(rows), batch):
        g.query(query, {"rows": rows[i:i + batch]})
    return len(rows)


# ---------- people, teams, code ownership ----------

def people(g, rows):
    """rows: {login, company?}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (p:Person {login: r.login})
        WITH p, r WHERE r.company IS NOT NULL
        MERGE (o:Org {name: r.company})
        MERGE (p)-[:AFFILIATED_WITH]->(o)""", rows)


def teams(g, rows):
    """rows: {id, kind, name, mission, label, url}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (t:Team {id: r.id})
        SET t.kind = r.kind, t.name = r.name, t.mission = r.mission, t.label = r.label, t.url = r.url""", rows)


def subprojects(g, rows):
    """rows: {team, id, name, description}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (t:Team {id: r.team})
        MERGE (s:Subproject {id: r.id})
        SET s.name = r.name, s.description = r.description
        MERGE (t)-[:HAS_SUBPROJECT]->(s)""", rows)


def code_areas(g, rows):
    """rows: {id, repo, path, url, labels}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (c:CodeArea {id: r.id})
        SET c.repo = r.repo, c.path = r.path, c.url = r.url, c.labels = r.labels""", rows)


def owns_code(g, rows):
    """rows: {subproject, area}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (s:Subproject {id: r.subproject}), (c:CodeArea {id: r.area})
        MERGE (s)-[:OWNS_CODE]->(c)""", rows)


def code_roles(g, rows):
    """rows: {login, area, role: APPROVES_CODE|REVIEWS_CODE, former: bool}"""
    out = 0
    for rel in ("APPROVES_CODE", "REVIEWS_CODE"):
        out += run_batches(g, f"""
            UNWIND $rows AS r
            MERGE (p:Person {{login: r.login}})
            WITH p, r MATCH (c:CodeArea {{id: r.area}})
            MERGE (p)-[e:{rel}]->(c)
            SET e.former = CASE WHEN r.former THEN true ELSE null END""",
                           [r for r in rows if r["role"] == rel])
    return out


def leads_interval(g, rows):
    """Open a leadership interval. rows: {login, team, role, t}. Closes nothing (see close_leads)."""
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (p:Person {login: r.login})
        MERGE (t:Team {id: r.team})
        MERGE (p)-[l:LEADS {role: r.role, valid_from: r.t}]->(t)
        ON CREATE SET l.valid_to = null""", rows)


def close_leads(g, rows):
    """Close the open leadership interval. rows: {login, team, role, t}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (:Person {login: r.login})-[l:LEADS {role: r.role}]->(:Team {id: r.team})
        WHERE l.valid_to IS NULL AND l.valid_from < r.t
        SET l.valid_to = r.t""", rows)


# ---------- KEPs ----------

def kep_nodes(g, rows):
    """rows: KEP records from keps.jsonl (static properties) plus created_ts."""
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (k:KEP {id: r.id})
        SET k.number = r.number, k.title = r.title, k.path = r.path, k.url = r.url,
            k.readme_url = r.readme_url, k.issue_url = r.issue_url,
            k.created_ts = coalesce(k.created_ts, r.created_ts)
        WITH k, r WHERE r.owning_sig IS NOT NULL
        MERGE (t:Team {id: r.owning_sig})
        MERGE (t)-[:OWNS]->(k)""", rows)


def kep_people(g, rows):
    """rows: {kep, login, rel: AUTHORED|REVIEWS}"""
    out = 0
    for rel in ("AUTHORED", "REVIEWS"):
        out += run_batches(g, f"""
            UNWIND $rows AS r
            MATCH (k:KEP {{id: r.kep}})
            MERGE (p:Person {{login: r.login}})
            MERGE (p)-[:{rel}]->(k)""", [r for r in rows if r["rel"] == rel])
    return out


def kep_participants(g, rows):
    """rows: {kep, team}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (k:KEP {id: r.kep})
        MERGE (t:Team {id: r.team})
        MERGE (t)-[:PARTICIPATES_IN]->(k)""", rows)


def kep_gates(g, rows):
    """rows: {kep, gate, components: [..]}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (k:KEP {id: r.kep})
        MERGE (f:FeatureGate {name: r.gate})
        MERGE (k)-[:GATED_BY]->(f)
        WITH f, r UNWIND r.components AS comp
        MERGE (c:Component {name: comp})
        MERGE (f)-[:AFFECTS]->(c)""", rows)


def kep_links(g, rows):
    """rows: {src, dst, rel: SEE_ALSO|SUPERSEDES, source}"""
    out = run_batches(g, """
        UNWIND $rows AS r
        MATCH (a:KEP {id: r.src}), (b:KEP {id: r.dst})
        MERGE (a)-[:SEE_ALSO]->(b)""", [r for r in rows if r["rel"] == "SEE_ALSO"])
    out += run_batches(g, """
        UNWIND $rows AS r
        MATCH (a:KEP {id: r.src}), (b:KEP {id: r.dst})
        MERGE (a)-[s:SUPERSEDES]->(b)
        SET s.source = coalesce(s.source, r.source), s.verified = coalesce(s.verified, r.source = 'yaml'),
            s.evidence_url = coalesce(s.evidence_url, r.evidence_url)""", [r for r in rows if r["rel"] == "SUPERSEDES"])
    return out


def kep_docs(g, rows):
    """rows: {kep, url, rel: REPLACES_DOC|SEE_ALSO_DOC}"""
    out = 0
    for rel in ("REPLACES_DOC", "SEE_ALSO_DOC"):
        out += run_batches(g, f"""
            UNWIND $rows AS r
            MATCH (k:KEP {{id: r.kep}})
            MERGE (d:Document {{url: r.url}})
            MERGE (k)-[:{rel}]->(d)""", [r for r in rows if r["rel"] == rel])
    return out


def set_interval(g, kep: str, rel: str, target_label: str, target_key: str, target: str | None,
                 t: int, extra: dict | None = None, source_url: str | None = None) -> None:
    """Move a KEP's open `rel` interval (HAD_STATUS / TARGETS{stage} / APPROVES) to a new target at time t.

    Idempotent and safe with out-of-order events:
    - the interval starting at t is MERGEd (keyed by valid_from), so a replay does not duplicate it;
    - any interval that started before t and is still open (or ends after t) is closed at t;
    - the new interval ends where the next later interval begins (null if none).
    `extra` holds properties that scope the interval (e.g. {'stage': 'beta'} for TARGETS).
    """
    extra = extra or {}
    scope = "".join(f" AND e.{k} = ${k}" for k in extra)
    params = {"kep": kep, "t": t, "target": target, "src": source_url, **extra}
    g.query(f"""
        MATCH (k:KEP {{id: $kep}})-[e:{rel}]->()
        WHERE e.valid_from < $t AND (e.valid_to IS NULL OR e.valid_to > $t){scope}
        SET e.valid_to = $t""", params)
    if target is None:
        return
    props = ", ".join(f"{k}: ${k}" for k in extra)
    props = (props + ", " if props else "") + "valid_from: $t"
    g.query(f"""
        MATCH (k:KEP {{id: $kep}})
        MERGE (x:{target_label} {{{target_key}: $target}})
        MERGE (k)-[e:{rel} {{{props}}}]->(x)
        SET e.source_url = coalesce(e.source_url, $src)
        WITH k, e
        OPTIONAL MATCH (k)-[later:{rel}]->() WHERE later.valid_from > $t{scope.replace('e.', 'later.')}
        WITH e, min(later.valid_from) AS next_start
        SET e.valid_to = next_start""", params)


def approver_interval(g, kep: str, login: str, t: int, added: bool) -> None:
    """Open (added) or close (removed) a person's APPROVES interval on a KEP at time t."""
    if added:
        g.query("""
            MATCH (k:KEP {id: $kep})
            MERGE (p:Person {login: $login})
            MERGE (p)-[e:APPROVES {valid_from: $t}]->(k)
            ON CREATE SET e.valid_to = null""", {"kep": kep, "login": login, "t": t})
    else:
        g.query("""
            MATCH (:Person {login: $login})-[e:APPROVES]->(:KEP {id: $kep})
            WHERE e.valid_from < $t AND e.valid_to IS NULL
            SET e.valid_to = $t""", {"kep": kep, "login": login, "t": t})


# ---------- tickets, PRs, comments ----------

def tickets(g, rows):
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (t:Ticket {id: r.id})
        SET t.number = r.number, t.title = r.title, t.state = r.state, t.state_reason = r.state_reason,
            t.url = r.url, t.created_ts = r.created_ts, t.closed_ts = r.closed_ts, t.labels = r.labels,
            t.process_labels = r.process_labels, t.body = r.body, t.comment_count = r.comment_count
        WITH t, r
        FOREACH (_ IN CASE WHEN r.author IS NULL THEN [] ELSE [1] END |
            MERGE (p:Person {login: r.author}) MERGE (p)-[:OPENED]->(t))
        WITH t, r WHERE r.kep IS NOT NULL
        MATCH (k:KEP {id: r.kep})
        MERGE (k)-[:TRACKED_BY]->(t)""", rows)


def prs(g, rows):
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (x:PR {id: r.id})
        SET x.number = r.number, x.title = r.title, x.state = r.state, x.merged = r.merged, x.url = r.url,
            x.created_ts = r.created_ts, x.closed_ts = r.closed_ts, x.labels = r.labels,
            x.process_labels = r.process_labels, x.body = r.body
        WITH x, r
        FOREACH (_ IN CASE WHEN r.author IS NULL THEN [] ELSE [1] END |
            MERGE (p:Person {login: r.author}) MERGE (p)-[:OPENED]->(x))
        WITH x, r UNWIND r.keps AS kid
        MATCH (k:KEP {id: kid})
        MERGE (x)-[:CHANGES]->(k)""", rows)


def comments(g, rows):
    """rows: {id, kind, thread, author, at_ts, url, body, path?, mentions[]}; the thread must exist."""
    return run_batches(g, """
        UNWIND $rows AS r
        OPTIONAL MATCH (tk:Ticket {id: r.thread})
        OPTIONAL MATCH (pr:PR {id: r.thread})
        WITH r, coalesce(tk, pr) AS thread WHERE thread IS NOT NULL
        MERGE (c:Comment {id: r.id})
        SET c.kind = r.kind, c.body = r.body, c.at_ts = r.at_ts, c.url = r.url, c.path = r.path
        MERGE (c)-[:ON]->(thread)
        MERGE (p:Person {login: r.author})
        MERGE (p)-[:WROTE]->(c)
        WITH c, r UNWIND r.mentions AS kid
        MATCH (k:KEP {id: kid})
        MERGE (c)-[:MENTIONS]->(k)""", rows)


def reviews(g, rows):
    """rows: {pr, author, state, at_ts}"""
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (x:PR {id: r.pr})
        MERGE (p:Person {login: r.author})
        MERGE (p)-[e:REVIEWED {at: r.at_ts}]->(x)
        SET e.state = r.state""", rows)


def releases(g, rows):
    """rows: {version, date, ts, url} (Kubernetes minor releases)"""
    return run_batches(g, """
        UNWIND $rows AS r
        MERGE (x:Release {version: r.version})
        SET x.date = r.date, x.ts = r.ts, x.url = r.url""", rows)


# ---------- chunks (vector search) ----------

def chunks(g, rows):
    """rows: {id, kind, text, source, url, at_ts, embedding: [384 floats]}; links to the source KEP or Comment."""
    out = run_batches(g, """
        UNWIND $rows AS r
        MATCH (k:KEP {id: r.source})
        MERGE (c:Chunk {id: r.id})
        SET c.kind = r.kind, c.text = r.text, c.url = r.url, c.embedding = vecf32(r.embedding)
        MERGE (c)-[:CHUNK_OF]->(k)""", [r for r in rows if r["kind"] == "kep"], batch=200)
    out += run_batches(g, """
        UNWIND $rows AS r
        MATCH (m:Comment {id: r.source})
        MERGE (c:Chunk {id: r.id})
        SET c.kind = r.kind, c.text = r.text, c.url = r.url, c.at_ts = r.at_ts, c.embedding = vecf32(r.embedding)
        MERGE (c)-[:CHUNK_OF]->(m)""", [r for r in rows if r["kind"] == "comment"], batch=200)
    return out


# ---------- events ----------

def event_node(g, ev: dict, label: str, key: str, value: str) -> None:
    """Record an event and link it to the entity it touched (MERGE on event_id: replays are no-ops)."""
    g.query(f"""
        MERGE (e:Event {{event_id: $id}})
        SET e.kind = $kind, e.source = $source, e.occurred_at = $occurred_at, e.occurred_ts = $ts,
            e.observed_at = $observed_at, e.url = $url, e.summary = $summary
        WITH e
        MATCH (x:{label} {{{key}: $value}})
        MERGE (e)-[:TOUCHED]->(x)""",
            {"id": ev["event_id"], "kind": ev["kind"], "source": ev["source"], "occurred_at": ev["occurred_at"],
             "ts": ev["ts"], "observed_at": ev["observed_at"], "url": ev["payload"].get("url"),
             "summary": ev.get("summary"), "value": value})


# ---------- decisions (LLM rationale layer) ----------

def decisions(g, rows):
    """rows: validated decisions from data/seed/decisions.jsonl. Comment quotes become EVIDENCED_BY edges;
    design-doc quotes stay on the Decision (readme_quotes) with the KEP's readme_url as their source."""
    for r in rows:
        r["readme_quotes"] = [e["quote"] for e in r["evidence"] if e["ref"] == "readme"]
        r["comment_quotes"] = [{"id": e["ref"], "quote": e["quote"]} for e in r["evidence"] if e["ref"] != "readme"]
    return run_batches(g, """
        UNWIND $rows AS r
        MATCH (k:KEP {id: r.kep})
        MERGE (d:Decision {id: r.id})
        SET d.outcome = r.outcome, d.summary = r.summary, d.rationale = r.rationale,
            d.outcome_reason = r.outcome_reason, d.alternatives = r.alternatives, d.readme_quotes = r.readme_quotes,
            d.readme_url = k.readme_url, d.confidence = r.confidence, d.verified = r.verified,
            d.model = r.model, d.input_hash = r.input_hash, d.extracted_at = r.extracted_at, d.stale = false
        MERGE (d)-[:DECIDED]->(k)
        WITH d, r UNWIND r.comment_quotes AS q
        MATCH (c:Comment {id: q.id})
        MERGE (d)-[e:EVIDENCED_BY]->(c)
        SET e.quote = q.quote""", rows)
