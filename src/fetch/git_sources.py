"""Clone or update the git sources (no API rate limit).

- kubernetes/enhancements: every keps/**/kep.yaml plus its full history (status changes over time).
- kubernetes/community: sigs.yaml plus its history (leadership changes over time).

Blobless clones (--filter=blob:none) keep the download small; git fetches file
contents lazily, only for the paths we read.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOS_DIR = ROOT / "data" / "raw" / "repos"
REPOS = {
    "enhancements": "https://github.com/kubernetes/enhancements.git",
    "community": "https://github.com/kubernetes/community.git",
}


def run(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sync(name: str) -> str:
    """Clone if missing, otherwise fetch and fast-forward. Returns the HEAD sha."""
    path = REPOS_DIR / name
    if not path.exists():
        REPOS_DIR.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--filter=blob:none", "--quiet", REPOS[name], str(path)])
    else:
        run(["git", "fetch", "--quiet", "origin"], cwd=path)
        run(["git", "reset", "--quiet", "--hard", "origin/HEAD"], cwd=path)
    return run(["git", "rev-parse", "HEAD"], cwd=path)


if __name__ == "__main__":
    for name in sys.argv[1:] or REPOS:
        print(name, sync(name))
