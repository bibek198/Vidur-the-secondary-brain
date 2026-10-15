"""Freeze the raw data: write data/MANIFEST.json with counts, sizes and checksums.

Every number we report is computed from this exact snapshot. The manifest records:
- the git HEAD of each cloned repo,
- per raw folder (api/issues, api/comments, ..., owners): file count, bytes and one combined
  SHA-256 over all files (sorted paths + per-file hashes), so any change is detectable,
- the live-feed cutoff: changes after it are held back from the frozen graph and replayed
  through the real-time engine for the demo and the replay eval.

Usage: python -m src.fetch.manifest [--cutoff 2026-09-15]
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "MANIFEST.json"
DEFAULT_CUTOFF = "2026-09-15T00:00:00Z"


def folder_digest(folder: Path) -> dict:
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    combined = hashlib.sha256()
    total = 0
    for p in files:
        data = p.read_bytes()
        total += len(data)
        combined.update(str(p.relative_to(folder)).encode())
        combined.update(hashlib.sha256(data).digest())
    return {"files": len(files), "bytes": total, "sha256": combined.hexdigest()}


def repo_head(name: str) -> dict:
    path = RAW / "repos" / name
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True)
    date = subprocess.run(["git", "log", "-1", "--format=%cI"], cwd=path, capture_output=True, text=True, check=True)
    return {"head": head.stdout.strip(), "head_date": date.stdout.strip()}


def main(cutoff: str) -> None:
    manifest = {
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "live_feed_cutoff": cutoff,
        "repos": {name: repo_head(name) for name in ("enhancements", "community")},
        "folders": {},
    }
    for sub in ("api/issues", "api/comments", "api/reviews", "api/review_comments", "owners"):
        manifest["folders"][sub] = folder_digest(RAW / sub)
    OUT.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args[args.index("--cutoff") + 1] + "T00:00:00Z" if "--cutoff" in args else DEFAULT_CUTOFF)
