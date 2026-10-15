# Client Auth and Errors

Use this when changing `10xgraph-client` authentication, request headers, credentials, timeouts, debug behavior, or structured error handling.

## Auth Helpers

Public auth types and helpers live in https://github.com/10xGraph/10xgraph-client/blob/master/src/request.ts.

Auth union:

- `TenxGraphBearerAuth`: `{ type: "bearer", token }`
- `TenxGraphBasicAuth`: `{ type: "basic", username, password }`
- `TenxGraphHeaderAuth`: `{ type: "header", name, value, prefix? }`
- `TenxGraphAuth`: union of the above

Helpers:

- `bearerAuth(token)`
- `basicAuth(username, password)`
- `headerAuth(name, value, prefix?)`
- `buildHeaders(context, defaults?)`
- `getRequestCredentials(context)`

## TenxGraphClient Config

`TenxGraphConfig` fields:

- `baseUrl`: required API base URL.
- `authToken`: bearer-token shorthand.
- `auth`: structured auth helper.
- `headers`: additional headers merged into every request.
- `credentials`: browser fetch credentials mode.
- `timeout`: request timeout in milliseconds; default is 300000.
- `debug`: enables client debug logging.

Auth precedence:

1. Defaults are applied first.
2. `headers` are merged next.
3. If `auth` is set, it writes auth headers.
4. Otherwise, `authToken` writes `Authorization: Bearer ...` only if no authorization header already exists.

Use `credentials` for cookie-based auth or same-origin sessions.

## Structured Errors

All endpoint helpers call `createErrorFromResponse` for non-OK HTTP responses where implemented.

Core error types:

- `TenxGraphError`
- `BadRequestError`
- `AuthenticationError`
- `PermissionError`
- `NotFoundError`
- `ValidationError`
- `ServerError`
- `GraphError`
- `NodeError`
- `GraphRecursionError`
- `StorageError`
- `TransientStorageError`
- `MetricsError`
- `SchemaVersionError`
- `SerializationError`

Error instances expose:

- `statusCode`
- `errorCode`
- `requestId`
- `timestamp`
- `details`
- `context`
- `endpoint`
- `method`
- `recoverySuggestion`
- `getUserMessage()`
- `toJSON()`

## Rules

- Prefer structured `auth` helpers for new examples; keep `authToken` as shorthand.
- Do not overwrite an explicit user-provided `Authorization` header unless `auth` is set.
- Keep fetch `credentials` forwarding in every endpoint helper.
- Keep new server error codes mapped in `createErrorFromResponse`.
- In UI code, show `getUserMessage()` to users and log `toJSON()` for debugging.

## Source Map

- Auth/request helpers: https://github.com/10xGraph/10xgraph-client/blob/master/src/request.ts
- Error classes: https://github.com/10xGraph/10xgraph-client/blob/master/src/errors.ts
- Client facade: https://github.com/10xGraph/10xgraph-client/blob/master/src/client.ts
- Docs: https://10xgraph.com/
