# 10xGraph API

*Formerly `10xscale-agentflow-cli`.* The server and CLI half of 10xGraph: write your AI logic, and this package runs the API around it.

[![CI](https://github.com/10xGraph/10xgraph-api/actions/workflows/ci.yaml/badge.svg)](https://github.com/10xGraph/10xgraph-api/actions/workflows/ci.yaml)
[![Release](https://img.shields.io/github/v/release/10xGraph/10xgraph-api)](https://github.com/10xGraph/10xgraph-api/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/10xgraph-api?color=blue)](https://pypi.org/project/10xgraph-api/)
[![Python](https://img.shields.io/pypi/pyversions/10xgraph-api)](https://pypi.org/project/10xgraph-api/)
[![License](https://img.shields.io/github/license/10xGraph/10xgraph-api)](https://github.com/10xGraph/10xgraph-api/blob/main/LICENSE)
[![Verified on FreeAPI](https://findfreeapi.com/api/badge/10xgraph-api)](https://findfreeapi.com/en/service/10xgraph-api)

10xGraph is open-source, self-hosted infrastructure for AI applications. You write the agents, tools and tasks; 10xGraph runs them behind an API server with authentication, role-based access, tenant isolation, durable state in Postgres and Redis, background jobs, metrics and tracing, and generates Docker and Kubernetes deploy files. MIT licensed, by 10xScale. Formerly Agentflow.

`10xgraph-api` is the server and the `10xgraph` command. Point `10xgraph.json` at your compiled graph and it serves REST, NDJSON streaming, WebSocket and realtime-audio endpoints behind authentication, roles and tenant-aware access control, then writes your Docker and Kubernetes files. Built for teams going through SOC 2 and ISO 27001 audits: see the [control mapping](https://10xgraph.com/docs/production/control-mapping) for what the platform supports and what stays with you.

---

## What the server gives you

- **Endpoints generated from the graph.** Invoke, NDJSON stream, stop, WebSocket and realtime audio; threads, state and messages; the memory store; file uploads for multimodal input; and an optional [AG-UI endpoint](https://10xgraph.com/docs/server/ag-ui) for CopilotKit and other AG-UI clients. See [Invoke and stream](https://10xgraph.com/docs/server/invoke-and-stream).
- **Authentication.** `"auth": "jwt"`, or your own `BaseAuth` subclass. See [Authentication](https://10xgraph.com/docs/server/auth).
- **Roles with reach.** Each role carries permissions such as `graph:invoke` or `threads:delete`, and a reach of `own`, `tenant` or `all`. Reach widens reads, lists, deletes and stop; runs and writes always stay with the owner. The server refuses to boot if a non-public route has no permission guard. See [Tenancy and access](https://10xgraph.com/docs/concepts/tenancy-and-access).
- **Tenant isolation.** Ownership is enforced in storage: a thread, message, memory, file or run outside the caller's reach answers 404. You declare the tenant columns; `tenant_claims` must match them.
- **Cache store.** One `cache_store` object holds run locks and caps, idempotency, token revocation and rate-limit counters: in memory, on Redis, or your own `BaseCacheStore`. See [Cache store](https://10xgraph.com/docs/concepts/cache-store) and [Rate limiting](https://10xgraph.com/docs/server/rate-limiting).
- **Background jobs.** A thin TaskIQ layer: `10xgraph worker`, `10xgraph scheduler` and `/v1/tasks` routes, run under the same auth. See [Background tasks](https://10xgraph.com/docs/server/background-tasks).
- **Metrics, logs and audit.** Prometheus metrics from the `metrics` block, JSON logs with `LOG_FORMAT=json`, and a metadata-only audit log from the `audit` block that you ship to your own sink. 10xGraph stores no audit data. See [Observability](https://10xgraph.com/docs/server/observability), [Metrics with Prometheus and Grafana](https://10xgraph.com/docs/guides/metrics-prometheus-grafana) and [Audit log](https://10xgraph.com/docs/server/audit-log).
- **Hardened defaults.** Request size limits, security headers, error and log sanitization, SSRF checks on external media URLs, startup warnings for insecure settings. See the [Production checklist](https://10xgraph.com/docs/server/production-checklist).
- **Deploy files.** `10xgraph build --docker-compose --k8s` writes a `Dockerfile`, `docker-compose.yml` and `k8s.yaml`. See [Deploy](https://10xgraph.com/docs/server/deploy) and [Kubernetes](https://10xgraph.com/docs/server/kubernetes).

---

## Installation

```bash
pip install 10xgraph-api
```

Optional extras; install only what you configure:

```bash
pip install "10xgraph-api[jwt,redis,websocket]"  # JWT auth, Redis cache store, WebSocket and realtime audio
pip install "10xgraph-api[jobs]"      # background jobs (TaskIQ worker and scheduler)
pip install "10xgraph-api[metrics]"   # Prometheus scrape or OTLP push metrics
pip install "10xgraph-api[otel]"      # OpenTelemetry tracing
pip install "10xgraph-api[ag-ui]"     # AG-UI endpoint
pip install "10xgraph-api[media]"     # document text extraction (multimodal)
pip install "10xgraph-api[images]"    # image resize, EXIF, format conversion
pip install "10xgraph-api[snowflakekit]"  # Snowflake ID generation
pip install "10xgraph-api[sentry]"    # Sentry error reporting
pip install "10xgraph-api[gcloud]"    # Google Cloud Logging
pip install "10xgraph-api[firebase]"  # Firebase Admin SDK
```

`jobs-rabbitmq` and `jobs-kafka` add a RabbitMQ or Kafka broker for jobs.

Requires Python 3.12 or newer. Installs the core `10xgraph` package as a dependency.

---

## Quick start

```bash
# 1. Scaffold a project (asks for a name, a template and an AI provider)
10xgraph init

# 2. Run the dev server and open the playground (127.0.0.1:8000)
10xgraph dev
```

Auth is off in the Quick Start template. To turn it on, add the `auth` object to `10xgraph.json` and set a secret in `.env`:

```json
{ "auth": { "method": "jwt" } }
```

```bash
JWT_SECRET_KEY=<32+ characters>
```

`"auth": "jwt"` is a shortcut for the same thing. Every authenticated user then gets the default permission set with reach `own`. Add `roles` and `tenant_claims` for the setup below, or pick the Production template in `10xgraph init`, which writes JWT auth, a Redis cache store with rate limits and token revocation, a prompt-injection validator, evals and tests.

---

## Configuration

`10xgraph.json` defines your agent and the infrastructure around it. A realistic example:

```json
{
  "agent": "graph.react:app",
  "env": ".env",
  "auth": {
    "method": "jwt",
    "roles_claim": "roles",
    "tenant_claims": ["org_id"],
    "roles": {
      "member": {
        "permissions": ["graph:*", "threads:*", "store:*", "files:*", "tasks:trigger", "tasks:read", "tasks:cancel"],
        "reach": "own"
      },
      "org_admin": { "inherits": ["member"], "permissions": ["admin:read"], "reach": "tenant" }
    },
    "default_role": "member"
  },
  "cache_store": {
    "type": "redis",
    "rate_limit": { "enabled": true, "requests": 100, "window": 60, "by": "user", "fail_open": false },
    "token_revocation": true
  },
  "audit": { "enabled": true, "sinks": ["log"] }
}
```

- The Redis URL comes from the `REDIS_URL` environment variable when `cache_store.url` is unset.
- `tenant_claims` must match the tenant columns declared on your checkpointer and store. A mismatch refuses to boot in production and warns in development.
- Audit sinks are `log` (JSON lines from the `tenxgraph_api.audit` logger), `publisher` (your events publisher) or `module:Class` (a `BaseAuditSink`).

Other keys include `checkpointer`, `store`, `injectq`, `thread_name_generator`, `id_generator`, `ag_ui`, `websocket`, `runs` and `media`. Edit them in a browser with `10xgraph config`, or read the [configuration reference](https://10xgraph.com/docs/reference/api-cli/configuration) and [Configure the server](https://10xgraph.com/docs/server/configure).

For a custom auth class, set `{ "auth": { "method": "custom", "path": "auth.custom:MyAuth" } }` and subclass `tenxgraph_api.BaseAuth`. See [Authentication](https://10xgraph.com/docs/server/auth) and [Extensibility](https://10xgraph.com/docs/concepts/extensibility).

---

## CLI

Run `10xgraph --help` or `10xgraph COMMAND --help` for options. Reference: [CLI](https://10xgraph.com/docs/server/cli).

| Command | What it does |
|---|---|
| `10xgraph init` | Scaffold a project. Asks for name, template (Quick Start or Production) and AI provider. |
| `10xgraph dev` | Run the dev server with reload and open the hosted playground. `--no-open` runs the server only. |
| `10xgraph config` | Edit `10xgraph.json` in a local browser editor, with validation and a `.bak` of the previous file. |
| `10xgraph build` | Write a `Dockerfile`, and with `--docker-compose` and `--k8s` a Compose file and a Kubernetes manifest. |
| `10xgraph worker` | Run the TaskIQ worker for background jobs. |
| `10xgraph scheduler` | Run the TaskIQ scheduler. Run exactly one. |
| `10xgraph revoke` | Revoke one token (`--jti`) or all tokens of a user (`--user`). |
| `10xgraph audit` | Read-only check of the interpreter, installed packages, `10xgraph.json` and the default port. Exits `1` on failure, so it works as a CI gate. |
| `10xgraph test` | Run the project tests with pytest. |
| `10xgraph eval` | Run agent evaluations and write JSON and HTML reports to `eval_reports/`. |
| `10xgraph skills` | Install the bundled 10xGraph skill for coding agents (Codex, Claude, GitHub Copilot). |
| `10xgraph demo` | Preview the terminal animations without touching the project. |
| `10xgraph version` | Show the CLI and core versions. |

Machine-readable output is available with `--format json` or `--format jsonl`, and `--no-animation` gives a static screen.

---

## Deploy

```bash
10xgraph build --docker-compose --k8s
docker compose up --build -d
```

This writes a multi-stage, non-root `Dockerfile` that runs Gunicorn with `MODE=production` and `LOG_FORMAT=json`, plus `docker-compose.yml` and `k8s.yaml`. Liveness is `/ping`; readiness is `/health/ready` and checks the checkpointer, store and cache store. The manifest has a fixed replica count, so autoscaling is yours. Run more than one worker or replica only with a Redis cache store. See [Deploy](https://10xgraph.com/docs/server/deploy), [Kubernetes](https://10xgraph.com/docs/server/kubernetes) and [Scaling](https://10xgraph.com/docs/production/scaling).

---

## Honest limits

- **Auth is off until you configure it.** The bare server has hardened defaults, not authentication. The Production template of `10xgraph init` turns it on.
- **Tenant isolation needs columns you declare.** Declare `tenant_columns` on the checkpointer and store; it is not automatic.
- **Permissions apply per route, not per tool.** There is no built-in rule such as "user A may call `refund`, user B may not". Tools can read the caller's verified identity from the run config and decide for themselves.
- **No hosted service.** You run it, or nobody does.
- **No audit evidence of its own.** 10xGraph supports your controls; it does not hold a SOC 2 report. See [Limits](https://10xgraph.com/docs/production/limits).

---

## One stack

| Package | What it does | Install |
|---|---|---|
| [`10xgraph`](https://github.com/10xGraph/10xGraph) | Core: graphs, state, replay-safe tools, checkpointing, memory | `pip install 10xgraph` (pulled in by this package) |
| `10xgraph-api` (this package) | Server and the `10xgraph` command | `pip install 10xgraph-api` |
| [`10xgraph-client`](https://github.com/10xGraph/10xgraph-client) | Typed TypeScript client for every endpoint | `npm install 10xgraph-client` |
| [Docs](https://10xgraph.com/docs) | Guides, reference and the control mapping | [10xgraph.com](https://10xgraph.com) |

---

## Migrating from 10xscale-agentflow-cli

10xGraph is the new name of 10xScale Agentflow. The server, CLI, license and maintainers are the same, and existing projects keep working.

| | Before | Now |
|---|---|---|
| PyPI package | `10xscale-agentflow-cli` | `10xgraph-api` |
| Python import | `agentflow_cli` | `tenxgraph_api` |
| Command | `agentflow` | `10xgraph` |
| Config file | `agentflow.json` | `10xgraph.json` |
| Core package | `10xscale-agentflow` (import `agentflow`) | `10xgraph` (import `tenxgraph`) |
| CLI env vars | `AGENTFLOW_NO_FULLSCREEN`, `AGENTFLOW_NO_SPINNER`, `AGENTFLOW_ASCII` | `TENXGRAPH_NO_FULLSCREEN`, `TENXGRAPH_NO_SPINNER`, `TENXGRAPH_ASCII` |
| Website | agentflow.10xscale.ai | [10xgraph.com](https://10xgraph.com) |
| GitHub | github.com/10xHub | [github.com/10xGraph](https://github.com/10xGraph) |

```bash
pip uninstall 10xscale-agentflow-cli 10xscale-agentflow
pip install 10xgraph-api
```

Do not install `10xscale-agentflow` next to `10xgraph`: both ship an `agentflow` module.

Kept working until 2.0:

- The `agentflow` command runs the same CLI and prints a deprecation notice.
- `from agentflow_cli import ...` resolves to `tenxgraph_api` with a `DeprecationWarning`.
- `agentflow.json` is read when no `10xgraph.json` sits in the same directory; `10xgraph.json` wins when both exist. The keys are unchanged.
- `AGENTFLOW_*` CLI env vars are read when the `TENXGRAPH_*` variable is unset.
- The `agentflow-bearer` WebSocket subprotocol and `agentflow://media/` URLs are still accepted. New references use `10xgraph-bearer` and `graph://media/`.

More: [Coming from Agentflow](https://10xgraph.com/docs/get-started/coming-from-agentflow).

---

## Security

Report vulnerabilities privately; see [SECURITY.md](https://github.com/10xGraph/10xgraph-api/blob/main/SECURITY.md). Do not open a public issue for a security problem. Production settings are listed in the [Production checklist](https://10xgraph.com/docs/server/production-checklist) and the [security model](https://10xgraph.com/docs/production/security-model).

---

## Development

```bash
git clone https://github.com/10xGraph/10xgraph-api.git
cd 10xgraph-api
uv sync --dev
pre-commit install

pytest                         # tests (coverage gate: 80%)
ruff check . && ruff format .  # lint and format
uv run mypy
```

## Contributing

Pull requests are welcome, and drafts are fine: open early and ask questions in the PR. Good first contributions:

- Type one module that `mypy` still skips (listed in `pyproject.toml`).
- Add a test for an edge case you hit: a request that should be rejected, a config that should fail at startup.
- Write the deployment recipe you needed on [10xgraph.com](https://10xgraph.com); every docs page has an "Edit this page" link.

For bigger changes, start a thread in the core repository's [Discussions](https://github.com/10xGraph/10xGraph/discussions) first.

<a href="https://github.com/10xGraph/10xgraph-api/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=10xGraph/10xgraph-api" alt="People who have contributed to 10xgraph-api" />
</a>

## License

[MIT](https://github.com/10xGraph/10xgraph-api/blob/main/LICENSE). Made by [10xScale](https://10xscale.ai), which runs its own AI products on 10xGraph.

## Links

[Documentation](https://10xgraph.com) · [PyPI](https://pypi.org/project/10xgraph-api/) · [Issues](https://github.com/10xGraph/10xgraph-api/issues) · [Changelog](https://github.com/10xGraph/10xgraph-api/blob/main/CHANGELOG.md)
