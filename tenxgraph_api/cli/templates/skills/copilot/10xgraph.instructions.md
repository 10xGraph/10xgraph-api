---
applyTo: "**"
---

# 10xGraph project instructions

This repo uses **10xGraph** — a multi-agent framework that wraps the official OpenAI and Google SDKs behind a unified graph, agent, tool, state, storage, API, CLI, and TypeScript-client interface.

When generating, refactoring, or debugging code in this repo, prefer 10xGraph's own abstractions over hand-rolled equivalents.

## Public package names (use these in user-facing examples)

- Python core SDK: `10xgraph` — `pip install 10xgraph` — source under `tenxgraph/` in https://github.com/10xGraph/10xGraph
- Python API/CLI SDK: `10xgraph-api` — `pip install 10xgraph-api` — source under `tenxgraph_api/` in https://github.com/10xGraph/10xgraph-api
- TypeScript SDK: `10xgraph-client` — `npm install 10xgraph-client` — source at https://github.com/10xGraph/10xgraph-client/tree/master/src
- Docs: https://10xgraph.com/ (source of truth for public API names)
- Playground: `10xgraph dev` (after the CLI is installed)

Never use repository folder names (e.g. `10xgraph-api`) in install commands or user-facing docs — use the published package names above.

## Architecture overview

Three published packages, one request flow:

1. `10xgraph-client` or another HTTP caller sends messages with `config.thread_id`.
2. FastAPI (`10xgraph-api`) receives the request through auth and routers.
3. `GraphService` invokes or streams against the compiled graph loaded at startup.
4. The compiled graph loads state through the checkpointer when a `thread_id` exists.
5. Graph nodes run and update `AgentState`.
6. Checkpointer saves state, messages, and thread metadata.
7. API returns JSON or SSE chunks to the caller.

Compile graphs once at startup. Keep graph code storage-agnostic; wire dependencies through compile arguments, `InjectQ`, or `10xgraph.json`.

## Core abstractions to reach for

- Build workflows with `StateGraph`, `Agent`, `ToolNode`, `AgentState`, and `Message`.
- **Use prebuilt agents for common patterns before hand-writing graph loops:**
  - `ReactAgent` — standard tool-calling loop (use by default)
  - `PlanActReflectAgent` — plan → act → reflect loop for complex multi-step tasks
  - `StructuredOutputAgent` — forces JSON output matching a Pydantic schema
  - `SupervisorTeamAgent` — central supervisor routes to specialist worker agents
  - `SwarmAgent` — peer-to-peer agent handoff without a central supervisor
  - `RAGAgent` — retrieves documents from a vector store before each LLM call
  - `AudioAgent` — realtime full-duplex voice agent (Gemini Live); needs the `[realtime]` extra and is driven by `arealtime()`, not `invoke`/`stream`
- Persist conversation state with **checkpointers**. Use **stores** only for cross-thread memory.
- Inject business services through **`InjectQ`**, not module-level globals.
- Keep API/CLI graph modules storage-agnostic; wire dependencies via `10xgraph.json`.
- Every persisted interaction must include `config.thread_id`.
- Tools need docstrings and type annotations so model-facing schemas are useful.
- Injectable parameters (`state`, `config`, `tool_call_id`) are hidden from the model schema.
- For production, avoid process-local storage for shared state — use durable checkpointer/store backends.
- Add observability, audit, or business-logic side effects by registering a `GraphLifecycleHook` on `CallbackManager` — do not wrap `ainvoke()` / `astream()` calls in application code to achieve the same result.

## Agent configuration quick reference

```python
Agent(
    model="gpt-4o",                             # required; provider auto-detected
    provider="openai",                          # explicit or omit for auto-detect
    base_url="http://localhost:11434/v1",       # for Ollama / DeepSeek / OpenRouter
    output_type="text",                         # "text" | "image" | "video" | "audio"
    output_schema=MyPydanticModel,              # force structured JSON output
    system_prompt=[{"role": "system", "content": "Help {user_name}."}],  # state interpolation
    tool_node=tool_node,                        # ToolNode instance or graph node name string
    tools_tags={"search"},                      # expose only matching-tagged tools
    extra_messages=[...],                       # injected after system_prompt every call
    trim_context=True,                          # trim old messages to fit token limits
    reasoning_config={"effort": "medium"},      # on by default; None to disable
    retry_config=True,                          # default 3 retries with backoff
    fallback_models=["gpt-4o-mini"],            # tried in order after retries exhausted
    memory=MemoryConfig(...),                   # long-term memory store wiring
    skills=SkillConfig(...),                    # skills discovery and exposure
    api_style="chat",                           # "chat" | "responses" (OpenAI only)
)
```

## Key configuration (`10xgraph.json`)

```json
{
  "agent": "graph.agent:app",
  "env": ".env",
  "auth": null,
  "checkpointer": null,
  "injectq": null,
  "store": null,
  "cache_store": {"type": "memory"},
  "thread_name_generator": null
}
```

Set `"auth": {"method": "jwt"}` and `JWT_SECRET_KEY` in `.env` to enable JWT authentication.

## Important conventions

- A compiled graph is loaded once at API startup and reused per request.
- `thread_id` is the continuity key for conversation state — always pass it for persisted interactions.
- Auth and request permissions belong in API middleware/routers, not inside graph nodes.
- Long-term memory store records are cross-thread knowledge, not thread history.
- The cache store, rate limiting and token revocation live in one `10xgraph.json` object: `"cache_store": {"type": "memory" | "redis" | "custom", "url": "${REDIS_URL}", "rate_limit": {...}, "token_revocation": true}`.
- `reasoning_config` is on by default — disable explicitly with `reasoning_config=None` when not needed.

## Verifying behavior

Public names and behavior should match https://10xgraph.com/. Implementation in the `10xgraph`, `10xgraph-api` and `10xgraph-client` repos shows *how* — only consult source after docs establish *what*.

That file points Copilot at the installed skill bundle under `.github/skills/10xgraph/`.
