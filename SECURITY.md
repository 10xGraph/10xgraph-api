# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `1.0.x` (`10xgraph-api`) | Yes |
| `< 1.0` | No |

`10xgraph-api` is at 1.0 (release candidate `1.0.0rc2`). Security fixes are applied to the
latest published release only; there are no long-term support branches. The old
`10xscale-agentflow-cli` package is no longer published; upgrade to `10xgraph-api` to receive
fixes.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report privately through either channel:

1. **GitHub private vulnerability reporting** (preferred) - go to the
   [Security tab](https://github.com/10xGraph/10xgraph-api/security/advisories/new) and
   open a draft advisory.
2. **Email** - `contact@10xscale.ai`, with `SECURITY` in the subject line.

Please include:

- The affected version (`10xgraph version` output).
- A description of the issue and its impact.
- Reproduction steps or a proof of concept.
- Any relevant configuration (`10xgraph.json`, auth mode, rate limit backend) with
  secrets redacted.

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement of your report | 3 business days |
| Initial assessment and severity | 7 business days |
| Fix released, or a status update if longer | 30 days |

We will credit you in the release notes unless you ask us not to. Please give us a
reasonable window to ship a fix before publishing details.

## Scope

In scope:

- Authentication and authorization bypass in `tenxgraph_api/src/app/core/auth/`,
  including cross-user (IDOR) access to threads, checkpoints, or store memories.
- Route guard bypass - reaching a non-public route without a `RequirePermission` check.
- Rate limit bypass.
- Secret leakage through logs, error responses, or generated scaffolding.
- Path traversal or arbitrary file write in the media/upload endpoints or in
  `10xgraph init` scaffolding.
- Insecure defaults that a deployment would inherit without noticing.

Out of scope:

- Vulnerabilities in a user's own agent graph, tools, or `BaseAuth` subclass.
- Issues that require an already-compromised host or an attacker-controlled
  `10xgraph.json`.
- Missing hardening in a deliberately permissive development configuration
  (`MODE=development`).
- Denial of service through unbounded resource use in a user-supplied tool.

## Deployment hardening

The API refuses to start on some misconfigurations rather than warning - notably wildcard
CORS combined with credentials in production. For anything else, in production set:

- `MODE=production` (debug is off in production)
- `ORIGINS` to an explicit comma-separated list, never `*`
- `JWT_SECRET_KEY` to a random value of 32 characters or more
- An `authorization` backend (it defaults to `ownership` whenever auth is on; do not weaken
  it to `allow_all`)
- `STRICT_PRODUCTION_CHECKS=true` to refuse to start on wildcard `ALLOWED_HOST` or `ORIGINS`
  or enabled API docs
- `runs.thread_ids` at its default `"server"`, so nobody can claim an id before the user it
  was meant for (AG-UI and `/v1/graph/live` still accept frontend-made ids; `"client"` opts out)
- `runs.config_keys` / `runs.state_keys` if a tool or node trusts a value such as `role` or
  `plan` from request config or state; without them a client can send any non-reserved key
- `runs.timeout` and `runs.max_concurrent_per_user` to bound what one account can hold open
- `token_revocation` if you need logout or stolen-token revocation before a JWT expires
- `rate_limit.trusted_proxies` to your proxy's networks when `trusted_proxy_headers` is on
- Keep `graph_introspection: "scoped"` (the production default) unless every user may see
  the graph's structure and tool schemas

### Running more than one worker

Some protections keep state per process. With more than one worker or instance:

- **Thread ownership cache.** Each worker caches thread owners for 30 seconds. Configure
  Redis (the `redis` key in `10xgraph.json` or `REDIS_URL`) so the cache, and the
  tombstone a thread delete leaves behind, are shared. The tombstone stops a deleted thread
  id from being given to a new owner while another worker may still cache the old one.
  Without Redis the tombstone only exists on the worker that handled the delete, so for up
  to 30 seconds another worker can still let the old owner into a thread recreated under
  the same id.
- **Run and WebSocket caps** (`runs.max_concurrent_per_user`, `websocket.max_connections`,
  `websocket.max_connections_per_user`). With Redis configured they are shared through
  expiring leases, so the cap holds across workers and a crashed worker's slots free up on
  their own within a minute. Without Redis each process counts its own, so the real cap is
  the limit times the number of workers.
- **Rate limiting.** Use `"backend": "redis"` in `rate_limit`; the memory backend counts per
  process.
- **Media extraction cache.** Extracted document text is cached per process. This only costs
  repeated extraction work, not correctness.
