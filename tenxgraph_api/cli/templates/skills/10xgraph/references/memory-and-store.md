# Memory and Store

Use this when adding long-term memory, store APIs, semantic search, memory tools, or memory retrieval modes.

## Concept

The memory store is cross-thread, cross-user long-term knowledge. It is not the same as checkpointing.

- Checkpointer: exact `thread_id` conversation continuity and full state snapshots.
- Memory store: semantic memory records, user preferences, facts, and knowledge across conversations.

## Access Pattern

Memory is configured on the Agent: `Agent(..., memory=MemoryConfig(...))`. In `postload` mode the Agent registers two tools on its `ToolNode`:

- `user_memory_tool(action, text)`: `action="search"` (default) recalls user facts, `action="remember"` saves one. User memory is enabled by default. The model never passes memory ids.
- `agent_memory_tool(query)`: search only. Agent memory is off by default (`AgentMemoryConfig(enabled=True, agent_id=...)`), and the model cannot write it. Each agent's shared memory lives under its own reserved owner `system_user_id(agent_id)` (`"__system__:<agent_id>"`, or the `app_id` when no `agent_id` is set); a `RAGAgent` knowledge base defaults to `system_user_id("knowledge_base")`. The API refuses any caller whose `user_id` starts with `__system__`, so no request can read or write shared data as its owner.

Writes run in the background. A near-identical memory (similarity score 0.95 or higher) is skipped instead of stored twice.

## Retrieval Modes

`MemoryConfig(retrieval_mode=...)`, default `"postload"`:

- `"no_retrieval"`: no memory tools and nothing injected.
- `"preload"`: the Agent searches with the latest user message once per invocation (one invoke or stream run) and adds the results to the latest user message as a fenced `<user_memories_...>` block. Every model call in a tool loop reuses the same block. It is never saved to state, and the model gets no memory tools.
- `"postload"`: the model searches and remembers on demand through the tools above.

## Backends

- `QdrantStore`: vector database backend, local or cloud.
- `Mem0Store`: managed memory service backend.

Embedding helpers such as `OpenAIEmbedding` and factories like `create_local_qdrant_store` exist in the store package where available.

## Collections

Keep kinds of data apart (user, shared agent/system, company data) with Qdrant collections.

- A client can never choose a collection. The API drops `collection` from client config on every route (invoke, stream, `/v1/graph/ws`, AG-UI, store routes), ignored and logged at debug, like `user_id`, `user`, `authz`, `file_tool_root`.
- Precedence: 1) `UserMemoryConfig(collection=...)` / `AgentMemoryConfig(collection=...)` (a name or a function of the run config; always wins), 2) `collection` in a run config built by developer code, 3) `QdrantStore(collection=...)` default (a name or a function of the config; default `"agentflow_memories"`). With a function default, `asetup()` creates nothing; collections are created on first write or search. `RAGAgent`: `store_config={"collection": ...}` or `store_config_builder` returning `collection`.
- `/v1/store/...` routes always use the store default (and call it if it is a function, with the authenticated `user` in the config). Keep user memory in the default collection; put agent memory and RAG in named collections.
- Company data: either one shared collection with owner `system_user_id(f"company:{tenant_id}")`, or one collection per company: `UserMemoryConfig(collection=company_collection)` where the function reads `(cfg.get("user") or {}).get("tenant_id")` and raises if it is missing (with auth off there is no `user`). For RAG: `store_config_builder=lambda cfg: {"user_id": system_user_id(f"company:{t}"), "collection": f"company_{t}"}` with `t` read the same way. Read identity from `cfg["user"]` or `cfg["user_id"]`, never from state.

## Wiring

- Pass `MemoryConfig(store=..., retrieval_mode=...)` to `Agent`. Optional: `limit`, `score_threshold`, `max_tokens`, `inject_system_prompt`, `user_memory=UserMemoryConfig(...)`, `agent_memory=AgentMemoryConfig(...)`.
- In `postload` mode the Agent needs a `ToolNode` (or the name of a `ToolNode` node); memory setup registers the tools there. `preload` needs no ToolNode for memory.
- Pass the store to `compile(store=...)` too.
- The graph-level API (`MemoryIntegration`, `create_memory_preload_node`, `get_memory_system_prompt`) and the `memory_tool` function were removed. Do not use them.

## REST and Client

Store API routes include:

- `POST /v1/store/memories`
- `POST /v1/store/search`
- `POST /v1/store/memories/{memory_id}`
- `POST /v1/store/memories/list`
- `PUT /v1/store/memories/{memory_id}`
- `DELETE /v1/store/memories/{memory_id}`
- `POST /v1/store/memories/forget`

Forget deletes only what matches `memory_type`, `category` and `filters` (all combined). `Mem0Store` cannot narrow and rejects those fields. `query` is capped at 8,000 characters and `content` at 100,000 (`422` above). Client `options` accept only `infer` and `replace_metadata`.

TypeScript client methods include `storeMemory`, `searchMemory`, `getMemory`, `updateMemory`, `deleteMemory`, `listMemories`, and `forgetMemories`.

## Source Map

- Store interfaces/backends: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/storage/store
- Memory tools: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/prebuilt/tools/memory.py
- API store router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/store
- TS memory endpoints: https://github.com/10xGraph/10xgraph-client/tree/master/src/endpoints
