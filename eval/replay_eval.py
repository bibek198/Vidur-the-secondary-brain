"""Time-travel replay eval: does the brain stay correct as two years of real changes stream in?

1. brain_replay is loaded as of T0 = 2024-09-15 (the normal loader with --cutoff).
2. Every real change after T0 (data/seed/history_events.jsonl, from git) is published to the graph's Redis
   Stream and applied by the same consume + handler path the live worker uses, checkpoint by checkpoint.
3. At each release v1.32 ... v1.37, the status of 15 KEPs is checked against TRUTH FROM GIT: kep.yaml at the
   last first-parent commit on that date (eval/truth.py), never from the graph.
   - graph: the trace_decision tool's current status (deterministic, no LLM).
   - vector, stale index: the baseline with only comment passages dated <= T0 (index never refreshed).
   - vector, appended index: the baseline with passages dated <= the checkpoint (index kept up to date).
   Design-doc passages carry no date and are the docs as fetched in 2026, so both vector variants can see
   text newer than the checkpoint: a leak in the baseline's favour, reported as such.
4. Reported: accuracy per checkpoint, "old status stated as current" errors, and the per-event lag from
   publishing an event to the graph answering with it.

Usage: python eval/replay_eval.py [--skip-load] [--no-baseline]
"""
import json
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.truth import commit_at, kep_at, kep_files, status_of  # noqa: E402
from src.brain.db import client, graph  # noqa: E402
from src.realtime import stream  # noqa: E402
from src.realtime.events import iso_to_ts  # noqa: E402
from src.realtime.handlers import apply_event  # noqa: E402
from src.tools.base import reset_trace  # noqa: E402
from src.tools.registry import call_tool  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
G = "brain_replay"
T0 = "2024-09-15T00:00:00Z"
CHECKPOINTS = {"v1.32": "2024-12-11", "v1.33": "2025-04-23", "v1.34": "2025-08-27", "v1.35": "2025-12-17",
               "v1.36": "2026-04-22", "v1.37": "2026-08-26"}
SIGS = {"sig-node", "sig-scheduling", "sig-storage", "sig-auth", "sig-api-machinery", "sig-cli"}
STATUS = ("provisional", "implementable", "implemented", "withdrawn", "rejected", "replaced", "deferred")


def pick_keps() -> list[int]:
    """15 KEPs from the scoped SIGs: 12 whose status changed between T0 and the last checkpoint, 3 that did not."""
    start = commit_at("enhancements", T0)
    end = commit_at("enhancements", CHECKPOINTS["v1.37"] + "T23:59:59Z")
    changed, stable = [], []
    for n in kep_files(end):
        d_end = kep_at(n, end)
        if not d_end or d_end.get("owning-sig") not in SIGS:
            continue
        a, b = status_of(kep_at(n, start)), status_of(d_end)
        if a and b and a != b:
            changed.append(n)
        elif a and b:
            stable.append(n)
    rng = random.Random(11)
    return sorted(rng.sample(sorted(changed), 12) + rng.sample(sorted(stable), 3))


def truth_table(keps: list[int]) -> dict:
    out = {}
    for rel, day in CHECKPOINTS.items():
        c = commit_at("enhancements", day + "T23:59:59Z")
        out[rel] = {n: status_of(kep_at(n, c)) for n in keps}
    out["T0"] = {n: status_of(kep_at(n, commit_at("enhancements", T0))) for n in keps}
    return out


def first_status(text: str) -> str:
    low = text.lower()
    hits = [(low.find(s), s) for s in STATUS if s in low]
    return min(hits)[1] if hits else "unknown"


def graph_status(kep: int) -> str | None:
    reset_trace()
    r = call_tool("trace_decision", {"kep_or_query": f"KEP-{kep}"}, graph_name=G)
    return r["data"].get("status") if r["found"] else None


def baseline_status(kep: int, title: str, as_of_day: str) -> str:
    from src.baseline import answer_baseline
    q = (f"What is the current status of KEP-{kep} ({title})? Answer with one of: provisional, implementable, "
         "implemented, withdrawn, rejected, replaced, deferred, or unknown, then one sentence why.")
    return first_status(answer_baseline(q, "brain_eval", as_of=as_of_day)["answer"].split("Note: vector search")[0])


def main(argv: list[str]) -> None:
    keps = pick_keps()
    truth = truth_table(keps)
    titles = {n: str((kep_at(n, commit_at("enhancements", CHECKPOINTS["v1.37"] + "T23:59:59Z")) or {}).get("title", "")) for n in keps}
    print("KEPs:", keps)
    if "--skip-load" not in argv:
        c = client()
        if G in c.list_graphs():
            c.connection.execute_command("GRAPH.DELETE", G)
        stream.reset(G)
        subprocess.run([sys.executable, "-m", "src.load.loader", "--graph", G, "--cutoff", T0], check=True, cwd=ROOT,
                       stdout=subprocess.DEVNULL)
    g = graph(G)
    events = [json.loads(line) for line in (ROOT / "data" / "seed" / "history_events.jsonl").open()]
    after = sorted((e for e in events if e["occurred_at"] > T0), key=lambda e: e["occurred_at"])
    rows, lags, prev = [], [], T0
    for rel, day in CHECKPOINTS.items():
        batch = [e for e in after if prev < e["occurred_at"] <= day + "T23:59:59Z"]
        t0 = time.time()
        for e in batch:
            stream.publish(e, G)
        applied = 0
        while applied < len(batch):
            applied += stream.consume(G, lambda ev: apply_event(g, ev, live=False), block_ms=200, count=500)
        batch_s = time.time() - t0
        # event-to-correct-answer lag: one status change of this window, published alone, then queried
        probe = next((e for e in batch if e["kind"] == "kep_status_changed"), None)
        if probe:
            t1 = time.time()
            stream.publish({**probe, "event_id": probe["event_id"]}, G)       # same id: an idempotent re-apply
            stream.consume(G, lambda ev: apply_event(g, ev, live=False), block_ms=200)
            reset_trace()
            call_tool("trace_decision", {"kep_or_query": probe["entity_key"]}, graph_name=G)
            lags.append((time.time() - t1) * 1000)
        for n in keps:
            row = {"checkpoint": rel, "kep": n, "truth": truth[rel][n], "t0_status": truth["T0"][n],
                   "graph": graph_status(n)}
            if "--no-baseline" not in argv:
                row["vector_stale"] = baseline_status(n, titles[n], T0[:10])
                row["vector_appended"] = baseline_status(n, titles[n], day)
            rows.append(row)
        acc = sum(r["graph"] == r["truth"] for r in rows if r["checkpoint"] == rel)
        print(f"{rel}: {len(batch)} events applied in {batch_s:.1f}s; graph {acc}/{len(keps)}", flush=True)
        prev = day + "T23:59:59Z"

    systems = ["graph"] + ([] if "--no-baseline" in argv else ["vector_stale", "vector_appended"])
    summary = {"keps": keps, "events_replayed": len([e for e in after if e["occurred_at"] <= prev]),
               "median_event_to_answer_ms": round(statistics.median(lags), 1) if lags else None,
               "per_checkpoint": {}, "totals": {}}
    for rel in CHECKPOINTS:
        sel = [r for r in rows if r["checkpoint"] == rel]
        summary["per_checkpoint"][rel] = {s: sum(r[s] == r["truth"] for r in sel) for s in systems}
    for s in systems:
        wrong_old = sum(1 for r in rows if r[s] != r["truth"] and r[s] == r["t0_status"] and r["t0_status"] != r["truth"])
        summary["totals"][s] = {"correct": sum(r[s] == r["truth"] for r in rows), "n": len(rows),
                                "unknown": sum(r[s] in (None, "unknown") for r in rows), "old_status_as_current": wrong_old}
    out = ROOT / "eval" / "results" / f"replay_{time.strftime('%Y%m%d-%H%M')}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary["totals"], indent=1), "\nper checkpoint:", summary["per_checkpoint"],
          f"\nmedian event-to-answer lag: {summary['median_event_to_answer_ms']} ms -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
