# Decision Brain

A Company Brain agent on FalkorDB: it connects the people, teams, design documents, tickets, chats and decisions of the Kubernetes project in one graph. It answers who owns or knows what, why something was decided, what a change affects, and what changed, and every answer shows the graph path and its sources. It keeps up as the project changes, in real time.

> Graph Hacks 2026 (WeMakeDevs × FalkorDB). Track 3: Company Brain. Work in progress; see [docs/project/STATUS.md](docs/project/STATUS.md).

## Overview
## Quick start

### Ask the agent
```bash
.venv/bin/python -m src.agent.cli "Why was classic DRA (KEP-3063) withdrawn?" --trace
DEMO_REPLAY=1 .venv/bin/python -m src.agent.cli "Who owns the kubelet?"     # recorded answer, no API key needed
```

### Use the graph tools from Claude Desktop or any MCP client
The same 13 tools the agent uses (9 read tools, 4 actions) are served over MCP:
```bash
npx @modelcontextprotocol/inspector .venv/bin/python -m src.mcp_server    # browse and call the tools
```
For Claude Desktop, add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), with your own path to the repo:
```json
{
  "mcpServers": {
    "decision-brain": {
      "command": "/ABSOLUTE/PATH/decision-brain-graph-hack/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/decision-brain-graph-hack/src/mcp_server.py"]
    }
  }
}
```
Use the absolute path to the script: Claude Desktop starts servers in another folder and ignores `cwd`. FalkorDB must be running (`docker compose up -d`) with the graph loaded.

## Architecture
## Graph data model
## Cypher queries and graph algorithms
## Real-time updates
## Data
## How the LLM is used and checked
## Evaluation
## Limitations and future work
## Track
Track 3: Company Brain.
## License
MIT. Data: Kubernetes enhancements and community repositories (Apache-2.0).
