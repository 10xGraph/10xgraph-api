# Auth and Authorization

Use this when changing HTTP authentication, authorization, permission checks, scopes, storage
isolation, or client auth examples.

Two independent layers:

- Authentication (`BaseAuth`): who is the caller? Produces a `user` dict.
- Authorization (`AuthorizationBackend`): what may they do, and whose data can they touch?

## Authentication Modes

Default:

- `auth: null`
- No authentication. Local development / trusted gateways only.

JWT:

- `auth: "jwt"`
- Requires `JWT_SECRET_KEY` and `JWT_ALGORITHM`. Token must carry `user_id` and `exp`.
- Requests use `Authorization: Bearer <token>`. Decoded payload becomes the `user` context.

Custom:

```json
{ "auth": { "method": "custom", "path": "graph.auth:MyAuthBackend" } }
```

Custom backends subclass `BaseAuth`. `authenticate` is **synchronous** (called without `await`)
and takes three args:

```python
def authenticate(self, request, response, credential) -> dict | None: ...
```

- `request` may be a `Request` or a `WebSocket` (both are `HTTPConnection`): read
  `request.headers`, do not assume Request-only APIs.
- `credential` is the parsed bearer token (`HTTPAuthorizationCredentials | None`). For
  API-key/cookie schemes, ignore it and read headers directly.
- Return a dict with at least `user_id` (keys flow into `config["user"]`); `{}`/`None` = anonymous;
  raise `UserAccountError`/`HTTPException` to reject (401 HTTP, 1008 close on WebSocket).
- `user_id` must not start with `"__system__"` (`SYSTEM_USER_PREFIX` in core): `system_user_id(name)` = `"__system__:<name>"` owns shared data (each agent's memory, RAG knowledge bases). The API refuses any identity whose id starts with that prefix, from any auth backend, with 401 "This user id is reserved" (WebSocket close 1008) and a warning in the log. Auth-off development still uses `"anonymous"`.
- Do NOT declare it `async def`: an un-awaited coroutine breaks auth silently.
- Include `roles` / `scopes` in the returned dict to feed authorization.

## Authorization

Configured via the `authorization` key; separate from `auth`. Values:

- `null` (unset): mode-based default: `"ownership"` in production, `"allow_all"` in development.
- `"ownership"`: owner-only. A thread is readable/runnable/deletable only by its creator; a
  foreign `invoke`/`stream` is rejected up front (403). Enforced on every thread-touching step.
- `"allow_all"` (aliases `"default"`, `"none"`): any authenticated user may do anything.
- `"module:attr"`: custom `AuthorizationBackend`.
- `{ "backend": "rbac", ... }`: role-based access control (below).

Ownership is scalable: ownership is immutable, so it is cached (in-process LRU + the shared
cache store when it is not in-memory: `cache_store` type `redis` or `custom`) via
`ThreadOwnershipResolver` over
`BaseCheckpointer.aget_thread_owner`. After the first lookup a check is an in-memory hit, not a
per-request DB call; the cache is evicted on thread deletion. Ownership needs a persistent
checkpointer (pg/sqlite/in-memory); with none, requests pass through.

### Token revocation

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

### Custom backend interface

```python
class MyAuthz(AuthorizationBackend):
    async def authorize(
        self, user: dict, resource: str, action: str,
        resource_id: str | None = None, **context,
    ) -> bool: ...                     # required; False -> 403

    def isolation_scope(self) -> str:  # optional; "owner" | "none" (default "owner")
        return "owner"

    def scopes_for(self, user: dict) -> list[str] | None:  # optional; None = permissive
        return user.get("scopes")
```

Subclass `OwnershipAuthorizationBackend` to inherit cached owner-only checks and override only
what you need.

### Scopes

Each endpoint requires the scope `"<resource>:<action>"`. `RequirePermission` checks it against
`authz.scopes_for(user)`; `None` = permissive (default, nothing breaks until scopes are issued).
The default reads the token's `scope` claim (space-separated, OAuth style) and `scopes` (list or
space-separated string) and matches exact names; a claim of the wrong type is a 401.

- `graph`: `invoke`, `stream`, `stop`, `fix`, `setup`, `read`
- `checkpointer`: `read`, `write`, `delete`
- `store`: `read`, `write`, `delete`
- `files`: `upload`, `read`
- `config`: `read`

### RBAC config (no code)

```json
{
  "authorization": {
    "backend": "rbac",
    "roles": { "admin": ["*"], "member": ["graph:invoke", "checkpointer:read"] },
    "default_scopes": ["graph:read"],
    "isolation": "owner"
  }
}
```

Maps `user["roles"]` to scopes on top of owner isolation. `"*"` -> all scopes. `default_scopes`
granted to everyone. `isolation`: `"owner"` | `"none"`.

### Data isolation (config["authz"])

API checks decide access; the storage layer (checkpointer, store) enforces isolation from a
trusted policy. After a successful check, `RequirePermission` stamps
`user["authz"] = {user_id, scope, scopes}` where `scope` = backend `isolation_scope()`. Services
copy the trusted `user` into `config["user"]`, so the core library reads `config["user"]["authz"]`
and scopes rows to the caller (`owner`) or not (`none`). Set server-side only; not client-forgeable.

Server-owned config keys (`RESERVED_CONFIG_KEYS` in `core/auth/request_config.py`: `authz`, `user`, `user_id`, `remote_tools`, `file_tool_root`, `workspace_root`, `tool_timeout`, `node_timeout`, `max_parallel_tool_calls`, `collection`, plus any `_`-prefixed key) are dropped from client config by `client_config` on every route. They are ignored, not refused (debug log). A client never picks a Qdrant `collection`; see memory-and-store.md.

## Guarantees

- Routes are guarded by construction: `assert_all_routes_protected` refuses to boot if any
  non-public route lacks `RequirePermission`. Public: `/ping` and dev-only eval endpoints.
- Return 401 for auth failure, 403 for authorization/scope failure.

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
- Update the scope catalog and permission tables when adding routes.

## Source Map

- Base auth: tenxgraph_api/src/app/core/auth/base_auth.py
- JWT auth: tenxgraph_api/src/app/core/auth/jwt_auth.py
- Auth loader/dependency: tenxgraph_api/src/app/core/auth/auth_backend.py
- Authorization backends (ownership, RBAC): tenxgraph_api/src/app/core/auth/authorization.py
- Permission dependency + scope check + authz stamp: tenxgraph_api/src/app/core/auth/permissions.py
- Ownership cache: tenxgraph_api/src/app/core/auth/ownership_resolver.py
- Boot-time route guard: tenxgraph_api/src/app/core/auth/route_guard.py
- Scope catalog + authz contract: tenxgraph/core/authz.py
- Docs: https://10xgraph.com/
