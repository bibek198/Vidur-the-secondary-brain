"""GitHub API pages -> data/seed/threads/*.jsonl.gz (tickets, PRs, comments, reviews).

- tickets: issues. A KEP's tracking issue has the same number as the KEP.
- prs: pull requests, linked to the KEPs they change (from the git history and from
  inline review comment file paths).
- comments: issue comments, review bodies and inline review comments, bodies cut to 2,000 chars.
- reviews: one row per formal review (author, state, time), for REVIEWED edges.

Bots are dropped. Output is gzip-compressed JSONL so it can be committed (judges need no token).
Usage: python -m src.normalize.threads
"""
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path

from src.normalize.common import clean_handle, kep_id
from src.realtime.events import iso_to_ts

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "data" / "raw" / "api"
SEED = ROOT / "data" / "seed"
OUT = SEED / "threads"
REPO = "enhancements"
BODY_MAX, ITEM_BODY_MAX = 2000, 4000
_KEP_MENTION = re.compile(r"\bKEP[- ]?(\d{2,5})\b", re.I)
_KEP_PATH = re.compile(r"keps/[\w-]+/(\d+)-")
_PROCESS = re.compile(r"^(lgtm|approved|cncf-cla|size/|kind/|needs-|do-not-merge|lifecycle/|tracked/|stage/|"
                      r"triage/|priority/|ok-to-test|release-note|area/enhancements|api-review)")


def _pages(sub: str):
    for f in sorted((API / sub).glob("*/page_*.json")):
        yield f.parent.name, json.loads(f.read_text())


def _author(obj) -> str | None:
    user = obj.get("user") or {}
    if user.get("type") == "Bot":
        return None
    return clean_handle(user.get("login"))


def _body(text, limit=BODY_MAX) -> str:
    return (text or "").strip()[:limit]


def _ts(value) -> int | None:
    return iso_to_ts(value) if value else None


def _mentions(text: str, known: set[str]) -> list[str]:
    found = {kep_id(m) for m in _KEP_MENTION.findall(text or "")}
    return sorted(found & known)


def main() -> None:
    keps = {json.loads(line)["id"] for line in (SEED / "keps.jsonl").open()}
    # PR -> KEPs it changed, from the git history (exact) ...
    pr_keps: dict[int, set[str]] = defaultdict(set)
    for line in (SEED / "history_events.jsonl").open():
        e = json.loads(line)
        if e["entity_key"].startswith("KEP-") and e["payload"].get("pr") and "enhancements" in e["payload"].get("url", ""):
            pr_keps[e["payload"]["pr"]].add(e["entity_key"])

    items = {}
    for _, page in _pages("issues"):
        for it in page:
            items[it["number"]] = it

    comments, reviews = [], []
    for number, page in _pages("comments"):
        for c in page:
            login = _author(c)
            if login:
                comments.append({"id": f"comment:{c['id']}", "kind": "issue", "thread": f"{REPO}#{number}",
                                 "author": login, "at": c["created_at"], "at_ts": _ts(c["created_at"]),
                                 "url": c["html_url"], "body": _body(c["body"]),
                                 "mentions": _mentions(c["body"], keps)})
    for number, page in _pages("reviews"):
        for r in page:
            login = _author(r)
            if not login or not r.get("submitted_at"):
                continue
            reviews.append({"id": f"review:{r['id']}", "pr": f"{REPO}#{number}", "author": login,
                            "state": r["state"], "at": r["submitted_at"], "at_ts": _ts(r["submitted_at"]),
                            "url": r["html_url"]})
            if (r.get("body") or "").strip():
                comments.append({"id": f"review:{r['id']}", "kind": "review", "thread": f"{REPO}#{number}",
                                 "author": login, "at": r["submitted_at"], "at_ts": _ts(r["submitted_at"]),
                                 "url": r["html_url"], "body": _body(r["body"]),
                                 "mentions": _mentions(r["body"], keps)})
    for number, page in _pages("review_comments"):
        for c in page:
            login = _author(c)
            m = _KEP_PATH.search(c.get("path") or "")
            if m and kep_id(m.group(1)) in keps:
                pr_keps[int(number)].add(kep_id(m.group(1)))  # ... and from inline comment file paths
            if login:
                comments.append({"id": f"rcomment:{c['id']}", "kind": "inline", "thread": f"{REPO}#{number}",
                                 "author": login, "at": c["created_at"], "at_ts": _ts(c["created_at"]),
                                 "url": c["html_url"], "body": _body(c["body"]), "path": c.get("path"),
                                 "mentions": _mentions(c["body"], keps)})

    tickets, prs = [], []
    for n, it in sorted(items.items()):
        labels = [lab["name"] for lab in it.get("labels") or []]
        row = {"id": f"{REPO}#{n}", "number": n, "title": it["title"], "state": it["state"],
               "author": _author(it), "created_at": it["created_at"], "created_ts": _ts(it["created_at"]),
               "closed_at": it.get("closed_at"), "closed_ts": _ts(it.get("closed_at")), "url": it["html_url"],
               "sigs": sorted(lab.split("/", 1)[1] for lab in labels if lab.startswith("sig/")),
               "labels": [lab for lab in labels if not lab.startswith("sig/") and not _PROCESS.match(lab)],
               "process_labels": [lab for lab in labels if _PROCESS.match(lab)],
               "body": _body(it.get("body"), ITEM_BODY_MAX), "comment_count": it.get("comments", 0)}
        if "pull_request" in it:
            merged = (it["pull_request"] or {}).get("merged_at")
            linked = pr_keps.get(n, set()) | set(_mentions(it["title"], keps))
            prs.append({**row, "merged": bool(merged), "merged_at": merged, "keps": sorted(linked)})
        else:
            tickets.append({**row, "state_reason": it.get("state_reason"),
                            "kep": kep_id(n) if kep_id(n) in keps else None})

    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in (("tickets", tickets), ("prs", prs), ("comments", comments), ("reviews", reviews)):
        with gzip.open(OUT / f"{name}.jsonl.gz", "wt") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    linked_t = sum(1 for t in tickets if t["kep"])
    linked_p = sum(1 for p in prs if p["keps"])
    print(f"{len(tickets)} tickets ({linked_t} KEP tracking issues), {len(prs)} PRs ({linked_p} linked to KEPs), "
          f"{len(comments)} comments, {len(reviews)} reviews -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
