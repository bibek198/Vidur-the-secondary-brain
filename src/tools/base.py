"""Shared layer for every graph tool: traced queries, path collection, as_of parsing, result shape.

Every tool returns the same shape:
    {tool, found, data, notes[], sources[], path{nodes,edges}, cypher_trace[], as_of, stale_flags[]}
- cypher_trace: every query the tool ran, with parameters, rows and milliseconds ("How I got this").
- path: the nodes and edges the answer is built from, ready for the UI graph panel.
- sources: URLs that exist as properties in the graph. The agent may only cite these.
"""
import re
from contextvars import ContextVar
from datetime import datetime, timezone

from falkordb.edge import Edge
from falkordb.node import Node
from falkordb.path import Path

from src.brain.db import graph
from src.brain.schema import NODE_KEYS

_TRACE: ContextVar[list | None] = ContextVar("cypher_trace", default=None)
URL_KEYS = ("url", "issue_url", "readme_url", "source_url", "evidence_url")
MAX_PATH_NODES = 60


def trace() -> list:
    t = _TRACE.get()
    if t is None:
        t = []
        _TRACE.set(t)
    return t


def reset_trace() -> None:
    _TRACE.set([])


def run(g, cypher: str, params: dict | None = None, tool: str = ""):
    """Execute a parameterized query and record it in the trace."""
    result = g.query(cypher, params or {})
    trace().append({"tool": tool, "cypher": " ".join(cypher.split()), "params": _short(params or {}),
                    "rows": len(result.result_set), "ms": round(result.run_time_ms, 2)})
    return result


def _short(params: dict) -> dict:
    """Trace-friendly params (embedding vectors are summarized)."""
    return {k: (f"<vector {len(v)}d>" if isinstance(v, list) and len(v) > 50 else v) for k, v in params.items()}


# ---------- path collection ----------

def node_view(n: Node) -> dict:
    label = n.labels[0] if n.labels else "?"
    p = n.properties
    key = p.get(NODE_KEYS.get(label, "id"))
    name = p.get("title") or p.get("login") or p.get("name") or p.get("version") or key
    if label == "KEP":
        name = f"{key} {p.get('title') or ''}".strip()
    if label == "Comment":
        name = f"comment ({p.get('kind')})"
    url = next((p[k] for k in URL_KEYS if p.get(k)), None)
    return {"id": n.id, "label": label, "key": key, "name": name, "url": url,
            "status": p.get("status"), "kind": p.get("kind")}


class PathCollector:
    """Walks result values (nodes, edges, paths, lists) and keeps unique nodes and edges for the UI."""

    def __init__(self):
        self.nodes: dict[int, dict] = {}
        self.edges: dict[int, dict] = {}

    def add(self, value) -> None:
        if value is None:
            return
        if isinstance(value, Node):
            if value.id not in self.nodes:
                self.nodes[value.id] = node_view(value)
        elif isinstance(value, Edge):
            self.edges[value.id] = {"id": value.id, "src": _id(value.src_node), "dst": _id(value.dest_node),
                                    "type": value.relation, "props": _edge_props(value.properties)}
        elif isinstance(value, Path):
            for n in value.nodes():
                self.add(n)
            for e in value.edges():
                self.add(e)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.add(v)

    def add_rows(self, rows) -> None:
        for row in rows:
            self.add(list(row))

    def view(self) -> dict:
        nodes = list(self.nodes.values())[:MAX_PATH_NODES]
        keep = {n["id"] for n in nodes}
        edges = [e for e in self.edges.values() if e["src"] in keep and e["dst"] in keep]
        return {"nodes": nodes, "edges": edges, "total_nodes": len(self.nodes)}


def _id(x) -> int:
    return x.id if isinstance(x, Node) else x


def _edge_props(props: dict) -> dict:
    out = {k: v for k, v in props.items() if k in ("role", "stage", "state", "valid_from", "valid_to", "former",
                                                     "verified", "source", "weight")}
    for k in ("valid_from", "valid_to"):
        if isinstance(out.get(k), int):
            out[k] = ts_to_date(out[k])
    return out


# ---------- time ----------

def ts_to_date(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None


def parse_as_of(g, value) -> tuple[int | None, str]:
    """None/'now' -> (None, 'current'); 'v1.29' -> release date; ISO date -> epoch seconds."""
    if value in (None, "", "now", "current"):
        return None, "current"
    s = str(value).strip()
    m = re.fullmatch(r"v?(1\.\d+)(?:\.\d+)?", s)
    if m:
        rows = run(g, "MATCH (r:Release {version: $v}) RETURN r.ts", {"v": f"v{m.group(1)}"}, "as_of").result_set
        if rows and rows[0][0]:
            return rows[0][0], f"release v{m.group(1)} ({ts_to_date(rows[0][0])})"
        raise ValueError(f"unknown release {s}")
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()), dt.strftime("%Y-%m-%d")


def valid_at(alias: str, as_of: int | None) -> str:
    """Cypher condition: interval edge `alias` holds at as_of (or is open now). Uses $as_of."""
    if as_of is None:
        return f"{alias}.valid_to IS NULL"
    return f"({alias}.valid_from <= $as_of AND ({alias}.valid_to IS NULL OR {alias}.valid_to > $as_of))"


def snapshot_cutoff(g) -> str | None:
    rows = run(g, "MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff", tool="meta").result_set
    return rows[0][0] if rows else None


# ---------- result ----------

class ToolCall:
    """Collects one tool's queries, path, sources and notes, then builds the common result shape."""

    def __init__(self, name: str, graph_name: str | None = None):
        self.name = name
        self.g = graph(graph_name)
        self.start = len(trace())
        self.path = PathCollector()
        self.sources: list[str] = []
        self.notes: list[str] = []
        self.stale: list[str] = []
        self.as_of_label = "current"

    def q(self, cypher: str, params: dict | None = None, collect: bool = True):
        result = run(self.g, cypher, params, self.name)
        if collect:
            self.path.add_rows(result.result_set)
        return result.result_set

    def source(self, *urls) -> None:
        for u in urls:
            if isinstance(u, (list, tuple)):
                self.source(*u)
            elif u and isinstance(u, str) and u.startswith("http") and u not in self.sources:
                self.sources.append(u)

    def result(self, data, found: bool = True) -> dict:
        for n in self.path.nodes.values():
            if n["url"] and n["label"] in ("KEP", "Ticket", "PR", "Comment", "Team", "CodeArea", "Document"):
                self.source(n["url"])
        view = self.path.view()
        if view["total_nodes"] > MAX_PATH_NODES:
            self.notes.append(f"graph view shows {MAX_PATH_NODES} of {view['total_nodes']} nodes")
        return {"tool": self.name, "found": found, "data": data, "notes": self.notes, "sources": self.sources,
                "path": view, "cypher_trace": trace()[self.start:], "as_of": self.as_of_label,
                "stale_flags": self.stale}
