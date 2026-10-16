"""Run one graph tool from the command line and print its result (for hand-checking).

Usage:
  python -m src.tools.cli trace_decision KEP-3063
  python -m src.tools.cli who_owns kubelet
  python -m src.tools.cli what_changed 2026-09-01 --team sig-node
  python -m src.tools.cli connect derekwaynecarr KEP-753
  python -m src.tools.cli recommend_reviewers KEP-4381
Multi-word arguments need quotes: who_knows "dynamic resource allocation".
Options: --graph NAME, --as_of DATE|vX.Y, --full (also print path and cypher trace)
"""
import json
import sys

from src.tools.registry import TOOLS as REGISTRY

TOOLS = {name: spec["fn"] for name, spec in REGISTRY.items()}


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in TOOLS:
        sys.exit(__doc__)
    name, args, kwargs, full = argv[0], [], {}, False
    i = 1
    while i < len(argv):
        if argv[i] == "--full":
            full = True
        elif argv[i].startswith("--"):
            kwargs[argv[i][2:] if argv[i] != "--graph" else "graph_name"] = argv[i + 1]
            i += 1
        else:
            args.append(argv[i])
        i += 1
    out = TOOLS[name](*args, **kwargs)
    if not full:
        out = {k: v for k, v in out.items() if k not in ("path", "cypher_trace")} | {
            "path": f"{len(out['path']['nodes'])} nodes, {len(out['path']['edges'])} edges",
            "cypher_trace": [f"{c['ms']} ms, {c['rows']} rows: {c['cypher'][:110]}" for c in out["cypher_trace"]]}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
