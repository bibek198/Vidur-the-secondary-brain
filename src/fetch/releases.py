"""Kubernetes minor-release dates (v1.N.0) -> data/seed/releases.jsonl.

Used to answer "as of v1.29" questions: a release name resolves to its publish date.
Usage: python -m src.fetch.releases
"""
import json
import re
from pathlib import Path

from src.fetch.github_api import GitHub
from src.realtime.events import iso_to_ts

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "seed" / "releases.jsonl"
_MINOR = re.compile(r"^v1\.(\d+)\.0$")


def main() -> None:
    gh = GitHub()
    rows = {}
    for _, page in gh.pages("/repos/kubernetes/kubernetes/releases"):
        for r in page:
            m = _MINOR.match(r.get("tag_name") or "")
            if m and r.get("published_at") and not r.get("prerelease"):
                version = f"v1.{m.group(1)}"
                rows[version] = {"version": version, "date": r["published_at"], "ts": iso_to_ts(r["published_at"]),
                                 "url": r["html_url"]}
    with OUT.open("w") as fh:
        for r in sorted(rows.values(), key=lambda r: r["ts"]):
            fh.write(json.dumps(r) + "\n")
    print(f"{len(rows)} releases -> {OUT.relative_to(ROOT)}: {min(rows)} .. "
          f"{max(rows, key=lambda v: int(v.split('.')[1]))}")


if __name__ == "__main__":
    main()
