"""Build the extraction input for one KEP and define what the LLM must return.

The input comes from the frozen graph (status, history, comments up to the cutoff) and the
KEP's design doc as it was at the cutoff commit, so the model never sees the live feed.
The outcome (status) is GIVEN to the model, never decided by it: the LLM only explains why,
and must quote its evidence word for word from the labelled sources ([readme] or [comment:N]).

Usage: python -m src.extract.prompt KEP-3063     (prints the input that would be sent)
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

from src.brain.db import graph
from src.tools.base import ts_to_date

ROOT = Path(__file__).resolve().parents[2]
ENH = ROOT / "data" / "raw" / "repos" / "enhancements"
README_SECTIONS = re.compile(r"^(summary|motivation|goals|non-goals|drawbacks|alternatives|proposal)", re.I)
README_WORDS, PROPOSAL_WORDS, COMMENT_BUDGET, COMMENT_WORDS, COMMENT_MIN = 2500, 400, 3500, 200, 12
DECISION_WORDS = ["withdraw", "reject", "decid", "closing", "close this", "supersed", "replace", "abandon",
                  "deprecat", "not moving forward", "no longer", "instead", "because", "agreed", "consensus",
                  "alternative", "concern", "trade-off", "tradeoff", "drop", "punt", "defer"]

SYSTEM = """You explain decisions in the Kubernetes project from their design document and discussion.
Rules:
- The decision's STATUS is given and is a fact. Never contradict it or guess a different outcome.
- Use only the sources below. If they do not say why something happened, say so and set confidence to "low".
- Every evidence quote must be copied word for word from one source (8 to 30 consecutive words) and cite
  that source's label exactly as shown in brackets, without the brackets: "readme", "comment:<n>" or "rcomment:<n>".
- Only report a supersession claim when a source explicitly says one KEP replaces another, naming its KEP number.
- Be brief and concrete. No marketing language."""

TOOL = {
    "name": "record_decision",
    "description": "Record the explanation of this KEP decision, grounded in quoted evidence.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "rationale", "outcome_reason", "alternatives", "evidence",
                     "supersession_claims", "confidence"],
        "properties": {
            "summary": {"type": "string", "description": "What was proposed or decided, one or two sentences."},
            "rationale": {"type": "string", "description": "Why: the problem and the main reasons, at most 4 sentences."},
            "outcome_reason": {"type": ["string", "null"], "description":
                               "Only when the status is withdrawn, rejected, replaced or deferred: why, per the "
                               "sources; null otherwise or if the sources do not say."},
            "alternatives": {"type": "array", "description": "Options considered and why they were not chosen (max 4).",
                             "items": {"type": "object", "additionalProperties": False,
                                       "required": ["option", "why_not"],
                                       "properties": {"option": {"type": "string"}, "why_not": {"type": "string"}}}},
            "evidence": {"type": "array", "description": "1 to 5 verbatim quotes supporting the rationale or outcome.",
                         "items": {"type": "object", "additionalProperties": False, "required": ["ref", "quote"],
                                   "properties": {"ref": {"type": "string"}, "quote": {"type": "string"}}}},
            "supersession_claims": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["kep", "relation", "ref", "quote"],
                "properties": {"kep": {"type": "string", "description": "The other KEP, as KEP-<number>"},
                               "relation": {"type": "string", "enum": ["replaces", "replaced_by"]},
                               "ref": {"type": "string"}, "quote": {"type": "string"}}}},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
    },
}


def cutoff_commit(cutoff_iso: str) -> str:
    """Last enhancements commit before the cutoff, so the design doc matches the frozen graph."""
    return subprocess.run(["git", "-C", str(ENH), "rev-list", "-1", f"--before={cutoff_iso}", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def readme_at(commit: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(ENH), "show", f"{commit}:{path}/README.md"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def readme_excerpt(text: str) -> str:
    """Decision sections of the design doc, comments stripped, capped (Proposal only its start)."""
    out, budget = [], README_WORDS
    for section in re.split(r"(?m)^#{2,3} ", re.sub(r"<!--.*?-->", " ", text, flags=re.S)):
        if not README_SECTIONS.match(section) or budget <= 0:
            continue
        title, _, body = section.partition("\n")
        cap = PROPOSAL_WORDS if title.lower().startswith("proposal") else budget
        words = body.split()[:min(cap, budget)]
        if words:
            out.append(f"## {title.strip()}\n{' '.join(words)}")
            budget -= len(words)
    return "\n\n".join(out)


def _score(c: dict, status_prs: set) -> float:
    body = c["body"].lower()
    hits = sum(w in body for w in DECISION_WORDS)
    return (5 if c["thread"] in status_prs else 0) + 2 * hits + min(len(body.split()), 200) / 100 - \
        (1.5 if c["kind"] == "inline" else 0)


def select_comments(g, kid: str, status_prs: set) -> list[dict]:
    """Decision-relevant comments from the tracking issue and the PRs that changed the KEP, within a word budget."""
    rows = g.query("""MATCH (k:KEP {id:$id})-[:TRACKED_BY|CHANGES]-(t)<-[:ON]-(c:Comment)<-[:WROTE]-(p:Person)
                      WHERE t:Ticket OR t:PR
                      RETURN c.id, c.kind, c.body, c.at_ts, p.login, t.id, t.number, labels(t)[0]""",
                   {"id": kid}).result_set
    comments = [{"id": r[0], "kind": r[1], "body": r[2] or "", "at_ts": r[3], "author": r[4], "thread": r[5],
                 "where": f"{'issue' if r[7] == 'Ticket' else 'PR'} #{r[6]}"} for r in rows]
    comments = [c for c in comments if len(c["body"].split()) >= COMMENT_MIN
                and not all(line.startswith("/") for line in c["body"].strip().splitlines() if line.strip())]
    comments.sort(key=lambda c: -_score(c, status_prs))
    chosen, used = [], 0
    for c in comments:
        n = min(len(c["body"].split()), COMMENT_WORDS)
        if used + n > COMMENT_BUDGET:
            continue
        chosen.append(c)
        used += n
    return sorted(chosen, key=lambda c: c["at_ts"] or 0)


def build_input(kid: str, graph_name: str = "brain_eval", commit: str | None = None) -> dict | None:
    """The user message for one KEP plus the source texts the validator checks quotes against."""
    g = graph(graph_name)
    row = g.query("""MATCH (k:KEP {id:$id}) OPTIONAL MATCH (t:Team)-[:OWNS]->(k)
                     RETURN k.title, k.status, k.path, k.stage, t.id""", {"id": kid}).result_set
    if not row or row[0][1] is None:
        return None
    title, status, path, stage, team = row[0]
    history = g.query("""MATCH (k:KEP {id:$id})-[e:HAD_STATUS]->(s:Status)
                         RETURN s.name, e.valid_from, e.source_url ORDER BY e.valid_from""", {"id": kid}).result_set
    # only links kep.yaml records: LLM-proposed ones must never feed back into the LLM's input as facts
    links = g.query("""MATCH (k:KEP {id:$id})-[r:SUPERSEDES]-(o:KEP) WHERE r.source = 'yaml'
                       RETURN startNode(r) = k, o.id, o.title""", {"id": kid}).result_set
    status_prs = {f"enhancements#{u.rstrip('/').split('/')[-1]}" for _, _, u in history if u and "/pull/" in u}
    if commit is None:
        meta = g.query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff").result_set
        commit = cutoff_commit(meta[0][0])
    readme = readme_excerpt(readme_at(commit, path) or "") if path else ""
    comments = select_comments(g, kid, status_prs)
    if not readme and not comments:
        return None

    lines = [f"KEP: {kid} {title}", f"Owning team: {team}", f"STATUS (fact): {status}" + (f", stage {stage}" if stage else ""),
             "Status history: " + "; ".join(f"{s} from {ts_to_date(t)}" for s, t, _ in history)]
    for is_newer, other, other_title in links:
        lines.append(f"Recorded in kep.yaml: {kid} {'replaces' if is_newer else 'is replaced by'} {other} {other_title}")
    if readme:
        lines += ["", "[readme] Design document excerpt:", readme]
    if comments:
        lines += ["", "Discussion (selected comments, oldest first):"]
        for c in comments:
            body = " ".join(c["body"].split()[:COMMENT_WORDS])
            lines.append(f"[{c['id']}] @{c['author']} {ts_to_date(c['at_ts'])} on {c['where']}: {body}")
    lines += ["", f"Explain the decision {kid} with the record_decision tool."]
    text = "\n".join(lines)
    sources = {"readme": readme, **{c["id"]: c["body"] for c in comments}}
    return {"kep": kid, "status": status, "text": text, "sources": sources,
            "input_hash": hashlib.sha256((SYSTEM + text).encode()).hexdigest()[:16]}


def request_params(inp: dict, model: str) -> dict:
    """Messages API parameters for one KEP (used for both the direct call and the Batch API)."""
    return {"model": model, "max_tokens": 1500, "system": SYSTEM, "tools": [TOOL],
            "tool_choice": {"type": "tool", "name": "record_decision"},
            "messages": [{"role": "user", "content": inp["text"]}]}


if __name__ == "__main__":
    inp = build_input(sys.argv[1])
    print(inp["text"] if inp else "no input")
    if inp:
        print(f"\n# {len(inp['text'].split())} words, {len(inp['sources'])} sources, hash {inp['input_hash']}")
