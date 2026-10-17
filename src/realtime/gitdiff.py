"""Live git diff: fetch kubernetes/enhancements and kubernetes/community, turn new commits into events.

Every 5 minutes (worker): `git fetch`, then the kep.yaml and sigs.yaml changes in last_sha..origin/HEAD
become events through the SAME diff code as the history backfill (src/normalize/history.py), so a live
change and a replayed one get the same event id. The last processed SHA per repo is kept in
Meta {key:'gitdiff'}; the first run starts from the graph's cutoff (first-parent commit before it).
Checked: over cutoff..HEAD this reproduces exactly the 222 post-cutoff backfill events.

Usage: python -m src.realtime.gitdiff --graph brain_live     (one run; the worker calls run_once)
"""
import subprocess
import sys

from src.brain.db import graph
from src.normalize.history import REPOS, kep_events, sig_events
from src.realtime import stream
from src.realtime.events import now_iso

REPOS_EVENTS = {"enhancements": kep_events, "community": sig_events}


def _git(repo: str, *args) -> str:
    return subprocess.run(["git", "-C", str(REPOS / repo), *args], capture_output=True, text=True,
                          check=True, timeout=120).stdout.strip()


def run_once(graph_name: str, fetch: bool = True) -> int:
    g = graph(graph_name)
    meta = g.query("MERGE (m:Meta {key:'gitdiff'}) RETURN m").result_set[0][0].properties
    cutoff = g.query("MATCH (m:Meta {key:'snapshot'}) RETURN m.cutoff").result_set[0][0]
    published = 0
    for repo, events_fn in REPOS_EVENTS.items():
        try:
            if fetch:
                _git(repo, "fetch", "--quiet", "origin")
            head = _git(repo, "rev-parse", "origin/HEAD")   # master (enhancements) or main (community)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"git diff: {repo} unavailable ({exc})", flush=True)
            continue
        base = meta.get(f"{repo}_sha") or _git(repo, "rev-list", "-1", "--first-parent", f"--before={cutoff}", head)
        if base == head:
            continue
        observed = now_iso(ms=True)
        for ev in events_fn(f"{base}..{head}", base, observed):
            stream.publish(ev, graph_name)
            published += 1
        g.query(f"MATCH (m:Meta {{key:'gitdiff'}}) SET m.{repo}_sha = $sha, m.checked_at = $now",
                {"sha": head, "now": observed})
    # a graph that follows git reads design docs at the latest commit when re-extracting
    g.query("MATCH (m:Meta {key:'snapshot'}) SET m.readme_commit = 'origin/HEAD'")
    return published


if __name__ == "__main__":
    argv = sys.argv[1:]
    name = argv[argv.index("--graph") + 1] if "--graph" in argv else "brain_live"
    print(f"published {run_once(name, fetch='--no-fetch' not in argv)} events to brain:events:{name}")
