# Streaming

Use this when working on incremental output, SSE, cancellation, response granularity, or TypeScript async iteration.

## Modes

- `invoke`: run to completion and return a final result dict.
- `ainvoke`: async invoke.
- `stream`: sync generator yielding `StreamChunk`.
- `astream`: async generator yielding `StreamChunk`.

Use streaming for chat UIs and any caller that needs partial output.

## StreamChunk

Important fields:

- `event`: `"message"`, `"state"`, `"error"`, or `"updates"`.
- `message`: populated for message events.
- `state`: populated for state events.
- `data`: populated for errors/updates.
- `thread_id`, `run_id`, `metadata`, `timestamp`.

## ResponseGranularity

- `LOW`: latest messages only; good default for UI streaming.
- `PARTIAL`: context, summary, and latest messages.
- `FULL`: complete state and messages.

Use lower granularity for client performance and privacy unless full state is needed.

## Stop

Use `app.stop()` / `app.astop()` or `POST /v1/graph/stop` to request cancellation for a thread. The graph exits cleanly after node boundaries.

## REST and Client

REST:

- `POST /v1/graph/stream` returns server-sent events.
- Each SSE data payload is a serialized `StreamChunk`.

TypeScript:

- `TenxGraphClient.stream(messages, options)` returns an async generator.
- Handle `chunk.event === "message"` for incremental text.

## Source Map

- Stream chunks: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/state/stream_chunks.py
- Compiled graph streaming: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/compiled_graph.py
- Graph API router/service: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/graph
- TS stream endpoint: https://github.com/10xGraph/10xgraph-client/blob/master/src/endpoints/stream.ts
