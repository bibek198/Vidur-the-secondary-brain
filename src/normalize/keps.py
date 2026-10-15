"""kep.yaml files -> data/seed/keps.jsonl (one clean record per KEP).

Usage: python -m src.normalize.keps
"""
import difflib
import json
import re
from pathlib import Path

from src.normalize.common import clean_status, handles, kep_id, kep_id_from_ref, load_yaml

ROOT = Path(__file__).resolve().parents[2]
ENH = ROOT / "data" / "raw" / "repos" / "enhancements"
SEED = ROOT / "data" / "seed"
GH = "https://github.com/kubernetes/enhancements"


def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).split("#")[0].strip().strip('"')
    return s or None


def _list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _sig(v) -> str | None:
    s = _str(v)
    return s.lower() if s else None


_NOT_A_REF = {"n/a", "na", "none", "nil", "-", ""}
_DATE_STYLE = re.compile(r"(?:^|/)(?:\d{8})-([a-z0-9-]+?)(?:\.md)?/?$")


def _ref_values(v) -> list[str]:
    out = []
    for x in _list(v):
        s = str(x).strip().strip("()").strip()
        if s.lower() not in _NOT_A_REF:
            out.append(s)
    return out


def _doc_url(s: str) -> str | None:
    """Non-KEP references (design-proposals archive, community PRs, Google Docs) as URLs."""
    if s.startswith("http"):
        return s
    if "design-proposals" in s:
        tail = s.split("design-proposals", 1)[1].lstrip("/").removeprefix("blob/main/").removeprefix("blob/master/")
        return f"https://github.com/kubernetes/design-proposals-archive/blob/main/{tail}"
    return None


def parse_kep(data: dict, rel_path: str) -> dict | None:
    """One kep.yaml dict -> normalized record. rel_path is like keps/sig-node/753-sidecar-containers."""
    if not data or not data.get("kep-number"):
        return None
    try:
        number = int(str(data["kep-number"]).strip())
    except ValueError:
        return None
    milestones = {k: _str(v) for k, v in (data.get("milestone") or {}).items() if _str(v)}
    gates = []
    for g in _list(data.get("feature-gates")):
        if isinstance(g, dict) and g.get("name"):
            gates.append({"name": str(g["name"]).strip(),
                          "components": [str(c).strip() for c in _list(g.get("components")) if c]})
    me = kep_id(number)
    def refs(key):
        """KEP references stay as ids (date-style ones resolved later); others become doc URLs."""
        ids, docs = [], []
        for s in _ref_values(data.get(key)):
            m = _DATE_STYLE.search(s.lower())
            rid = f"slug:{m.group(1)}" if m else kep_id_from_ref(s)
            if rid and rid != me and rid not in ids:
                ids.append(rid)
            elif not rid and _doc_url(s):
                docs.append(_doc_url(s))
        return ids, docs

    see_also, see_also_docs = refs("see-also")
    replaces, replaces_docs = refs("replaces")
    superseded_by, _ = refs("superseded-by")
    return {
        "id": me,
        "number": number,
        "title": _str(data.get("title")),
        "owning_sig": _sig(data.get("owning-sig")),
        "participating_sigs": [s for s in (_sig(x) for x in _list(data.get("participating-sigs"))) if s],
        "status": clean_status(data.get("status")),
        "status_raw": _str(data.get("status")),
        "stage": _str(data.get("stage")),
        "latest_milestone": _str(data.get("latest-milestone")),
        "milestones": milestones,
        "authors": handles(data.get("authors")),
        "reviewers": handles(data.get("reviewers")),
        "approvers": handles(data.get("approvers")),
        "editor": (handles([data.get("editor")]) or [None])[0],
        "creation_date": _str(data.get("creation-date")),
        "last_updated": _str(data.get("last-updated")),
        "see_also": see_also,
        "see_also_docs": see_also_docs,
        "replaces": replaces,
        "replaces_docs": replaces_docs,
        "superseded_by": superseded_by,
        "feature_gates": gates,
        "path": rel_path,
        "url": f"{GH}/tree/master/{rel_path}",
        "readme_url": f"{GH}/blob/master/{rel_path}/README.md",
        "issue_url": f"{GH}/issues/{number}",
    }


def resolve_refs(records: dict) -> int:
    """Map 'slug:<name>' refs (pre-numbering KEP files) to current KEP ids by name similarity.
    Unresolvable or unknown refs are dropped. Returns the number of dropped refs."""
    slugs = {r["path"].rsplit("/", 1)[-1].split("-", 1)[-1]: r["id"] for r in records.values()}
    dropped = 0
    for r in records.values():
        for key in ("see_also", "replaces", "superseded_by"):
            out = []
            for ref in r[key]:
                if ref.startswith("slug:"):
                    m = difflib.get_close_matches(ref[5:], slugs, n=1, cutoff=0.85)
                    ref = slugs[m[0]] if m else None
                if ref and ref in records and ref != r["id"] and ref not in out:
                    out.append(ref)
                else:
                    dropped += 1
            r[key] = out
    return dropped


def main() -> None:
    SEED.mkdir(parents=True, exist_ok=True)
    records, skipped = {}, []
    for f in sorted((ENH / "keps").glob("*/*/kep.yaml")):
        rel = str(f.parent.relative_to(ENH))
        try:
            rec = parse_kep(load_yaml(f.read_text()), rel)
        except Exception as e:  # noqa: BLE001 - log and keep going
            skipped.append((rel, str(e)[:80]))
            continue
        if rec is None:
            skipped.append((rel, "no kep-number"))
            continue
        if rec["id"] in records:
            skipped.append((rel, f"duplicate of {records[rec['id']]['path']}"))
            continue
        records[rec["id"]] = rec
    dropped = resolve_refs(records)
    out = SEED / "keps.jsonl"
    with out.open("w") as fh:
        for rec in sorted(records.values(), key=lambda r: r["number"]):
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    print(f"{len(records)} KEPs -> {out.relative_to(ROOT)}; skipped {len(skipped)}; dropped {dropped} unresolvable refs")
    for s in skipped:
        print("  skipped", *s)


if __name__ == "__main__":
    main()
