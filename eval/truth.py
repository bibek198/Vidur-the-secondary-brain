"""Ground truth for the evaluation, read straight from the source repositories with git, never from the graph.

Deliberately independent of the project's pipeline: it does not import src/normalize or src/load and does
not query FalkorDB. It reads files with `git show <commit>:<path>` and parses them with plain YAML
(BaseLoader: every value stays a string), so a bug in the loader cannot make the truth agree with the graph.
The hand-verified gold labels (eval/gold_decisions.csv) are the truth for decision outcomes.
"""
import functools
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPOS = ROOT / "data" / "raw" / "repos"
STATUS_WORDS = ("provisional", "implementable", "implemented", "withdrawn", "rejected", "replaced", "deferred")
# kep.yaml values that mean the same as a STATUS_WORD (typos and synonyms seen in the repo)
STATUS_ALIASES = {"superseded": "replaced", "removed": "withdrawn", "imlpemented": "implemented"}


def git(repo: str, *args) -> str:
    return subprocess.run(["git", "-C", str(REPOS / repo), *args], capture_output=True, text=True, check=True).stdout


def commit_at(repo: str, iso: str) -> str:
    """Last first-parent commit on the default branch at or before a date."""
    return git(repo, "rev-list", "-1", "--first-parent", f"--before={iso}", "origin/HEAD").strip()


@functools.lru_cache(maxsize=None)
def kep_files(commit: str) -> dict[int, str]:
    """kep-number -> path of its kep.yaml at a commit (from the folder name '<number>-<slug>')."""
    out = {}
    for path in git("enhancements", "ls-tree", "-r", "--name-only", commit, "keps").splitlines():
        m = re.match(r"keps/[^/]+/(?:[^/]+/)?(\d+)-[^/]+/kep\.yaml$", path)
        if m:
            out.setdefault(int(m.group(1)), path)
    return out


@functools.lru_cache(maxsize=None)
def kep_at(number: int, commit: str) -> dict | None:
    path = kep_files(commit).get(number)
    if not path:
        return None
    try:
        data = yaml.load(git("enhancements", "show", f"{commit}:{path}"), Loader=yaml.BaseLoader) or {}
    except (subprocess.CalledProcessError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def status_of(data: dict | None) -> str | None:
    if not data or not data.get("status"):
        return None
    raw = str(data["status"]).split("#")[0].strip().lower()
    return STATUS_ALIASES.get(raw, raw)


def kep_numbers(values) -> list[int]:
    """KEP references in replaces / superseded-by: '#1205', 'KEP-1205', '1205-bound-...', 'keps/sig-x/1205-...'.
    URLs to other repositories are skipped: kubernetes/community/pull/2314 is a PR, not KEP-2314."""
    out = []
    for v in values or []:
        s = str(v)
        if "://" in s and "kubernetes/enhancements" not in s:
            continue   # a link to something else (e.g. a kubernetes/community pull request), not a KEP
        m = re.search(r"(?:^|[#/ -])(\d{2,5})(?:-|$)", str(v)) or re.search(r"\b(\d{2,5})\b", str(v))
        if m:
            out.append(int(m.group(1)))
    return out


def logins(values) -> set[str]:
    return {str(v).strip().lstrip("@").lower() for v in values or [] if str(v).strip()}


def code_owner(path: str, commit: str) -> list[str]:
    """SIGs whose subprojects list kubernetes/kubernetes/<path>/OWNERS in sigs.yaml at a commit."""
    data = yaml.load(git("community", "show", f"{commit}:sigs.yaml"), Loader=yaml.BaseLoader)
    sigs = set()
    for group in data.get("sigs", []):
        for sp in group.get("subprojects", []) or []:
            if any(o.rstrip("/").endswith(f"/kubernetes/master/{path}/OWNERS") for o in sp.get("owners", []) or []):
                sigs.add(group["dir"])
    return sorted(sigs)
