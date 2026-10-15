# Memory and Store

Use this when adding long-term memory, store APIs, semantic search, memory tools, or memory retrieval modes.

## Concept

The memory store is cross-thread, cross-user long-term knowledge. It is not the same as checkpointing.

- Checkpointer: exact `thread_id` conversation continuity and full state snapshots.
- Memory store: semantic memory records, user preferences, facts, and knowledge across conversations.

## Access Pattern

Agents access memory through tools, especially `memory_tool`. The LLM can decide to:

- `store`: save or update a memory.
- `search`: recall relevant memories.
- `delete`: remove a memory by ID.

Writes can run in the background and memory keys deduplicate/update records.

## Retrieval Modes

- `"no_retrieval"`: model cannot read memory automatically but can write memories.
- `"preload"`: relevant memories are injected before the model call.
- `"postload"`: model searches memory on demand through the tool.

All modes can include write instructions.

## Backends

- `QdrantStore`: vector database backend, local or cloud.
- `Mem0Store`: managed memory service backend.

Embedding helpers such as `OpenAIEmbedding` and factories like `create_local_qdrant_store` exist in the store package where available.

## Wiring Options

High-level `Agent`:

- Pass `MemoryConfig(store=..., retrieval_mode=...)`.
- Also pass a `ToolNode`; memory setup registers memory tools there.

Lower-level graph:

- Use `MemoryIntegration`.
- Add `memory.tools` to your `ToolNode`.
- Use `memory.system_prompt`.
- Let `memory.wire(graph, entry_to="AGENT")` add preload routing when needed.

## REST and Client

Store API routes include:

- `POST /v1/store/memories`
- `POST /v1/store/search`
- `POST /v1/store/memories/{memory_id}`
- `POST /v1/store/memories/list`
- `PUT /v1/store/memories/{memory_id}`
- `DELETE /v1/store/memories/{memory_id}`
- `POST /v1/store/memories/forget`

TypeScript client methods include `storeMemory`, `searchMemory`, `getMemory`, `updateMemory`, `deleteMemory`, `listMemories`, and `forgetMemories`.

## Source Map

- Store interfaces/backends: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/storage/store
- Memory tool: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/prebuilt/tools/memory.py
- API store router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/store
- TS memory endpoints: https://github.com/10xGraph/10xgraph-client/tree/master/src/endpoints
