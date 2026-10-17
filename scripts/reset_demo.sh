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
c.connection.execute_command("GRAPH.COPY", "brain_eval", "brain_demo")
print(f"brain_demo reset from brain_eval in {time.time() - t:.1f}s")
EOF
