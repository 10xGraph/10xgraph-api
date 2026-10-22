# CLI Commands

Use this when changing `10xgraph-api` command behavior, generated files, or deployment scaffolding.

## Package

Install:

```bash
pip install 10xgraph-api
```

Entry point:

```text
10xgraph = tenxgraph_api.cli.main:main
agentflow = tenxgraph_api.cli.main:legacy_main   # deprecated alias, removed in 2.0
```

## Commands

`10xgraph init`

- Scaffolds `10xgraph.json`, `graph/agent.py` and `.env.example`, and suggests `10xgraph skills` as a next step.
- Asks for the template (quick-start or production), the AI provider (Google, OpenAI, Anthropic) and whether to enable WebSocket streaming / realtime audio; production also asks for auth and rate limiting.
- Non-interactive options: `--path/-p`, `--force/-f`, `--name`, `--template` (quick-start|production), `--provider` (google|openai|anthropic), `--websocket/--no-websocket` (default off), `--auth` (none|jwt|custom), `--rate-limit` (none|memory|redis), `--yes/-y`, `--non-interactive`, `--dry-run`.
- Production adds `pyproject.toml` (dependencies follow the answers, e.g. `10xgraph[openai]`, `10xgraph-api[jwt,redis,websocket]`), `.pre-commit-config.yaml`, `tests/` and `evals/`.

`10xgraph dev`

- Starts the local development server (Uvicorn, reload on) for a compiled graph and opens the hosted playground when it is ready.
- Options include `--config/-c`, `--host/-H`, `--port/-p`, `--reload/--no-reload`, `--open/--no-open`, `--verbose/-v`, and `--quiet/-q`.
- Defaults are config `10xgraph.json`, host `127.0.0.1`, port `8000`, reload on, playground opened. `--no-open` serves only.
- There is no `10xgraph api` or `10xgraph play`; both were replaced by `dev`. Production runs Gunicorn in the image from `10xgraph build`.

`10xgraph build`

- Generates Docker deployment files.
- Options include `--output/-o`, `--force/-f`, `--python-version`, `--port/-p`, `--docker-compose/--no-docker-compose`, `--service-name`, `--verbose/-v`, and `--quiet/-q`.

`10xgraph skills`

- Installs the bundled 10xGraph skill (an Agent Skills spec folder: `SKILL.md` + `references/`) into an agent-specific project directory. Every agent gets the same folder; paths inside it are relative to the skill directory.
- Prompts for the target agent when `--agent` is omitted:
  - `1` / `codex`: `.agents/skills/10xgraph`
  - `2` / `claude`: `.claude/skills/10xgraph`
  - `3` / `github`: `.github/instructions/10xgraph.instructions.md` and `.github/skills/10xgraph`
- Options include `--agent/-a`, `--path/-p`, `--force/-f`, `--all`, `--list/-l`, `--verbose/-v`, and `--quiet/-q`.
- `--validate PATH` (repeatable) checks a skill directory, or a folder of skill directories, against the Agent Skills specification (https://agentskills.io/specification) and exits non-zero on errors. Nothing is installed.
- Source templates: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/cli/templates/skills

`10xgraph version`

- Prints CLI and library version information.

## Rules

- Use docs package names in user-facing text.
- Keep command options aligned with https://10xgraph.com/
- If command defaults change, update docs, templates, and tests together.
- Generated skill templates live under https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/cli/templates/skills

## Source Map

- CLI main: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/cli/main.py
- Commands: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/cli/commands
- CLI config/output/validation: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/cli/core
- Templates: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/cli/templates
- Docs: https://10xgraph.com/
