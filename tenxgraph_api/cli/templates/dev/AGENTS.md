<!-- 10xgraph:start -->
## 10xGraph

This project is built on 10xGraph (`10xgraph`, `10xgraph-api`).

- Before you write or change agent, graph, tool or `10xgraph.json` code, read
  https://10xgraph.com/skill.md (the agent quickstart).
- Docs index: https://10xgraph.com/llms.txt. Every doc page has a markdown copy at
  `https://10xgraph.com/docs/<page>.md`.
- For deeper guidance, run `10xgraph skills --agent codex` (or `claude`, `github`). It replaces
  this block with the full skill instructions.

Key files:

- `10xgraph.json`: server config. `agent` points to the compiled graph.
- `graph/agent.py`: the agent and its graph.
- `.env.example`: environment variables. Copy it to `.env`.
- `requirements.txt`: dependencies.

Run it with `10xgraph dev`.
<!-- 10xgraph:end -->
