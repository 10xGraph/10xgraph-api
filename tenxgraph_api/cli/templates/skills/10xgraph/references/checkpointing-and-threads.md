# Checkpointing and Threads

Use this when adding persistence, thread APIs, message history, interrupts, or multi-worker behavior.

## Concept

A checkpointer persists per-thread `AgentState`, messages, and thread metadata. Without a checkpointer, each `invoke` starts from a fresh state. With a checkpointer, calls with the same `config.thread_id` resume the saved state.

## Config

At minimum, checkpointed calls need:

```python
config = {"thread_id": "conv-1"}
```

Use `user_id` for multi-tenant scoping where the backend supports it.

## Backends

- `InMemoryCheckpointer`: development/tests/single-process only. State is lost on restart and not shared across workers.
- `PgCheckpointer`: production backend using PostgreSQL for durable data and Redis for fast cache. Requires setup before use.

Use production storage for load-balanced API deployments.

## API Surface

Checkpointer methods exist in async and sync forms:

- State: put/get/clear state and state cache.
- Messages: append/list/get/delete thread messages.
- Threads: create/get/list/clean thread metadata and content.
- Generic cache: namespaced key-value cache with optional TTL.

## REST and Client

The API exposes thread state/message/thread operations under `/v1/threads...` routes in the checkpointer router. The TypeScript client wraps these with methods such as:

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

## Rules

- Always preserve `thread_id` across a conversation.
- Use `PgCheckpointer` or equivalent durable storage for multi-worker production.
- Store conversation continuity in the checkpointer, not the memory store.
- Use `aclean_thread`/delete APIs when removing a thread so state, messages, and metadata stay consistent.

## Source Map

- Base API: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/storage/checkpointer/base_checkpointer.py
- In-memory backend: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/storage/checkpointer/in_memory_checkpointer.py
- Postgres backend: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/storage/checkpointer/pg_checkpointer.py
- API router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/checkpointer
- TS endpoints: https://github.com/10xGraph/10xgraph-client/tree/master/src/endpoints
