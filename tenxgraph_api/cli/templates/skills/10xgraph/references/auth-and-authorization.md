# Auth and Authorization

Use this when changing HTTP authentication, authorization, permission checks, scopes, storage
isolation, or client auth examples.

One `auth` key in `10xgraph.json` covers authentication (who is the caller) and role-based
access (what may they do, how far can they see). `BaseAuth.authorize` is the one hook for custom
rules.

## Authentication Modes

`method` picks the class:

- `none` (or `"auth": null`): no authentication. Local development / trusted gateways only.
- `jwt` (`"auth": "jwt"` is a shortcut): requires `JWT_SECRET_KEY` and `JWT_ALGORITHM`. Token must carry `user_id` (non-empty string or int, else 401) and `exp`. Requests use `Authorization: Bearer <token>`. The decoded payload becomes the `user` context. With no roles configured: default permission set, reach `own`.
- `custom` plus `path`: `{ "auth": { "method": "custom", "path": "graph.auth:MyAuth" } }`. `path` with another method, or `custom` without a valid `path`, refuses to boot.

Custom classes subclass `BaseAuth` (`from tenxgraph_api import BaseAuth`). `authenticate` is **synchronous** (called without `await`)
and takes three args:

```python
def authenticate(self, request, response, credential) -> dict | None: ...
```

- `request` may be a `Request` or a `WebSocket` (both are `HTTPConnection`): read
  `request.headers`, do not assume Request-only APIs.
- `credential` is the parsed bearer token (`HTTPAuthorizationCredentials | None`). For
  API-key/cookie schemes, ignore it and read headers directly.
- Return the verified claims as a dict with at least `user_id` (keys flow into `config["user"]`, which is the core `Identity`); raise `UserAccountError`/`HTTPException` to reject (401 HTTP, 1008 close on WebSocket).
- `user_id` must not start with `"__system__"` (`SYSTEM_USER_PREFIX` in core): `system_user_id(name)` = `"__system__:<name>"` owns shared data (each agent's memory, RAG knowledge bases). The API refuses any identity whose id starts with that prefix, from any auth backend, with 401 "This user id is reserved" (WebSocket close 1008) and a warning in the log. Auth-off development still uses `"anonymous"`.
- Do NOT declare it `async def`: an un-awaited coroutine breaks auth silently.
- A token without a configured tenant claim, or a malformed roles/permissions claim, is 401.

## Roles, permissions and reach

RBAC keys sit inside `auth` and apply to any auth class. They are refused when auth is off.

```json
{
  "auth": {
    "method": "jwt",
    "roles_claim": "roles",
    "tenant_claims": ["org_id"],
    "default_role": "member",
    "roles": {
      "admin": { "permissions": ["*"], "reach": "tenant" },
      "member": { "permissions": ["graph:*", "threads:*"], "reach": "own" },
      "viewer": { "inherits": ["member"], "permissions": ["threads:read"], "reach": "own" }
    },
    "system_identities": { "nightly": { "roles": ["member"], "tenant": { "org_id": "acme" } } }
  }
}
```

- `roles`: name -> `{permissions, inherits, reach}`. `reach` is required per role, never a fallback.
- `roles_claim` or `permissions_claim` (not both): the token claim that carries roles or direct permissions. Token `scope`/`scopes` only narrow what is granted.
- `tenant_claims`: claims copied to the identity's tenant (needs to match the checkpointer and store `tenant_columns`; boot refuses in production on a mismatch, warns in development).
- `default_role`: role for callers with none. `system_identities`: non-user callers (below).
- No `roles`: default permission set (everything except `admin:*`), reach `own`.
- Permissions are `resource:action`, from one registry: `graph` (read, invoke, stream, stop, fix), `threads` (read, write, delete), `store` (read, write, delete), `files` (read, upload, delete), `config:read`, `tasks` (trigger, read, cancel), `admin` (read, write). Wildcards: `resource:*` and `*`. There is no `tasks:pause`. Reading the graph is `graph:read`.
- Reach is `own` | `tenant` | `all`. It widens reads, lists, deletes and stop only. Runs and state/message writes always stay `own`, admins included. `tenant` needs `tenant_claims`; `all` roles are logged at boot.
- Unknown permission, unknown role in `default_role`/`inherits`/`system_identities`, an inheritance cycle, or an unknown reach refuses to boot. `10xgraph config`, `audit` and `build` run the same check.
- The top-level `authorization` and `graph_introspection` keys are removed: the server refuses to boot with either.

## Custom rules: `BaseAuth.authorize`

```python
class MyAuth(BaseAuth):
    def authenticate(self, request, response, credential): ...

    async def authorize(self, identity, permission, resource=None) -> bool:
        if not await super().authorize(identity, permission, resource):  # keeps RBAC
            return False
        return True  # add your own rule; resource is the thread/task id, may be None
```

- Default is `identity.can(permission)`. If `roles` are configured, overriding without `super().authorize` drops them.
- It takes no request, so the jobs worker calls it too. False answers 403.
- Foreign resources (another user's thread, message, memory, file or run) answer **404**, enforced in storage by the caller's reach. There is no ownership cache or resolver.

## Who am I: `GET /v1/me`

Returns `user_id`, `tenant`, `roles`, `permissions`, `reach`. With auth off it returns `{"auth": "off"}`. Use it so a UI shows only what the caller may do.

## Jobs worker

With auth on, the worker rebuilds the identity from the stored claims with the current role config, then checks `tasks:trigger` through `authorize`. A refused or invalid identity fails the run without executing. A task label `identity="<name>"` runs calls that have no `config["user"]` as `__system__:<name>` using `auth.system_identities`; it never replaces a caller's identity. `/v1/tasks` GET and cancel widen by reach; trigger and PATCH stay own. Auth off changes nothing.

## Token revocation

`"cache_store": {"type": "redis", "url": "${REDIS_URL}", "token_revocation": true}` (or
`"token_revocation": {"enabled": true, "token_lifetime": 3600, "user_ttl": 2592000}` inside
`cache_store`) rejects revoked tokens with 401 before they expire: by `jti` (`10xgraph revoke --jti`) or every
token of a user issued before a cutoff (`10xgraph revoke --user`). Revocations live in the API's
cache store, so share them between workers with `cache_store` type `redis` or `custom`; with
the in-memory store a revocation reaches one process and `10xgraph revoke` refuses to run
(`--redis-url` points it at a Redis directly). It fails closed: while the cache store is
unreachable, authenticated requests get 503 and the server refuses to start. The value is
`true`, `false`, `null` or the object above; a string or a top-level `token_revocation` is a
config error. It only applies with auth on.

## Data isolation (config["user"])

Storage (checkpointer, store) filters every query by the caller's reach, taken from `config["user"]`. It is set server-side only; clients cannot forge it.

Tenant and extra column values (`org_id`, `project_id`, `public_id`) come only from `config["user"]`: return them from `BaseAuth.authenticate()` (or put them in the JWT claims; the decoded token is the user). Never read them from request config, metadata, state or model output.

Server-owned config keys (`RESERVED_CONFIG_KEYS` in `core/validation/request_config.py`: `user`, `user_id`, `remote_tools`, `file_tool_root`, `workspace_root`, `tool_timeout`, `node_timeout`, `max_parallel_tool_calls`, `collection`, plus any `_`-prefixed key) are dropped from client config by `client_config` on every route. They are ignored, not refused (debug log). A client never picks a Qdrant `collection`; see memory-and-store.md.

## Guarantees

- Routes are guarded by construction: `assert_all_routes_protected` refuses to boot if any
  non-public route lacks `RequirePermission` or `RequireIdentity`. Public: `/ping` and dev-only eval endpoints.
- Return 401 for auth failure, 403 for a missing permission, 404 for a resource outside the caller's reach.

## TypeScript Client

```typescript
const client = new TenxGraphClient({
  baseUrl: "http://127.0.0.1:8000",
  headers: { Authorization: `Bearer ${token}` },
});
```

## Rules

- No unauthenticated API mode in production unless a trusted gateway handles auth.
- Keep `authenticate` synchronous; keep authorization out of graph business logic.
- Sanitize logging for tokens and user payloads.
- Add new routes to the permission registry and guard them with `RequirePermission`.

## Source Map

- Base auth: tenxgraph_api/src/app/core/auth/base_auth.py
- JWT auth: tenxgraph_api/src/app/core/auth/jwt_auth.py
- Config parser and boot checks: tenxgraph_api/src/app/core/auth/auth_config.py
- Roles and reach: tenxgraph_api/src/app/core/auth/rbac.py
- Permission registry: tenxgraph_api/src/app/core/auth/permissions_registry.py
- Dependencies (`RequireIdentity`, `RequirePermission`): tenxgraph_api/src/app/core/auth/permissions.py
- `GET /v1/me`: tenxgraph_api/src/app/routers/me.py
- Jobs identity: tenxgraph_api/jobs/middleware.py
- Boot-time route guard: tenxgraph_api/src/app/core/auth/route_guard.py
- Identity contract: tenxgraph/core/authz.py
- Docs: https://10xgraph.com/
