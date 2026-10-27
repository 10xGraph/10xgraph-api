# Architecture

Use this when deciding where a change belongs or explaining how 10xGraph packages interact. Check https://10xgraph.com/ first for public package names.

---

## Published packages

| Public package | Registry | Install | Source |
|---|---|---|---|
| `10xgraph` | PyPI | `pip install 10xgraph` | `tenxgraph/` in 10xGraph/10xGraph |
| `10xgraph-api` | PyPI | `pip install 10xgraph-api` | `10xgraph-api/tenxgraph_api` |
| `10xgraph-client` | npm | `npm install 10xgraph-client` | `src/` in 10xGraph/10xgraph-client |

---

## Layer responsibilities

### `10xgraph` — core Python library

| Sub-package | Key exports |
|---|---|
| `tenxgraph.core` | `StateGraph`, `Agent`, `ToolNode`, `AgentState`, `Message`, `StreamChunk` |
| `tenxgraph.prebuilt.agent` | `ReactAgent`, `PlanActReflectAgent`, `StructuredOutputAgent`, `SupervisorTeamAgent`, `SwarmAgent`, `RAGAgent`, `AudioAgent` (realtime voice) |
| `tenxgraph.core.realtime` | `LiveInputQueue`, `RealtimeConfig`, `RealtimeEvent` (Gemini Live audio-to-audio) |
| `tenxgraph.prebuilt.tools` | `fetch_url`, `safe_calculator`, `file_read`, `file_write`, `file_search`, `google_web_search`, `vertex_ai_search`, `make_user_memory_tool`, `make_agent_memory_tool`, `create_handoff_tool` |
| `tenxgraph.storage.checkpointer` | `InMemoryCheckpointer`, `PgCheckpointer` |
| `tenxgraph.storage.store` | `QdrantStore`, `Mem0Store` |
| `tenxgraph.storage.media` | `InMemoryMediaStore`, `LocalFileMediaStore`, `CloudMediaStore` |
| `tenxgraph.runtime` | Publisher adapters (SSE, A2A, Kafka, RabbitMQ, Redis Pub/Sub) |
| `tenxgraph.utils` | `ResponseGranularity`, `CallbackManager`, `tool` decorator |
| `tenxgraph.qa` | Testing helpers and evaluation tools |

### `10xgraph-api` — API and CLI

Owns `10xgraph dev`, `10xgraph init`, `10xgraph build`, REST routers, auth/middleware, config loading, and graph service execution.

### `10xgraph-client` — TypeScript HTTP client

Typed methods for invoke, stream, threads, memory store, file uploads, graph metadata, remote tools, and auth helpers. Calls a running 10xGraph API server — does not run Python graphs.

---

## Request flow: invoke

1. `10xgraph-client` or another HTTP caller sends messages plus `config.thread_id`.
2. FastAPI receives the request through auth and routers.
3. `GraphService` invokes against the compiled graph loaded at startup.
4. The compiled graph loads state from the checkpointer when `thread_id` exists.
5. Graph nodes run and update `AgentState`.
6. Checkpointer saves state, messages, and thread metadata.
7. API returns JSON to the caller.

## Request flow: stream

Identical through authentication and state loading. The difference: the graph sends `StreamChunk` events incrementally via SSE. Each chunk carries `event` (`"message"`, `"state"`, `"error"`, or `"updates"`), and the response is a `StreamingResponse`.

---

## Key design decisions

| Decision | Rationale |
|---|---|
| Graph compiled once at startup | Avoids repeated module loading per request |
| `thread_id` in every request | Allows stateless servers to restore conversation history |
| Checkpointer is injected, not hardcoded | Graph code does not depend on the storage backend |
| Auth is middleware, not in the graph | Business logic stays separate from access control |
| `injectq` for service wiring | Nodes and tools declare dependencies declaratively; the runtime resolves them |

---

## Design rules

- Compile graphs once at startup for API serving.
- Keep graph code storage-agnostic; wire checkpointer/store/media/dependencies through compile arguments, `InjectQ`, or `10xgraph.json`.
- Treat `thread_id` as the continuity key for conversation state.
- Treat long-term memory store records as cross-thread knowledge, not thread history.
- Keep auth and request permissions in API middleware/routers, not inside graph nodes.

---

## Source map

- Core graph: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/core/graph
- State/message models: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/core/state
- Checkpointers: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/storage/checkpointer
- Memory stores: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/storage/store
- Media stores: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/storage/media
- API routers: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers
- TS client: https://github.com/10xGraph/10xgraph-client/blob/master/src/client.ts
- Concepts: https://10xgraph.com/concepts/architecture
