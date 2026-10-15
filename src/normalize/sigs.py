"""sigs.yaml + OWNERS files -> data/seed/teams.jsonl, people.jsonl, code_areas.jsonl.

- teams.jsonl: one record per SIG / working group / committee, with leads and subprojects.
- people.jsonl: login + company for SIG leaders (names and emails are dropped, rule 6).
- code_areas.jsonl: one record per OWNERS file: the code path a subproject owns and who approves
  and reviews it (aliases expanded; emeritus approvers kept as former owners).

Usage: python -m src.normalize.sigs
"""
import json
from pathlib import Path

from src.fetch.owners import OUT as OWNERS_DIR
from src.fetch.owners import local_path
from src.normalize.common import clean_handle, handles, load_yaml

ROOT = Path(__file__).resolve().parents[2]
SIGS = ROOT / "data" / "raw" / "repos" / "community" / "sigs.yaml"
SEED = ROOT / "data" / "seed"
KINDS = {"sigs": "sig", "workinggroups": "wg", "committees": "committee"}


def _aliases(org: str, repo: str) -> dict[str, list[str]]:
    f = OWNERS_DIR / org / repo / "OWNERS_ALIASES"
    if not f.exists():
        return {}
    try:
        data = load_yaml(f.read_text()) or {}
    except Exception:  # noqa: BLE001 - malformed alias file: treat as no aliases
        return {}
    aliases = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(aliases, dict):
        return {}
    return {str(k).lower(): handles(v) for k, v in aliases.items()}


def _expand(values, aliases: dict) -> list[str]:
    out = []
    for v in values or []:
        key = str(v).split("#")[0].strip().lower()
        for login in aliases.get(key) or handles([v]):
            if login not in out:
                out.append(login)
    return out


def parse_owners(url: str, alias_cache: dict) -> dict | None:
    f = local_path(url)
    if not f.exists():
        return None
    org, repo, branch, *rest = url.split("/")[3:]
    path = "/".join(rest[:-1]) or "."
    if (org, repo) not in alias_cache:
        alias_cache[(org, repo)] = _aliases(org, repo)
    aliases = alias_cache[(org, repo)]
    try:
        data = load_yaml(f.read_text()) or {}
    except Exception:  # noqa: BLE001 - a malformed OWNERS file is skipped, not fatal
        return None
    if not isinstance(data, dict):
        return None
    return {
        "id": f"{org}/{repo}:{path}",
        "repo": f"{org}/{repo}",
        "path": path,
        "url": f"https://github.com/{org}/{repo}/tree/{branch}/{path}",
        "owners_url": url,
        "approvers": _expand(data.get("approvers"), aliases),
        "reviewers": _expand(data.get("reviewers"), aliases),
        "emeritus_approvers": _expand(data.get("emeritus_approvers"), aliases),
        "labels": [str(x) for x in data.get("labels") or []],
    }


def main() -> None:
    SEED.mkdir(parents=True, exist_ok=True)
    data = load_yaml(SIGS.read_text())
    teams, people, areas, alias_cache = [], {}, {}, {}
    for key, kind in KINDS.items():
        for g in data.get(key) or []:
            leads = []
            for role in ("chairs", "tech_leads", "emeritus_leads"):
                for p in (g.get("leadership") or {}).get(role) or []:
                    login = clean_handle(p.get("github"))
                    if not login:
                        continue
                    leads.append({"login": login, "role": role.rstrip("s")})
                    if p.get("company"):
                        people.setdefault(login, {"login": login, "company": str(p["company"]).strip()})
            subprojects = []
            for sp in g.get("subprojects") or []:
                area_ids = []
                for url in sp.get("owners") or []:
                    area = parse_owners(url, alias_cache)
                    if area:
                        areas.setdefault(area["id"], area)
                        area_ids.append(area["id"])
                subprojects.append({"name": sp["name"], "description": (sp.get("description") or "").strip(),
                                    "code_areas": area_ids})
            teams.append({
                "id": g["dir"], "kind": kind, "name": g["name"],
                "mission": " ".join(str(g.get("mission_statement") or "").split()),
                "label": g.get("label"),
                "stakeholder_sigs": [s.lower() for s in g.get("stakeholder_sigs") or []],
                "leads": leads, "subprojects": subprojects,
                "url": f"https://github.com/kubernetes/community/tree/master/{g['dir']}",
            })
    for name, rows in (("teams", teams), ("people", list(people.values())), ("code_areas", list(areas.values()))):
        with (SEED / f"{name}.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    n_sp = sum(len(t["subprojects"]) for t in teams)
    print(f"{len(teams)} teams, {n_sp} subprojects, {len(areas)} code areas, {len(people)} leaders with company")


if __name__ == "__main__":
    main()
