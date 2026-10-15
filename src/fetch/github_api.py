"""GitHub REST fetcher for the scoped SIGs.

Every response page is saved under data/raw/api/<key>/, so a crashed run resumes
where it stopped. The same GitHub client (ETag + rate-limit handling) is reused
by the real-time poller.

Usage:
    python -m src.fetch.github_api issues      # issues + PRs labelled with each scoped SIG
    python -m src.fetch.github_api comments    # conversation comments for every item with comments
    python -m src.fetch.github_api reviews     # reviews + inline review comments for PRs
    python -m src.fetch.github_api all
"""
import json
import sys
import time
from pathlib import Path

import httpx

from src.brain import config

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "data" / "raw" / "api"
REPO = "kubernetes/enhancements"
SCOPED_SIGS = ["node", "api-machinery", "storage", "scheduling", "auth", "cli"]


class GitHub:
    """Small GitHub REST client: auth, pagination, rate-limit sleep, ETag support."""

    def __init__(self, token: str | None = config.GITHUB_TOKEN):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30)

    def get(self, url: str, params: dict | None = None, etag: str | None = None) -> httpx.Response:
        headers = {"If-None-Match": etag} if etag else {}
        for attempt in range(5):
            r = self.http.get(url, params=params, headers=headers)
            if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
                wait = int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - time.time() + 2
                print(f"  rate limited, sleeping {wait:.0f}s", flush=True)
                time.sleep(max(wait, 1))
                continue
            if r.status_code >= 500 or r.status_code in (403, 429):
                time.sleep(2 ** attempt)
                continue
            return r
        r.raise_for_status()
        return r

    def pages(self, url: str, params: dict | None = None):
        """Yield (page_number, items) following Link rel="next"."""
        page, next_url, next_params = 1, url, dict(params or {}, per_page=100)
        while next_url:
            r = self.get(next_url, params=next_params)
            r.raise_for_status()
            yield page, r.json()
            next_url = r.links.get("next", {}).get("url")
            next_params, page = None, page + 1


def fetch_all_pages(gh: GitHub, key: str, url: str, params: dict | None = None) -> list:
    """Fetch every page into API_DIR/key/, skipping the work if a complete marker exists."""
    out = API_DIR / key
    done = out / "_complete"
    if done.exists():
        return load_pages(key)
    out.mkdir(parents=True, exist_ok=True)
    for page, items in gh.pages(url, params):
        (out / f"page_{page:04d}.json").write_text(json.dumps(items))
    done.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return load_pages(key)


def load_pages(key: str) -> list:
    items = []
    for f in sorted((API_DIR / key).glob("page_*.json")):
        items.extend(json.loads(f.read_text()))
    return items


def fetch_issues(gh: GitHub) -> list:
    """Issues and PRs labelled sig/<name>. KEP tracking issues share the KEP number."""
    items = {}
    for sig in SCOPED_SIGS:
        rows = fetch_all_pages(gh, f"issues/sig-{sig}", f"/repos/{REPO}/issues",
                               {"state": "all", "labels": f"sig/{sig}"})
        print(f"sig/{sig}: {len(rows)} issues+PRs", flush=True)
        for it in rows:
            items[it["number"]] = it
    return list(items.values())


def fetch_comments(gh: GitHub, items: list) -> None:
    todo = [it for it in items if it.get("comments", 0) > 0]
    print(f"comments: {len(todo)} threads", flush=True)
    for i, it in enumerate(todo, 1):
        fetch_all_pages(gh, f"comments/{it['number']}", f"/repos/{REPO}/issues/{it['number']}/comments")
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}", flush=True)


def fetch_reviews(gh: GitHub, items: list) -> None:
    prs = [it for it in items if "pull_request" in it]
    print(f"reviews: {len(prs)} PRs", flush=True)
    for i, it in enumerate(prs, 1):
        n = it["number"]
        fetch_all_pages(gh, f"reviews/{n}", f"/repos/{REPO}/pulls/{n}/reviews")
        fetch_all_pages(gh, f"review_comments/{n}", f"/repos/{REPO}/pulls/{n}/comments")
        if i % 50 == 0:
            print(f"  {i}/{len(prs)}", flush=True)


def main(steps: list[str]) -> None:
    if not config.GITHUB_TOKEN:
        sys.exit("GITHUB_TOKEN is not set. Copy .env.example to .env and add a read-only token.")
    gh = GitHub()
    items = fetch_issues(gh)
    if "comments" in steps or "all" in steps:
        fetch_comments(gh, items)
    if "reviews" in steps or "all" in steps:
        fetch_reviews(gh, items)
    r = gh.get("/rate_limit").json()["rate"]
    print(f"done. rate limit remaining {r['remaining']}/{r['limit']}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["all"])
