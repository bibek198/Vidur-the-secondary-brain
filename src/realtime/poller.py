"""GitHub poller: new comments and issue/PR changes in kubernetes/enhancements, every 60 s.

Two calls per poll, both "updated since the cursor", oldest first:
  GET /repos/kubernetes/enhancements/issues/comments?since=...   (issue and PR conversation comments)
  GET /repos/kubernetes/enhancements/issues?state=all&since=...  (issues and PRs opened or closed)
Each request sends the previous ETag (If-None-Match); an unchanged answer is a 304, which does not
count against the rate limit. Rows are normalized with the same functions as the bulk data
(src/normalize/threads.py), so a comment seen by the poller and by the catch-up feed gets the same
event id. Cursor and ETags live in Meta {key:'poller'}; the first poll starts at the graph's cutoff.
Bots are skipped, as in the bulk data.

Usage: python -m src.realtime.poller --graph brain_live     (one poll; the worker calls poll_once)
"""
import json
import sys

from src.brain.db import graph
from src.fetch.github_api import GitHub
from src.normalize.threads import comment_row, thread_row
from src.realtime import stream
from src.realtime.events import Event, now_iso

REPO_API = "/repos/kubernetes/enhancements"


def comment_event(c: dict, keps: set[str], observed: str | None = None) -> Event | None:
    """GitHub issue-comment JSON -> comment_added (or comment_edited when updated after creation)."""
    number = c["issue_url"].rstrip("/").split("/")[-1]
    row = comment_row(c, number, keps)
    if not row["author"]:
        return None   # bot
    label = "PR" if "/pull/" in c["html_url"] else "Ticket"
    payload = {**row, "thread_label": label, "thread_number": int(number),
               "thread_url": f"https://github.com/kubernetes/enhancements/{'pull' if label == 'PR' else 'issues'}/{number}"}
    edited = c.get("updated_at") and c["updated_at"] != c["created_at"]
    return Event("github", "comment_edited" if edited else "comment_added", row["id"],
                 c["updated_at"] if edited else c["created_at"], payload, observed)


def thread_events(it: dict, keps: set[str], cursor: str, observed: str | None = None) -> list[Event]:
    """GitHub issue/PR JSON -> thread_opened (created after the cursor) and/or thread_closed."""
    label, row = thread_row(it, keps)
    out = []
    if it["created_at"] > cursor:
        out.append(Event("github", "thread_opened", row["id"], it["created_at"], {**row, "label": label}, observed))
    if it.get("closed_at") and it["closed_at"] > cursor:
        out.append(Event("github", "thread_closed", row["id"], it["closed_at"],
                         {"label": label, "url": it["html_url"], "title": it["title"]}, observed))
    return out


def _fetch(gh: GitHub, path: str, params: dict, etag: str | None):
    """All pages of a 'since' query; returns (items, new_etag, not_modified)."""
    r = gh.get(f"{REPO_API}{path}", params={**params, "per_page": 100}, etag=etag)
    if r.status_code == 304:
        return [], etag, True
    r.raise_for_status()
    items, new_etag = r.json(), r.headers.get("ETag")
    next_url = r.links.get("next", {}).get("url")
    while next_url:
        r = gh.get(next_url)
        r.raise_for_status()
        items += r.json()
        next_url = r.links.get("next", {}).get("url")
    return items, new_etag, False


def poll_once(graph_name: str, gh: GitHub | None = None) -> int:
    g = graph(graph_name)
    gh = gh or GitHub()
    meta = g.query("MERGE (m:Meta {key:'poller'}) RETURN m").result_set[0][0].properties
    cutoff = g.query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff").result_set[0][0]
    cursor = meta.get("cursor") or cutoff
    keps = {r[0] for r in g.query("MATCH (k:KEP) RETURN k.id").result_set}
    observed = now_iso(ms=True)
    events, newest = [], cursor
    calls = {"comments": ("/issues/comments", {"since": cursor, "sort": "updated", "direction": "asc"}),
             "threads": ("/issues", {"since": cursor, "state": "all", "sort": "updated", "direction": "asc"})}
    etags = json.loads(meta.get("etags") or "{}")
    for name, (path, params) in calls.items():
        key = f"{name}|{cursor}"   # an ETag is only valid for the exact same URL (same cursor)
        items, etag, not_modified = _fetch(gh, path, params, etags.get(key))
        etags = {key: etag} | {k: v for k, v in etags.items() if k.startswith(name) is False}
        if not_modified:
            continue
        for it in items:
            newest = max(newest, it.get("updated_at") or cursor)
            if name == "comments":
                ev = comment_event(it, keps, observed)
                events += [ev] if ev else []
            else:
                events += thread_events(it, keps, cursor, observed)
    events.sort(key=lambda e: (e.occurred_at, e.kind != "thread_opened"))
    for ev in events:
        stream.publish(ev, graph_name)
    g.query("MATCH (m:Meta {key:'poller'}) SET m.cursor = $c, m.etags = $e, m.polled_at = $now, m.last_found = $n",
            {"c": newest, "e": json.dumps(etags), "now": observed, "n": len(events)})
    return len(events)


if __name__ == "__main__":
    argv = sys.argv[1:]
    name = argv[argv.index("--graph") + 1] if "--graph" in argv else "brain_live"
    print(f"published {poll_once(name)} events to brain:events:{name}")
