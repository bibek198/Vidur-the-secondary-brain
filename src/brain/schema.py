"""Graph schema: node labels, relationship types and the indexes FalkorDB needs.

The full model with the reasoning for every label and edge is in docs/schema.md.
Run `python -m src.brain.schema [graph_name]` to create the indexes. It is safe to run
twice: "already indexed" errors are treated as success.
"""
import sys

from src.brain import config
from src.brain.db import graph

# label -> key property used by MERGE (every loader and handler MERGEs on these)
NODE_KEYS = {
    "Person": "login",
    "Org": "name",
    "Team": "id",            # sig-node, wg-device-management, committee-steering
    "Subproject": "id",      # sig-node/kubelet
    "CodeArea": "id",        # kubernetes/kubernetes:pkg/kubelet
    "KEP": "id",             # KEP-753
    "Status": "name",        # provisional, implementable, implemented, withdrawn, ...
    "Release": "version",    # v1.28
    "FeatureGate": "name",
    "Component": "name",     # kubelet, kube-apiserver, ...
    "Document": "url",       # legacy design docs a KEP replaces or links to
    "Ticket": "id",          # enhancements#753 (issue)
    "PR": "id",              # enhancements#4255
    "Comment": "id",         # comment:<github id> / review:<id> / rcomment:<id>
    "Decision": "id",        # dec:KEP-753 (LLM rationale layer, Phase 5)
    "Chunk": "id",           # text chunk with an embedding (retrieval + baseline)
    "Event": "event_id",     # change log (real-time engine)
    "Question": "id",        # memory: questions asked and what they touched
    "Draft": "id",           # artefacts produced by action tools
    "Watch": "id",
    "Meta": "key",
}

# relationship types (direction: actor -> thing, newer -> older)
RELATIONSHIPS = [
    "LEADS",            # Person -> Team {role, valid_from, valid_to}
    "AFFILIATED_WITH",  # Person -> Org
    "HAS_SUBPROJECT",   # Team -> Subproject
    "OWNS_CODE",        # Subproject -> CodeArea
    "APPROVES_CODE",    # Person -> CodeArea {former}
    "REVIEWS_CODE",     # Person -> CodeArea
    "IMPLEMENTED_IN",   # Component -> CodeArea
    "OWNS",             # Team -> KEP
    "PARTICIPATES_IN",  # Team -> KEP
    "AUTHORED",         # Person -> KEP
    "REVIEWS",          # Person -> KEP
    "APPROVES",         # Person -> KEP {valid_from, valid_to}
    "HAD_STATUS",       # KEP -> Status {valid_from, valid_to, observed_at, source_url}
    "TARGETS",          # KEP -> Release {stage, valid_from, valid_to}
    "SEE_ALSO",         # KEP -> KEP
    "SUPERSEDES",       # KEP(newer) -> KEP(older) {source, verified, evidence_url}
    "REPLACES_DOC",     # KEP -> Document
    "SEE_ALSO_DOC",     # KEP -> Document
    "GATED_BY",         # KEP -> FeatureGate
    "AFFECTS",          # FeatureGate -> Component
    "TRACKED_BY",       # KEP -> Ticket
    "CHANGES",          # PR -> KEP (PR that edits the KEP)
    "OPENED",           # Person -> Ticket | PR
    "WROTE",            # Person -> Comment
    "ON",               # Comment -> Ticket | PR
    "REVIEWED",         # Person -> PR {state, at}
    "MENTIONS",         # Comment -> KEP
    "DECIDED",          # Decision -> KEP
    "EVIDENCED_BY",     # Decision -> Comment
    "CHUNK_OF",         # Chunk -> KEP | Comment
    "TOUCHED",          # Event -> any entity
    "ASKED_ABOUT",      # Question -> any entity
    "OUTDATED_BY",      # Question -> Event
    "WATCHES",          # Watch -> any entity
    "REVIEWED_FOR",     # Person -> Person {weight} (derived, for PageRank)
]

FULLTEXT = {  # label -> properties searched when resolving names in a question
    "KEP": ["title"],
    "Team": ["name", "mission"],
    "Subproject": ["name", "description"],
    "FeatureGate": ["name"],
    "Component": ["name"],
    "Comment": ["body"],
    "Chunk": ["text"],
}

EXTRA_RANGE = [  # extra range indexes for filters used by the tools
    ("node", "KEP", "status"), ("node", "KEP", "owning_sig"), ("node", "Event", "occurred_ts"),
    ("node", "Comment", "at_ts"), ("node", "CodeArea", "path"),
    ("rel", "HAD_STATUS", "valid_to"), ("rel", "LEADS", "valid_to"), ("rel", "TARGETS", "valid_to"),
    ("rel", "APPROVES", "valid_to"),
]


def statements() -> list[str]:
    out = [f"CREATE INDEX FOR (n:{label}) ON (n.{key})" for label, key in NODE_KEYS.items()]
    for kind, name, prop in EXTRA_RANGE:
        pattern = f"(n:{name})" if kind == "node" else f"()-[n:{name}]-()"
        out.append(f"CREATE INDEX FOR {pattern} ON (n.{prop})")
    for label, props in FULLTEXT.items():
        args = ", ".join(f"'{p}'" for p in props)
        out.append(f"CALL db.idx.fulltext.createNodeIndex('{label}', {args})")
    out.append(f"CREATE VECTOR INDEX FOR (c:Chunk) ON (c.embedding) "
               f"OPTIONS {{dimension:{config.EMBED_DIM}, similarityFunction:'cosine'}}")
    return out


def create_indexes(graph_name: str | None = None) -> dict:
    """Create every index; returns counts of created / already present."""
    g = graph(graph_name)
    created = existing = 0
    for stmt in statements():
        try:
            g.query(stmt)
            created += 1
        except Exception as e:  # noqa: BLE001
            if "already indexed" not in str(e):
                raise
            existing += 1
    return {"created": created, "already_present": existing}


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else None
    print(name or config.GRAPH_NAME, create_indexes(name))
