"""MCP server: the same graph tools for any MCP client (Claude Desktop, Claude Code, the MCP Inspector).

Each registry tool gets a small typed wrapper, because the MCP SDK builds the tool's input schema from
the function signature. Descriptions come from src/tools/registry.py, so the agent and MCP clients
see the same catalogue. Results are returned without the large `path` (the UI uses it, chat clients
do not need it); the Cypher trace is kept so a client can show how the answer was found.

Run: python -m src.mcp_server   or   python /absolute/path/src/mcp_server.py   (stdio)
Inspect: npx @modelcontextprotocol/inspector .venv/bin/python -m src.mcp_server
Claude Desktop starts servers in another folder and ignores `cwd`, so this file puts the repo root on
sys.path and changes into it (for .env) before importing the project.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from mcp.server.mcpserver import MCPServer  # noqa: E402

from src.tools.registry import TOOLS, call_tool  # noqa: E402

server = MCPServer("decision-brain", instructions=(
    "Decision Brain: a knowledge graph of the Kubernetes project as a company (teams, people, code ownership, "
    "decisions/KEPs and their history, design docs and issue/PR discussions). Answer only from tool results, cite the returned source URLs, and say "
    "'not in the graph' when a tool returns found=false."))


def _run(name: str, **args) -> dict:
    out = call_tool(name, {k: v for k, v in args.items() if v is not None})
    out.pop("path", None)
    return out


def trace_decision(kep_or_query: str, as_of: str | None = None) -> dict:
    return _run("trace_decision", kep_or_query=kep_or_query, as_of=as_of)


def who_owns(target: str, as_of: str | None = None) -> dict:
    return _run("who_owns", target=target, as_of=as_of)


def who_knows(topic: str, limit: int | None = None) -> dict:
    return _run("who_knows", topic=topic, limit=limit)


def impact_of(entity: str, depth: int | None = None) -> dict:
    return _run("impact_of", entity=entity, depth=depth)


def timeline(kep: str, as_of: str | None = None) -> dict:
    return _run("timeline", kep=kep, as_of=as_of)


def what_changed(since: str, team: str | None = None, kep: str | None = None, limit: int | None = None) -> dict:
    return _run("what_changed", since=since, team=team, kep=kep, limit=limit)


def connect(a: str, b: str) -> dict:
    return _run("connect", a=a, b=b)


def influence(team: str | None = None, metric: str | None = None, limit: int | None = None) -> dict:
    return _run("influence", team=team, metric=metric, limit=limit)


def resolve(text: str) -> dict:
    return _run("resolve", text=text)


def recommend_reviewers(kep_or_path: str, limit: int | None = None) -> dict:
    return _run("recommend_reviewers", kep_or_path=kep_or_path, limit=limit)


def draft_decision_brief(kep: str) -> dict:
    return _run("draft_decision_brief", kep=kep)


def route_question(question: str, topic: str) -> dict:
    return _run("route_question", question=question, topic=topic)


def watch(entity: str) -> dict:
    return _run("watch", entity=entity)


def search(query: str, kep: str | None = None, kind: str | None = None, as_of: str | None = None) -> dict:
    return _run("search", query=query, kep=kep, kind=kind, as_of=as_of)


def read_thread(ref: str) -> dict:
    return _run("read_thread", ref=ref)


def read_kep(kep: str, section: str | None = None) -> dict:
    return _run("read_kep", kep=kep, section=section)


WRAPPERS = {f.__name__: f for f in (trace_decision, who_owns, who_knows, impact_of, timeline, what_changed,
                                     connect, influence, search, read_thread, read_kep, resolve,
                                     recommend_reviewers, draft_decision_brief,
                                     route_question, watch)}
assert set(WRAPPERS) == set(TOOLS), "every registry tool needs an MCP wrapper"
for name, fn in WRAPPERS.items():
    server.add_tool(fn, name=name, description=TOOLS[name]["description"])


if __name__ == "__main__":
    server.run("stdio")
