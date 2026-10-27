# REST API and Errors

Use this when changing API routes, response envelopes, schemas, Swagger docs, or error handling.

## Active Routers

Registered in https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/routers/setup_router.py:

- graph
- checkpointer / threads
- store
- ping
- media / files

## Graph Routes

- `POST /v1/graph/invoke`
- `POST /v1/graph/stream`
- `GET /v1/graph`
- `GET /v1/graph:StateSchema`
- `POST /v1/graph/stop`
- `POST /v1/graph/fix` - repair a stuck thread: removes assistant tool calls with no result and tool results with no call; keeps calls a paused thread is waiting on
- `WS /v1/graph/live` — realtime audio bridge, exposed only when the configured graph is rooted at a
  `LiveAgent`. First frame is a JSON control frame (`model`, `thread_id`, `voice`, ... override
  build-time config); upstream binary = PCM16 input audio, JSON control = text/activity/close;
  downstream binary = PCM16 model audio, JSON = transcripts/turn_complete/interrupted/tool_call/
  error. See `realtime.md`.

## Thread / Checkpointer Routes

These expose thread state, messages, thread listing/details, message mutation, and deletion. Use the docs and router source for exact method/path names because client helpers map to these endpoints.

TypeScript client wrappers include:

- `threadState`
- `updateThreadState`
- `clearThreadState`
- `threadDetails`
- `threads`
- `threadMessages`
- `addThreadMessages`
- `singleMessage`
- `deleteMessage`
- `deleteThread`

## Store Routes

- `POST /v1/store/memories`
- `POST /v1/store/search`
- `POST /v1/store/memories/{memory_id}`
- `POST /v1/store/memories/list`
- `PUT /v1/store/memories/{memory_id}`
- `DELETE /v1/store/memories/{memory_id}`
- `POST /v1/store/memories/forget`

## File Routes

- `POST /v1/files/upload`
- `GET /v1/files/{file_id}`
- `GET /v1/files/{file_id}/info`
- `GET /v1/files/{file_id}/url`
- `GET /v1/config/multimodal`

## Ping

Use the ping route for liveness/connectivity checks. The TypeScript client wraps this as `client.ping()`.

## Response and Error Handling

API helpers wrap successful responses with metadata and request context. Error handlers normalize framework exceptions such as graph errors, node errors, recursion errors, storage errors, validation errors, auth errors, and generic exceptions.

Common status expectations:

- 400/422 for invalid input.
- 401 for unauthenticated requests.
- 403 for authorization failures.
- 404 for missing resources.
- 500 for unexpected graph/server failures.

## Rules

- Keep Pydantic schemas, router docs, TypeScript endpoint types, and docs aligned.
- Use `success_response` helpers for consistent envelopes.
- Preserve SSE format for streaming responses.
- Sanitize error logs and avoid exposing secrets in details.
- Add Swagger response metadata for new routes.

## Source Map

- Route setup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/routers/setup_router.py
- Graph router/schema/service: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/graph
- Checkpointer router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/checkpointer
- Store router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/store
- Media router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/media
- Error handlers: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/exceptions/handle_errors.py
- Response helper: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/utils/response_helper.py
- Docs: https://10xgraph.com/
