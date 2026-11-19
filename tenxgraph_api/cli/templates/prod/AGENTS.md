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
- `graph/agent.py`: the agent and its graph. Tools live in `graph/tools/`.
- `.env.example`: environment variables. Copy it to `.env`.
- `pyproject.toml`: dependencies.
##[IF_TESTS]##
- `tests/`: unit tests, run with `10xgraph test`.
##[/IF_TESTS]##
##[IF_EVALS]##
- `evals/`: agent evaluations, run with `10xgraph eval`.
##[/IF_EVALS]##

Run it with `10xgraph dev`. Change settings with `10xgraph config`.
<!-- 10xgraph:end -->
