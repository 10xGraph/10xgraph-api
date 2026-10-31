# 10xGraph API

*Formerly `10xscale-agentflow-cli`.* The production server for 10xGraph agents.

[![CI](https://github.com/10xGraph/10xgraph-api/actions/workflows/ci.yaml/badge.svg)](https://github.com/10xGraph/10xgraph-api/actions/workflows/ci.yaml)
[![Release](https://img.shields.io/github/v/release/10xGraph/10xgraph-api)](https://github.com/10xGraph/10xgraph-api/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/10xgraph-api?color=blue)](https://pypi.org/project/10xgraph-api/)
[![Python](https://img.shields.io/pypi/pyversions/10xgraph-api)](https://pypi.org/project/10xgraph-api/)
[![License](https://img.shields.io/github/license/10xGraph/10xgraph-api)](https://github.com/10xGraph/10xgraph-api/blob/main/LICENSE)

`10xgraph-api` generates the production server around a [10xGraph](https://github.com/10xGraph/10xGraph) agent. Point `10xgraph.json` at your compiled graph and it serves REST, NDJSON streaming, WebSocket and realtime-audio endpoints with JWT or custom auth, scoped authorization on every endpoint, owner-only threads and rate limiting, then writes your Docker Compose and Kubernetes files. MIT licensed and self-hosted.

Other frameworks give you the graph. 10xGraph gives you the graph and the server around it, in the same open-source project.

---

## What the server gives you

- **Endpoints generated from the graph.** Invoke, NDJSON stream, stop, WebSocket and realtime audio; threads, state and messages; the memory store; file uploads for multimodal input; and an optional [AG-UI](https://10xgraph.com/docs/server/ag-ui) endpoint for CopilotKit and other AG-UI clients.
- **Authentication.** `"auth": "jwt"`, or your own `BaseAuth` subclass.
- **Scoped authorization on every endpoint.** Routes require `resource:action` scopes such as `graph:invoke`, `graph:stream` or `checkpointer:read`, checked by a pluggable `AuthorizationBackend`. The server refuses to start if any non-public route has no guard.
- **Owner-only threads.** In production mode a thread is readable and writable only by the user who created it, on every step that touches it. Ownership checks are cached in-process and optionally in Redis, so they do not hit the database on each request.
- **Rate limiting.** Sliding-window limits kept in the API's cache store: in memory, on Redis, or in your own `BaseCacheStore`.
- **Production hardening.** Request size limits (10 MB default), security headers, error and log sanitization, startup warnings for insecure settings, and Snowflake IDs for multi-node deployments.
- **Deployment files.** `10xgraph build --docker-compose --k8s` writes a `Dockerfile`, `.dockerignore`, `docker-compose.yml` and `k8s.yaml`.
- **A secure-by-default scaffold.** `10xgraph init --yes --template production --auth jwt --rate-limit redis` writes a project with JWT auth, owner-only thread access, a Redis cache store for rate limits and token revocation, a prompt-injection validator, evals, tests, `.env.example` and a pre-commit config.

Not built in: automatic per-tool permissions (user A may call `refund`, user B may not). Tools receive the caller's verified scopes and can check them with `tenxgraph.core.authz.has_scope`.

| Package | What it does | Install |
|---|---|---|
| [`10xgraph`](https://github.com/10xGraph/10xGraph) | Core engine: graph, state, replay-safe tools, checkpointing, memory | `pip install 10xgraph` (pulled in by this package) |
| `10xgraph-api` (this repository) | Production server and the `10xgraph` command | `pip install 10xgraph-api` |
| [`10xgraph-client`](https://github.com/10xGraph/10xgraph-client) | Typed TypeScript client for every endpoint | `npm install 10xgraph-client` |

Docs: [10xgraph.com](https://10xgraph.com).

---

## Migrating from 10xscale-agentflow-cli

10xGraph is the new name of 10xScale Agentflow. The server, CLI, license and maintainers are
the same, and existing projects keep working.

| | Before | Now |
|---|---|---|
| PyPI package | `10xscale-agentflow-cli` | `10xgraph-api` |
| Python import | `agentflow_cli` | `tenxgraph_api` |
| Command | `agentflow` | `10xgraph` |
| Config file | `agentflow.json` | `10xgraph.json` |
| Core framework | `10xscale-agentflow` (import `agentflow`) | `10xgraph` (import `tenxgraph`) |
| CLI env vars | `AGENTFLOW_NO_FULLSCREEN`, `AGENTFLOW_NO_SPINNER`, `AGENTFLOW_ASCII` | `TENXGRAPH_NO_FULLSCREEN`, `TENXGRAPH_NO_SPINNER`, `TENXGRAPH_ASCII` |
| Website | agentflow.10xscale.ai | [10xgraph.com](https://10xgraph.com) |
| GitHub | [github.com/10xHub](https://github.com/10xHub) | [github.com/10xGraph](https://github.com/10xGraph) |

```bash
pip uninstall 10xscale-agentflow-cli 10xscale-agentflow
pip install 10xgraph-api
```

The old core and the new one both ship an `agentflow` module, so do not install
`10xscale-agentflow` next to `10xgraph`.

Kept working until 2.0, so you can migrate at your own pace:

- **`agentflow` command.** Runs the same CLI and prints a one-line deprecation notice.
- **`from agentflow_cli import ...`.** Resolves to the same modules as `tenxgraph_api` and
  emits a `DeprecationWarning`.
- **`agentflow.json`.** Read when no `10xgraph.json` sits in the same directory. When both
  exist, `10xgraph.json` wins. Rename the file when convenient: the keys are unchanged.
- **`AGENTFLOW_*` CLI env vars.** Read when the `TENXGRAPH_*` variable is unset.
- **`agentflow-bearer` WebSocket subprotocol and `agentflow://media/` URLs.** Still accepted.
  New media references are written as `graph://media/`, and the server now also accepts
  `10xgraph-bearer`.

---

## Installation

**Basic installation:**

```bash
pip install 10xgraph-api
```

Optional extras; install only what you configure:

```bash
pip install "10xgraph-api[redis]"   # Redis cache store (locks, rate limits, revocation)
pip install "10xgraph-api[jwt]"     # JWT authentication
pip install "10xgraph-api[media]"   # Document text extraction (multimodal)
pip install "10xgraph-api[images]"  # Image resize / EXIF / format conversion (Pillow)
pip install "10xgraph-api[ag-ui]"   # AG-UI endpoint
pip install "10xgraph-api[websocket]"  # WebSocket streaming + realtime audio
pip install "10xgraph-api[otel]"    # OpenTelemetry tracing
pip install "10xgraph-api[snowflakekit]"  # Snowflake ID generation
pip install "10xgraph-api[sentry]"  # Sentry error reporting
pip install "10xgraph-api[firebase]"  # Firebase Admin SDK
pip install "10xgraph-api[gcloud]"  # Google Cloud Logging
```

Requires Python 3.12 or newer. Tested on 3.12, 3.13, 3.14 and 3.15. Installs the core `10xgraph` framework as a dependency.

---

## Quick start

```bash
# 1. Scaffold a project (interactive: dev vs production, auth, rate limiting)
10xgraph init

# 2. Start the local development server and open the playground (127.0.0.1:8000)
10xgraph dev

# 3. Check the environment if anything looks wrong
10xgraph audit

# 4. Generate production Docker files
10xgraph build --docker-compose
```

---

## CLI commands

Run `10xgraph --help` or `10xgraph COMMAND --help` for the generated command reference.

### `10xgraph init`

Initialize a new project with configuration and a sample graph.

```bash
10xgraph init                  # interactive (chooses dev vs production setup)
10xgraph init --path ./my-app  # custom directory
10xgraph init --force          # overwrite existing files
10xgraph init --path ./my-app --name MyAgent --template quick-start \
  --non-interactive            # reproducible CI/agent workflow
10xgraph init --path ./my-app --template production --auth jwt \
  --rate-limit redis --provider openai --yes --dry-run
```

`init` asks which AI provider to use (`--provider google|openai|anthropic`, default `google`)
and writes the matching model and API key variable. It also asks whether to enable WebSocket
streaming and realtime audio (`--websocket`, default off); off leaves `/v1/graph/ws` and
`/v1/graph/live` unmounted and installs no WebSocket library. A production project's `pyproject.toml`
lists only what your answers need, for example `10xgraph[openai]` and
`10xgraph-api[jwt,redis]`, and `10xgraph build` installs exactly that list, so no separate
`requirements.txt` is needed.

### `10xgraph dev`

Start the development API server and open the hosted playground when it is ready.

```bash
10xgraph dev                              # defaults (127.0.0.1:8000)
10xgraph dev --host 127.0.0.1 --port 9000 # custom host/port
10xgraph dev --config production.json     # custom config file
10xgraph dev --no-open --no-reload        # API only, without auto-reload
```

`dev` is the only server command: the old `api` and `play` commands were removed
(use `10xgraph dev --no-open` and `10xgraph dev`). It runs Uvicorn with reload for local
work; production runs Gunicorn in the image from `10xgraph build`.

### Adaptive and structured output

```bash
10xgraph dev                          # branded intro, then output in your scrollback
10xgraph demo                         # preview every animation theme safely
10xgraph demo --style build           # one theme: typing, network, init, build, or eval
10xgraph --fullscreen dev             # opt-in pinned header/footer surface
10xgraph --no-animation dev           # accessible/static workflow
10xgraph --format plain --no-color audit
10xgraph --format jsonl eval --parallel
10xgraph --quiet build
10xgraph --cwd ../my-agent dev
```

On an interactive terminal each command opens with a short intro (under a second) in
the logo's colors: the 10XGRAPH wordmark in ink, swept in by the amber entry node and the
blue accent, the command, the running versions (`10xgraph-api`, core `10xgraph`,
Python), and the command's own pipeline drawn like the logo's graph: `play`/`dev`
config→runtime→server→playground, `init` template→graph→config→project, `build`
source→deps→image→ship, `eval` discover→load→score→report, `audit`
python→core→config→port. Press any key to skip it; Ctrl+C quits at once. The intro then
leaves a one-line branded header in your normal scrollback, and the command's output
follows it, so there is nothing to dismiss when it ends.

`--fullscreen` (or `TENXGRAPH_FULLSCREEN=1`) runs the command on a dedicated surface
instead: a pinned header and footer with the output scrolling between them. That surface
waits for Enter after a normal finish, so a fast command cannot erase its own result;
Ctrl+C releases it immediately.

Long-running work reports through a live step timeline: stages are declared up
front, pending ones stay dimmed, and the running one animates with an elapsed
timer. `10xgraph eval` uses a determinate progress bar with a running pass/fail
tally.

Motion is disabled automatically for redirected output, CI, `TERM=dumb`,
JSON/JSONL, and `TENXGRAPH_NO_SPINNER=1`. Use `--no-animation` for a stable
screen-reader friendly experience, or `--animation` to force motion in a
compatible terminal. Every animated surface has a plain line-per-transition
renderer and a versioned JSON/JSONL event renderer.

### `10xgraph build`

Generate production Docker files.

```bash
10xgraph build                            # Dockerfile
10xgraph build --docker-compose           # Dockerfile + docker-compose.yml
10xgraph build --k8s                      # Dockerfile + k8s.yaml (Deployment + Service)
10xgraph build --python-version 3.12 --port 9000
10xgraph build --service-name my-agent    # name used in docker-compose.yml / k8s.yaml
10xgraph build --force                    # overwrite an existing Dockerfile
```

### `10xgraph eval` / `10xgraph test`

Run agent evaluations (discovers `*_eval.py` / `eval_*.py`, writes JSON and HTML reports to `eval_reports/` and opens the HTML report; `--no-open` skips that) and project tests (pytest).

```bash
10xgraph eval --parallel --threshold 0.8
10xgraph test --coverage
```

### `10xgraph skills`

Install bundled coding-agent skills (Codex, Claude, GitHub Copilot) into your project so your AI assistant knows how to build with 10xGraph.

```bash
10xgraph skills                # pick agents interactively (space toggles, enter confirms)
10xgraph skills --all          # install for every supported agent
10xgraph skills --agent claude # install for one
10xgraph skills --list         # show supported agents
10xgraph skills --force        # overwrite an existing install
10xgraph skills --validate ./.agents/skills  # check skills against the Agent Skills spec
```

Run without flags to get a checklist of the supported agents. Each row shows
where it installs, agents that are already set up are labelled and pre-checked,
and picking one that exists offers to overwrite rather than failing.

### `10xgraph version`

Display CLI and package version information.

```bash
10xgraph --version            # script-friendly CLI version only
10xgraph version
```

### `10xgraph audit`

Read-only check of everything that has to be true before `dev`, `eval`, or `build`
can work here: the Python interpreter, the installed `10xgraph-api` and
`10xgraph` packages, whether the installed core still exposes the
evaluation API this CLI imports, whether `10xgraph.json` is present and declares a
valid `agent` key, and whether the default port is free.

```bash
10xgraph audit                    # table of six checks
10xgraph --format json audit      # machine-readable, for CI
10xgraph --no-animation audit     # static output
```

Nothing is written or changed. It exits `1` if any check fails and `0` otherwise.
Warnings (no project config, port already bound) are reported without failing the run,
so it works as a CI gate.

### `10xgraph config`

Open a local browser editor for `10xgraph.json`: switch optional sections (auth,
authorization, rate limiting, observability, ...) on or off, fill in their fields,
**Validate** without saving, and **Save** to write the file. Saving is blocked while
there are errors, and the previous file is kept as `10xgraph.json.bak`.

```bash
10xgraph config                           # edit ./10xgraph.json
10xgraph config -c path/to/10xgraph.json --port 8765 --no-open
```

### `10xgraph demo`

Preview the terminal animations and progress states without touching project state.

```bash
10xgraph demo                  # every theme
10xgraph demo --style eval     # one of: typing, network, init, build, eval
```

---

## Configuration

The configuration file (`10xgraph.json`) defines your agent, authentication, and infrastructure settings:

```json
{
  "agent": "graph.react:app",
  "env": ".env",
  "auth": null,
  "checkpointer": null,
  "injectq": null,
  "authorization": null,
  "store": null,
  "remote_tools": [],
  "redis": null,
  "thread_name_generator": null,
  "rate_limit": {}
}
```

### Configuration Options

| Field | Type | Description |
|-------|------|-------------|
| `agent` | string | Path to your compiled agent graph, `"module:attribute"` (required) |
| `env` | string | Path to environment variables file |
| `auth` | null \| "jwt" \| object | Authentication configuration |
| `authorization` | string \| null | Path to an `AuthorizationBackend` (RBAC / per-tool access) |
| `checkpointer` | string \| null | Path to a custom checkpointer |
| `injectq` | string \| null | Path to an InjectQ container |
| `store` | string \| null | Path to a data store |
| `remote_tools` | array | Trusted schemas for tools executed by clients; attached at startup |
| `redis` | string \| null | Redis URL of the API's cache store (`${VAR}` expanded); falls back to `REDIS_URL` |
| `cache_store` | string \| null | Path to your own `BaseCacheStore`; wins over `redis` |
| `token_revocation` | bool \| object \| null | `true` rejects revoked tokens (`10xgraph revoke`); kept in the cache store |
| `rate_limit` | object \| null | Sliding-window rate limiting configuration |
| `thread_name_generator` | string \| null | Path to a custom thread name generator |
| `ag_ui` | object | `{"enabled": true}` mounts `POST /v1/ag-ui` (needs the `ag-ui` extra) |

Full reference: [Configuration](https://10xgraph.com/docs/reference/api-cli/configuration).

---

## Authentication

JWT or your own backend. Guide: [Authentication](https://10xgraph.com/docs/server/auth).

### JWT Authentication

**10xgraph.json:**
```json
{ "auth": "jwt" }
```

**.env:**
```bash
JWT_SECRET_KEY=your-super-secret-key
JWT_ALGORITHM=HS256
```

### Custom Authentication

**10xgraph.json:**
```json
{ "auth": { "method": "custom", "path": "auth.custom:MyAuthBackend" } }
```

**auth/custom.py:**
```python
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from tenxgraph_api import BaseAuth


class MyAuthBackend(BaseAuth):
    def authenticate(
        self,
        request: Request,
        response: Response,
        credential: HTTPAuthorizationCredentials,
    ) -> dict[str, Any] | None:
        token = credential.credentials
        user = verify_token(token)  # your token check
        if not user:
            raise HTTPException(401, "Invalid token")
        return {"user_id": user.id, "username": user.username, "email": user.email}
```

---

## ID generation

10xGraph includes Snowflake ID generation for distributed, time-sortable unique IDs.

```bash
pip install "10xgraph-api[snowflakekit]"
```

```python
from tenxgraph_api import SnowFlakeIdGenerator

generator = SnowFlakeIdGenerator(
    snowflake_epoch=1704067200000,  # Jan 1, 2024
    snowflake_node_id=1,
    snowflake_worker_id=1,
)
new_id = await generator.generate()
```

**Environment configuration:**
```bash
SNOWFLAKE_EPOCH=1704067200000
SNOWFLAKE_NODE_ID=1
SNOWFLAKE_WORKER_ID=1
SNOWFLAKE_TIME_BITS=39
SNOWFLAKE_NODE_BITS=5
SNOWFLAKE_WORKER_BITS=8
```

More: [Extensibility](https://10xgraph.com/docs/concepts/extensibility).

---

## Thread name generation

Generate human-friendly names for conversation threads.

```python
from tenxgraph_api.src.app.utils.thread_name_generator import AIThreadNameGenerator

generator = AIThreadNameGenerator()
name = generator.generate_name()
# "thoughtful-dialogue", "exploring-ideas", ...
```

Custom generators: [Extensibility](https://10xgraph.com/docs/concepts/extensibility).

---

## Security

Security features of the server:

- **Authentication:** JWT and custom authentication backends
- **Authorization:** scoped access on every endpoint, owner-only threads, extensible backends
- **Request limits:** configurable request size limit (10 MB default)
- **Error sanitization:** production-safe error messages that do not leak internals
- **Log sanitization:** automatic redaction of tokens, passwords and secrets
- **Security warnings:** startup validation for insecure configurations
- **Security headers:** HSTS, frame options and CSP, configurable

### Production Security Checklist

```bash
MODE=production                  # production mode
JWT_SECRET_KEY=<32+ chars>       # strong secret (secrets.token_urlsafe(32))
ORIGINS=https://yourdomain.com   # specific CORS origins (never *)
ALLOWED_HOST=yourdomain.com      # specific allowed hosts (never *)
DOCS_PATH=                       # recommended: disable API docs
REDOCS_PATH=
MAX_REQUEST_SIZE=10485760        # request size limit (10MB default)
```

See the [Production checklist](https://10xgraph.com/docs/server/production-checklist) and
[Authentication](https://10xgraph.com/docs/server/auth).

---

## Deployment

Guide: [Deploy](https://10xgraph.com/docs/server/deploy).

```bash
# Generate Docker files
10xgraph build --docker-compose

# Build and run
docker compose up --build -d

# Check logs
docker compose logs -f
```

For Kubernetes, `10xgraph build --k8s` writes `k8s.yaml`; see [Kubernetes](https://10xgraph.com/docs/server/kubernetes).

---

## Project structure

```
10xgraph-api/
├── tenxgraph_api/          # Main package
│   ├── __init__.py        # Package exports (BaseAuth, SnowFlakeIdGenerator, ThreadNameGenerator)
│   ├── cli/               # Typer CLI: main.py + commands/ + templates/
│   └── src/app/           # FastAPI application (main.py, loader.py, core/, routers/, utils/)
├── tests/                  # Test suite
├── 10xgraph.json          # Configuration
├── pyproject.toml          # Project metadata
└── README.md               # This file
```

---

## Development

```bash
# Clone and set up
git clone https://github.com/10xGraph/10xgraph-api.git
cd 10xgraph-api
uv sync --dev
pre-commit install

# Quality gate
pytest                                # tests (coverage gate: 80%)
pytest --cov=tenxgraph_api --cov-report=html
ruff check . && ruff format .         # lint + format
pre-commit run --all-files            # full gate (ruff + bandit, pinned versions)
```

### Using the Makefile

```bash
make build     # build sdist + wheel
make test      # run tests
make publish   # upload to PyPI (maintainers)
make clean     # remove build artifacts
```

### Releasing

Releases are cut by pushing a version tag that matches `pyproject.toml`. The
[`release.yml`](./.github/workflows/release.yml) workflow then verifies the tag, builds the
sdist + wheel, checks the distribution metadata, and creates a GitHub Release with auto-generated
notes and the artifacts attached. PyPI publishing is manual (`make publish`).

```bash
git tag v1.0.0rc3 && git push origin v1.0.0rc3
```

---

## License

10xGraph is [MIT licensed](https://github.com/10xGraph/10xgraph-api/blob/main/LICENSE) and made by
[10xScale](https://10xscale.ai). Contributions are accepted under the same license.

---

## Links

- Documentation: [10xgraph.com](https://10xgraph.com)
- Core framework: [`10xgraph`](https://github.com/10xGraph/10xGraph)
- PyPI: [`10xgraph-api`](https://pypi.org/project/10xgraph-api/)
- [Issues](https://github.com/10xGraph/10xgraph-api/issues)
- [Changelog](https://github.com/10xGraph/10xgraph-api/blob/main/CHANGELOG.md)

---

## Contributing

**Your avatar belongs on this wall.**

<a href="https://github.com/10xGraph/10xgraph-api/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=10xGraph/10xgraph-api" alt="People who have contributed to 10xgraph-api" />
</a>

Every person above shipped code that serves real agents in production. Merge one pull request and you join them, here and on the contributor page at [10xgraph.com/maintainers](https://10xgraph.com/maintainers).

Your first pull request can be small. These are real, self-contained, and useful today:

- **Type one module.** `pyproject.toml` lists the modules `mypy` still skips, each with its error count. Start with a one-error module, fix it, delete its line.
- **Add a test for an edge case you hit.** A request that should be rejected, a config that should fail at startup, a header that should be set.
- **Write the deployment recipe you needed.** Behind a reverse proxy, on a specific cloud, with your auth provider. Recipes go on [10xgraph.com](https://10xgraph.com), where every page has an "Edit this page" link.
- **Turn a bug into a failing test.** Open it as a draft pull request; the fix can come later.

From clone to a passing check:

```bash
git clone https://github.com/10xGraph/10xgraph-api.git
cd 10xgraph-api
uv sync --dev
uv run pytest
uv run pre-commit run --all-files   # ruff, bandit and hygiene checks, as in CI
uv run mypy
```

Draft pull requests are welcome, so open early and ask questions in the PR. For bigger changes, start a thread in the core repository's [Discussions](https://github.com/10xGraph/10xGraph/discussions) first so the work does not overlap.

---

## Support

- **Documentation:** [10xgraph.com](https://10xgraph.com/)
- **Issues:** [GitHub Issues](https://github.com/10xGraph/10xgraph-api/issues)
- **Repository:** [GitHub](https://github.com/10xGraph/10xgraph-api)

---

Developed by [10xScale](https://10xscale.ai), which runs its own AI products on 10xGraph.
