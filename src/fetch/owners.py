"""Download the OWNERS files that sigs.yaml subprojects point to (raw.githubusercontent.com, no API quota),
plus each repo's OWNERS_ALIASES so alias names can be expanded to people.

Saved as data/raw/owners/<org>/<repo>/<path>/OWNERS and data/raw/owners/<org>/<repo>/OWNERS_ALIASES.
Usage: python -m src.fetch.owners
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from src.normalize.common import load_yaml

ROOT = Path(__file__).resolve().parents[2]
SIGS = ROOT / "data" / "raw" / "repos" / "community" / "sigs.yaml"
OUT = ROOT / "data" / "raw" / "owners"


def owners_urls() -> list[str]:
    data = load_yaml(SIGS.read_text())
    urls = []
    for kind in ("sigs", "workinggroups", "committees"):
        for group in data.get(kind) or []:
            for sp in group.get("subprojects") or []:
                urls.extend(u for u in sp.get("owners") or [] if u.startswith("https://raw.githubusercontent.com/"))
    return sorted(set(urls))


def local_path(url: str) -> Path:
    # https://raw.githubusercontent.com/<org>/<repo>/<branch>/<path...>/OWNERS
    org, repo, _branch, *rest = url.split("/")[3:]
    return OUT / org / repo / "/".join(rest)


def alias_url(url: str) -> str:
    org, repo, branch = url.split("/")[3:6]
    return f"https://raw.githubusercontent.com/{org}/{repo}/{branch}/OWNERS_ALIASES"


def download(client: httpx.Client, url: str, dest: Path) -> str:
    if dest.exists():
        return "cached"
    r = client.get(url)
    if r.status_code != 200:
        return f"http {r.status_code}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(r.text)
    return "ok"


def main() -> None:
    urls = owners_urls()
    jobs = [(u, local_path(u)) for u in urls]
    jobs += [(a, OUT / a.split("/")[3] / a.split("/")[4] / "OWNERS_ALIASES") for a in sorted({alias_url(u) for u in urls})]
    with httpx.Client(timeout=20, follow_redirects=True) as client, ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda j: (j[0], download(client, *j)), jobs))
    bad = [(u, s) for u, s in results if s not in ("ok", "cached")]
    print(f"{len(jobs)} files ({len(urls)} OWNERS + aliases); failed {len(bad)}")
    for u, s in bad[:15]:
        print("  ", s, u)


if __name__ == "__main__":
    main()
