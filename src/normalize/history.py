"""Git history -> data/seed/history_events.jsonl (dated change events, oldest first).

Walks the first-parent history of master, so each change is dated by when it landed
(the merge commit) and linked to its PR. Each kep.yaml version is parsed and compared
with the previous version of the same KEP (keyed by kep-number, so moves between SIG
folders are followed). Each sigs.yaml version is compared for leadership and team changes.

These are the same Event records the live engine produces, so the history backfill and
the replay eval exercise the real handlers.

Usage: python -m src.normalize.history
"""
import json
import re
import subprocess
from pathlib import Path

from src.normalize.common import clean_handle, load_yaml
from src.normalize.keps import parse_kep
from src.realtime.events import Event, to_utc_iso

ROOT = Path(__file__).resolve().parents[2]
REPOS = ROOT / "data" / "raw" / "repos"
SEED = ROOT / "data" / "seed"
_PR = re.compile(r"Merge pull request #(\d+)")


class BlobReader:
    """Reads file contents at given commits through one long-running `git cat-file --batch`."""

    def __init__(self, repo: Path):
        self.p = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, rev: str, path: str) -> str | None:
        self.p.stdin.write(f"{rev}:{path}\n".encode())
        self.p.stdin.flush()
        header = self.p.stdout.readline().decode().split()
        if len(header) < 3 or header[1] == "missing":
            return None
        data = self.p.stdout.read(int(header[2]))
        self.p.stdout.read(1)  # trailing newline
        return data.decode("utf-8", "replace")

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def first_parent_commits(repo: Path, pathspec: str):
    """Yield (sha, iso_date, pr_number, [(status, path, old_path)]) oldest first."""
    out = subprocess.run(
        ["git", "log", "--reverse", "--first-parent", "-m", "--format=C %H %cI %s", "--name-status",
         "-M", "--", pathspec], cwd=repo, check=True, capture_output=True, text=True).stdout
    commit = None
    for line in out.splitlines():
        if line.startswith("C "):
            if commit:
                yield commit
            _, sha, date, *subject = line.split(" ")
            m = _PR.search(" ".join(subject))
            commit = (sha, to_utc_iso(date), int(m.group(1)) if m else None, [])
        elif line.strip() and commit:
            parts = line.split("\t")
            status = parts[0][0]
            commit[3].append((status, parts[-1], parts[1] if status == "R" else None))
    if commit:
        yield commit


TRACKED = ("status", "stage", "latest_milestone", "owning_sig", "title")
SETS = ("approvers", "superseded_by", "replaces")


def kep_events() -> list[Event]:
    repo = REPOS / "enhancements"
    reader = BlobReader(repo)
    state: dict[str, dict] = {}       # KEP id -> last parsed record
    path_to_id: dict[str, str] = {}
    events = []
    for sha, when, pr, changes in first_parent_commits(repo, "keps/*/*/kep.yaml"):
        src = {"commit": sha, "pr": pr,
               "url": f"https://github.com/kubernetes/enhancements/pull/{pr}" if pr else
                      f"https://github.com/kubernetes/enhancements/commit/{sha}"}
        for status, path, old_path in changes:
            if status == "D":
                kid = path_to_id.pop(path, None)
                if kid and kid in state:
                    events.append(Event("git", "kep_file_removed", kid, when, {**src, "path": path}, when))
                continue
            text = reader.read(sha, path)
            try:
                rec = parse_kep(load_yaml(text), path.rsplit("/", 1)[0]) if text else None
            except Exception:  # noqa: BLE001 - unparseable version: skip it, keep the previous state
                rec = None
            if not rec:
                continue
            kid = rec["id"]
            path_to_id[path] = kid
            if old_path:
                path_to_id.pop(old_path, None)
            prev = state.get(kid)
            if prev is None:
                snapshot = {k: rec[k] for k in TRACKED + SETS + ("milestones", "authors", "path")}
                events.append(Event("git", "kep_created", kid, when, {**src, **snapshot}, when))
            else:
                for field in TRACKED:
                    if rec[field] != prev[field] and rec[field] is not None:
                        events.append(Event("git", f"kep_{field}_changed", kid, when,
                                            {**src, "from": prev[field], "to": rec[field]}, when))
                for stage in sorted(set(rec["milestones"]) | set(prev["milestones"])):
                    a, b = prev["milestones"].get(stage), rec["milestones"].get(stage)
                    if a != b:
                        events.append(Event("git", "kep_milestone_changed", kid, when,
                                            {**src, "stage": stage, "from": a, "to": b}, when))
                for field in SETS:
                    added = [x for x in rec[field] if x not in prev[field]]
                    removed = [x for x in prev[field] if x not in rec[field]]
                    if added or removed:
                        events.append(Event("git", f"kep_{field}_changed", kid, when,
                                            {**src, "added": added, "removed": removed}, when))
            state[kid] = rec
    reader.close()
    return events


def _leads(group: dict) -> set[tuple[str, str]]:
    out = set()
    for role in ("chairs", "tech_leads"):
        for p in (group.get("leadership") or {}).get(role) or []:
            login = clean_handle((p or {}).get("github"))
            if login:
                out.add((login, role.rstrip("s")))
    return out


def _groups(text: str) -> dict[str, dict]:
    data = load_yaml(text) or {}
    return {g["dir"]: g for kind in ("sigs", "workinggroups", "committees")
            for g in (data.get(kind) or []) if isinstance(g, dict) and g.get("dir")}


def sig_events() -> list[Event]:
    repo = REPOS / "community"
    reader = BlobReader(repo)
    prev: dict[str, dict] | None = None
    events = []
    for sha, when, pr, _ in first_parent_commits(repo, "sigs.yaml"):
        text = reader.read(sha, "sigs.yaml")
        try:
            cur = _groups(text) if text else None
        except Exception:  # noqa: BLE001
            cur = None
        if cur is None:
            continue
        src = {"commit": sha, "pr": pr,
               "url": f"https://github.com/kubernetes/community/pull/{pr}" if pr else
                      f"https://github.com/kubernetes/community/commit/{sha}"}
        if prev is not None:
            for team in sorted(set(cur) | set(prev)):
                if team not in prev:
                    events.append(Event("git", "team_created", team, when, {**src, "name": cur[team].get("name")}, when))
                elif team not in cur:
                    events.append(Event("git", "team_retired", team, when, src, when))
                    continue
                before = _leads(prev.get(team, {}))
                after = _leads(cur[team])
                for login, role in sorted(after - before):
                    events.append(Event("git", "team_lead_added", team, when, {**src, "login": login, "role": role}, when))
                for login, role in sorted(before - after):
                    events.append(Event("git", "team_lead_removed", team, when, {**src, "login": login, "role": role}, when))
                sp_before = {s["name"] for s in prev.get(team, {}).get("subprojects") or [] if isinstance(s, dict)}
                sp_after = {s["name"] for s in cur[team].get("subprojects") or [] if isinstance(s, dict)}
                for name in sorted(sp_after - sp_before):
                    events.append(Event("git", "subproject_added", team, when, {**src, "subproject": name}, when))
                for name in sorted(sp_before - sp_after):
                    events.append(Event("git", "subproject_removed", team, when, {**src, "subproject": name}, when))
        else:  # first version: everything that exists is "created" at that date
            for team, g in sorted(cur.items()):
                events.append(Event("git", "team_created", team, when, {**src, "name": g.get("name")}, when))
                for login, role in sorted(_leads(g)):
                    events.append(Event("git", "team_lead_added", team, when, {**src, "login": login, "role": role}, when))
        prev = cur
    reader.close()
    return events


def main() -> None:
    SEED.mkdir(parents=True, exist_ok=True)
    events = sorted(kep_events() + sig_events(), key=lambda e: (e.occurred_at, e.entity_key, e.kind))
    out = SEED / "history_events.jsonl"
    with out.open("w") as fh:
        for e in events:
            fh.write(e.to_json() + "\n")
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    print(f"{len(events)} events -> {out.relative_to(ROOT)} ({events[0].occurred_at[:10]} .. {events[-1].occurred_at[:10]})")
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k:28}{n}")


if __name__ == "__main__":
    main()
