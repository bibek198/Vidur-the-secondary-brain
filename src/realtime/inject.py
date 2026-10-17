"""Put events on a graph's stream by hand: the demo fallback, the catch-up feed and tests.

  python -m src.realtime.inject status KEP-2699 withdrawn [--graph brain_demo]
  python -m src.realtime.inject comment KEP-2699 "We decided to withdraw this." [--author alice] [--graph ...]
  python -m src.realtime.inject feed [--graph brain_live] [--pace 0.5] [--limit N]

`feed` replays the real changes after the graph's cutoff, in time order: git-history events
(status, milestones, leads, new KEPs), new issues/PRs and new comments from data/seed. Event ids
are deterministic, so running the feed twice changes nothing. The worker must be running
(python -m src.realtime.worker --graph <name>) to apply them.
"""
import gzip
import hashlib
import json
import sys
import time
from pathlib import Path

from src.brain.db import graph
from src.realtime import stream
from src.realtime.events import Event, iso_to_ts, now_iso

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data" / "seed"


def status_event(kep: str, status: str, graph_name: str, url: str | None = None) -> Event:
    rows = graph(graph_name).query("MATCH (k:KEP {id:$k}) RETURN k.status", {"k": kep}).result_set
    return Event("inject", "kep_status_changed", kep, now_iso(),
                 {"from": rows[0][0] if rows else None, "to": status, "url": url})


def comment_event(kep: str, text: str, author: str = "demo-user") -> Event:
    number = kep.split("-")[1]
    cid = "comment:inject-" + hashlib.sha256(f"{kep}|{text}".encode()).hexdigest()[:10]
    return Event("inject", "comment_added", cid, now_iso(),
                 {"kind": "issue", "thread": f"enhancements#{number}", "thread_label": "Ticket",
                  "thread_number": int(number), "thread_url": f"https://github.com/kubernetes/enhancements/issues/{number}",
                  "author": author, "body": text, "url": None, "mentions": [kep]})


def _rows(name: str):
    with gzip.open(SEED / "threads" / name, "rt") as fh:
        for line in fh:
            yield json.loads(line)


def feed_events(cutoff_ts: int) -> list[dict]:
    """All real changes after the cutoff, oldest first, as event dicts (observed now)."""
    observed = now_iso(ms=True)
    out = []
    for line in (SEED / "history_events.jsonl").open():
        ev = json.loads(line)
        if iso_to_ts(ev["occurred_at"]) > cutoff_ts:
            out.append({**ev, "observed_at": observed})
    pr_ids = set()
    for name, label in (("tickets.jsonl.gz", "Ticket"), ("prs.jsonl.gz", "PR")):
        for row in _rows(name):
            if label == "PR":
                pr_ids.add(row["id"])
            if row.get("created_ts") and row["created_ts"] > cutoff_ts:
                e = Event("github", "thread_opened", row["id"], row["created_at"], {**row, "label": label}, observed)
                out.append(json.loads(e.to_json()))
    for row in _rows("comments.jsonl.gz"):
        if row.get("at_ts") and row["at_ts"] > cutoff_ts:
            number = int(row["thread"].split("#")[1])
            label = "PR" if row["thread"] in pr_ids else "Ticket"
            payload = {**row, "thread_label": label, "thread_number": number,
                       "thread_url": f"https://github.com/kubernetes/enhancements/{'pull' if label == 'PR' else 'issues'}/{number}"}
            e = Event("github", "comment_added", row["id"], row["at"], payload, observed)
            out.append(json.loads(e.to_json()))
    out.sort(key=lambda e: (e["occurred_at"], e["kind"] != "thread_opened"))  # a thread before its comments
    return out


def main(argv: list[str]) -> None:
    opts = {argv[i][2:]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or not argv[i - 1].startswith("--"))]
    graph_name = opts.get("graph", "brain_demo")
    if not args:
        sys.exit(__doc__)
    if args[0] == "status":
        ev = status_event(args[1], args[2], graph_name)
        print(stream.publish(ev, graph_name), ev.kind, ev.entity_key, ev.payload)
    elif args[0] == "comment":
        ev = comment_event(args[1], args[2], opts.get("author", "demo-user"))
        print(stream.publish(ev, graph_name), ev.kind, ev.entity_key)
    elif args[0] == "feed":
        cutoff = graph(graph_name).query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff_ts").result_set[0][0]
        events = feed_events(cutoff)[: int(opts["limit"]) if "limit" in opts else None]
        pace = float(opts.get("pace", 0))
        for ev in events:
            stream.publish(ev, graph_name)
            if pace:
                time.sleep(pace)
        print(f"published {len(events)} events after the cutoff to brain:events:{graph_name}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
