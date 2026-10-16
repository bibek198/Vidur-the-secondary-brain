"""Ask the agent one question from the command line.

Usage:
  python -m src.agent.cli "Who owns the kubelet?"
  python -m src.agent.cli "What was the status of KEP-2699 as of v1.30?" --trace
Options: --graph NAME, --model ID, --trace (show every Cypher query), --no-remember (do not store
the question), --record (save the answer for replay mode). DEMO_REPLAY=1 serves recorded answers only.
"""
import sys

from src.agent import replay
from src.agent.loop import ask


def main(argv: list[str]) -> None:
    flags = {a for a in argv if a in ("--trace", "--no-remember", "--record")}
    opts, words, i = {}, [], 0
    while i < len(argv):
        if argv[i] in ("--graph", "--model"):
            opts[argv[i][2:]] = argv[i + 1]
            i += 1
        elif argv[i] not in flags:
            words.append(argv[i])
        i += 1
    if not words:
        sys.exit(__doc__)
    out = ask(" ".join(words), graph_name=opts.get("graph"), model=opts.get("model"),
              remember="--no-remember" not in flags)
    print(out["answer"], "\n")
    for c in out["citations"]:
        print(f"  [{c['n']}] {c['url']}")
    if out.get("replayed"):
        print(f"\n(replayed answer, recorded {out.get('asked_at', '?')})")
        for q in out.get("closest_recorded", []):
            print(f"  try: {q}")
        return
    print(f"\ntools: " + "; ".join(f"{c['name']}({', '.join(f'{k}={v!r}' for k, v in c['args'].items())})"
                                    f"{'' if c['found'] is not False else ' -> not found'}" for c in out["tool_calls"]))
    print(f"as of: {out['as_of']} · guard: {out['guard']} · {out['rounds']} rounds · {out['seconds']} s · "
          f"${out['cost_usd']:.4f} · graph path: {len(out['path']['nodes'])} nodes")
    if out["stale_flags"]:
        print("stale: " + "; ".join(out["stale_flags"]))
    if "--trace" in flags:
        for c in out["cypher_trace"]:
            print(f"\n// {c['tool']}: {c['rows']} rows, {c['ms']} ms  params={c['params']}\n{c['cypher']}")
    if "--record" in flags:
        replay.record(out)
        print("\nrecorded for replay mode")


if __name__ == "__main__":
    main(sys.argv[1:])
