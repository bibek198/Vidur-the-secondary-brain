"""The real-time worker: one simple loop that keeps one graph current.

Each turn of the loop:
  1. apply new events from the stream (up to 1 s of waiting), through the same handlers the loader uses
  2. (background thread) re-extract rationales whose debounce time has passed, so a slow LLM call
     never delays applying events
  3. every 60 s: poll GitHub for new comments and issue/PR changes (src/realtime/poller.py)
  4. every 5 min: git fetch + diff kep.yaml and sigs.yaml (src/realtime/gitdiff.py)
  5. every 10 min or 500 events: recompute derived reviewer edges (for PageRank)
If FalkorDB goes away (restart), the loop waits and reconnects; unacknowledged events are
redelivered from the stream, which persists across restarts.

Usage: python -m src.realtime.worker --graph brain_live [--no-poll] [--no-gitdiff] [--once]
  --once: apply everything currently in the stream, then exit (feeds, tests)
"""
import sys
import threading
import time

import redis

from src.brain.db import graph
from src.realtime import reextract, stream
from src.realtime.handlers import apply_event

POLL_EVERY_S, GITDIFF_EVERY_S, DERIVED_EVERY_S, DERIVED_EVERY_EVENTS = 60, 300, 600, 500


def _reextract_loop(graph_name: str, totals: dict) -> None:
    while True:
        try:
            totals["reextracted"] += len(reextract.run_due(graph_name))
        except Exception as exc:  # noqa: BLE001 - keep the thread alive; the KEP stays stale
            print(f"re-extraction failed: {exc}", flush=True)
        time.sleep(2)


def run(graph_name: str, poll: bool = True, gitdiff: bool = True, once: bool = False) -> dict:
    g = graph(graph_name)
    totals = {"events": 0, "reextracted": 0}
    if not once:
        threading.Thread(target=_reextract_loop, args=(graph_name, totals), daemon=True).start()
    last = {"poll": 0.0, "gitdiff": 0.0, "derived": time.time()}
    since_derived = 0
    while True:
        try:
            n = stream.consume(graph_name, lambda ev: apply_event(g, ev, live=True), block_ms=200 if once else 1000)
            totals["events"] += n
            since_derived += n
            now = time.time()
            if poll and now - last["poll"] >= POLL_EVERY_S:
                from src.realtime import poller
                last["poll"] = now
                published = poller.poll_once(graph_name)
                if published:
                    print(f"poller: {published} new events", flush=True)
            if gitdiff and now - last["gitdiff"] >= GITDIFF_EVERY_S:
                from src.realtime import gitdiff as gd
                last["gitdiff"] = now
                published = gd.run_once(graph_name)
                if published:
                    print(f"git diff: {published} new events", flush=True)
            if since_derived >= DERIVED_EVERY_EVENTS or (since_derived and now - last["derived"] >= DERIVED_EVERY_S):
                from src.load.loader import load_derived
                load_derived(g)
                since_derived, last["derived"] = 0, now
            if n:
                print(f"applied {n} events (total {totals['events']}) {stream.stats(graph_name)}", flush=True)
            if once and n == 0:
                return totals
        except (redis.ConnectionError, ConnectionError, OSError) as exc:
            print(f"FalkorDB unavailable ({exc}); retrying in 2 s", flush=True)
            time.sleep(2)
            g = graph(graph_name)


if __name__ == "__main__":
    argv = sys.argv[1:]
    name = argv[argv.index("--graph") + 1] if "--graph" in argv else "brain_live"
    print(f"worker for graph '{name}'", flush=True)
    print(run(name, poll="--no-poll" not in argv, gitdiff="--no-gitdiff" not in argv, once="--once" in argv))
