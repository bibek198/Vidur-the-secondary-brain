#!/usr/bin/env bash
# Reset the demo graph to the frozen evaluation state (2026-09-15) in about 20 seconds:
# delete brain_demo, copy brain_eval into it, and clear its event stream and queues.
# Stop the demo worker first; start it again afterwards:
#   .venv/bin/python -m src.realtime.worker --graph brain_demo --no-poll --no-gitdiff
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python - <<'EOF'
import time
from src.brain.db import client
from src.realtime import stream
c = client()
if "brain_demo" in c.list_graphs():
    c.connection.execute_command("GRAPH.DELETE", "brain_demo")
stream.reset("brain_demo")
t = time.time()
try:
    c.connection.execute_command("GRAPH.COPY", "brain_eval", "brain_demo")
    print(f"brain_demo reset from brain_eval in {time.time() - t:.1f}s")
except Exception as exc:  # GRAPH.COPY forks the server and can fail for lack of memory: load the seed instead
    import subprocess, sys
    print(f"GRAPH.COPY failed ({exc}); loading brain_demo from data/seed instead (1-3 min)")
    subprocess.run([sys.executable, "-m", "src.load.loader", "--graph", "brain_demo"], check=True, stdout=subprocess.DEVNULL)
    print(f"brain_demo loaded in {time.time() - t:.1f}s")
EOF
