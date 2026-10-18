"""Bootstrap a fresh deployment (idempotent: every step is skipped when already done).

1. Full clones of kubernetes/enhancements and kubernetes/community into data/raw/repos (for the live git diff
   and for reading design docs when a rationale is re-extracted).
2. brain_eval: the frozen graph, loaded from data/seed (committed) as of the manifest cutoff (2026-09-15).
3. brain_demo and brain_live: copies of brain_eval (GRAPH.COPY). The live worker then catches brain_live up
   with git diff and the GitHub poller; the sandbox webhook drives brain_demo.

Run inside the app image: docker compose -f docker-compose.prod.yml run --rm app python scripts/bootstrap.py
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import redis  # noqa: E402

from src.brain.db import client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPOS = {"enhancements": "https://github.com/kubernetes/enhancements.git",
         "community": "https://github.com/kubernetes/community.git"}


def step(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    repos = ROOT / "data" / "raw" / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    for name, url in REPOS.items():
        if (repos / name / ".git").exists():
            step(f"repo {name}: present, fetching")
            subprocess.run(["git", "-C", str(repos / name), "fetch", "--quiet", "origin"], check=True)
        else:
            step(f"repo {name}: cloning (full history, a few minutes)")
            subprocess.run(["git", "clone", "--quiet", url, str(repos / name)], check=True)
    c = client()
    graphs = c.list_graphs()
    if "brain_eval" not in graphs:
        step("brain_eval: loading from data/seed (about 3-5 minutes)")
        subprocess.run([sys.executable, "-m", "src.load.loader", "--graph", "brain_eval"], check=True, cwd=ROOT)
    else:
        step("brain_eval: present")
    for name in ("brain_demo", "brain_live"):
        if name in c.list_graphs():
            step(f"{name}: present")
            continue
        step(f"{name}: copying brain_eval")
        try:
            c.connection.execute_command("GRAPH.COPY", "brain_eval", name)
        except redis.ResponseError as exc:
            # GRAPH.COPY forks the server; without vm.overcommit_memory=1 the fork can fail. Loading the same
            # seed with the same cutoff gives an identical graph (about 40 s).
            step(f"{name}: copy failed ({exc}); loading from data/seed instead")
            subprocess.run([sys.executable, "-m", "src.load.loader", "--graph", name], check=True, cwd=ROOT,
                           stdout=subprocess.DEVNULL)
    step(f"done: {c.list_graphs()}")


if __name__ == "__main__":
    main()
