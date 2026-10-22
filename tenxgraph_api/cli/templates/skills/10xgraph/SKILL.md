---
name: 10xgraph
description: >-
  Expert guidance for building, debugging, and extending applications with 10xGraph
  (10xgraph). Use when code imports from tenxgraph or the legacy agentflow name (e.g. `from tenxgraph import`,
  `StateGraph`, `Agent`, `ToolNode`, `AgentState`); when the user references `10xgraph.json`
  or CLI commands (`10xgraph init`, `10xgraph dev`, `10xgraph build`,
  `10xgraph skills`); or when the user is building graph-based multi-agent workflows, tools,
  memory, checkpointing, or streaming with this framework. Skip generic Python or multi-agent
  questions that do not reference 10xGraph, and other frameworks (LangGraph, CrewAI, AutoGen)
  unless comparing.
license: MIT
---

# 10xGraph Project Skill

Use this skill when working in a 10xGraph project. 10xGraph is a multi-agent framework that wraps official OpenAI and Google SDK capabilities behind a unified graph, agent, tool, state, storage, API, CLI, and TypeScript client interface.

Treat https://10xgraph.com/ as the first source of truth for public package names, install commands, and user-facing behavior. Use implementation source after the docs establish the intended API.

## Workflow

1. Identify the published package or docs surface involved:
   - PyPI core Python SDK: `10xgraph` (`pip install 10xgraph`), source at https://github.com/10xGraph/10xGraph/tree/main/tenxgraph
   - PyPI API/CLI SDK: `10xgraph-api` (`pip install 10xgraph-api`), source at https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api
   - npm TypeScript SDK: `10xgraph-client` (`npm install 10xgraph-client`), source at https://github.com/10xGraph/10xgraph-client/tree/master/src
   - Main docs: https://10xgraph.com/
   - Playground/UI: `10xgraph dev` command after installed cli

2. Read the matching reference file before changing behavior. Paths are relative to this skill's directory:

   ### Core Python SDK
   - Architecture and package flow: `references/architecture.md`
   - Agent constructor, provider, reasoning, retry, fallback, output_schema: `references/agents-and-tools.md`
   - Graph construction, nodes, edges, compile, interrupts, config keys: `references/state-graph.md`
   - State, messages, and content blocks: `references/state-and-messages.md`
   - Threads and checkpointing: `references/checkpointing-and-threads.md`
   - Dependency injection (InjectQ): `references/dependency-injection.md`
   - Multimodal files and media stores: `references/media-and-files.md`
   - Long-term memory stores (MemoryConfig, QdrantStore, Mem0Store): `references/memory-and-store.md`
   - Streaming, StreamChunk, SSE, ResponseGranularity: `references/streaming.md`
   - Stream emitter for tool progress updates: `references/stream-emitter.md`
   - Observability hooks, validators, and runtime jumps: `references/callbacks-and-command.md`
   - Prebuilt agents (ReactAgent, PlanActReflectAgent, StructuredOutputAgent, SupervisorTeamAgent, SwarmAgent, RAGAgent) and tools: `references/prebuilt-agents-and-tools.md`
   - Event publishers and A2A/ACP runtime protocols: `references/publishers-and-runtime-protocols.md`
   - Context management, ID generation, and background tasks: `references/context-id-background.md`
   - Provider internals and adapters: `references/providers-and-adapters.md`
   - Prompt-injection and validation safety: `references/security-and-validators.md`
   - Realtime audio-to-audio voice agents (AudioAgent, Gemini Live, `arealtime`, WebSocket bridge): `references/realtime.md`

   ### API/CLI SDK
   - CLI commands and generated project files: `references/cli-commands.md`
   - `10xgraph.json` and dependency loading: `references/api-configuration.md`
   - API auth and authorization: `references/auth-and-authorization.md`
   - API environment, settings, and middleware: `references/api-settings-and-middleware.md`
   - Rate limiting (config, backends, headers, custom backend): `references/rate-limiting.md`
   - REST routes and error behavior: `references/rest-api-and-errors.md`
   - API Snowflake IDs and thread naming: `references/id-and-thread-name-generators.md`
   - API server and deployment runtime: `references/production-runtime.md`

   ### TypeScript client SDK
   - REST and TypeScript client surface: `references/api-client.md`
   - Browser/client-side tool execution: `references/remote-tools.md`
   - TypeScript auth helpers and structured errors: `references/client-auth-and-errors.md`
   - TypeScript messages, invoke, and stream details: `references/client-messages-invoke-stream.md`
   - TypeScript thread, memory, and file APIs: `references/client-threads-memory-files.md`

   ### Testing and QA
   - Unit testing without LLM calls (TestAgent, QuickTest, MockToolRegistry, `10xgraph test`): `references/unit-testing.md`
   - Evaluation framework (EvalSet, criteria, AgentEvaluator, QuickEval, UserSimulator, `10xgraph eval`): `references/evaluation.md`
   - Testing helpers overview: `references/testing-and-evaluation.md`

3. Prefer existing 10xGraph abstractions over new custom wiring:
   - Build workflows with `StateGraph`, `Agent`, `ToolNode`, `AgentState`, and `Message`.
   - Use prebuilt agents (`ReactAgent`, `PlanActReflectAgent`, `StructuredOutputAgent`, `SupervisorTeamAgent`, `SwarmAgent`, `RAGAgent`) for common patterns before hand-writing graph loops.
   - Persist conversation state with checkpointers; use stores only for cross-thread memory.
   - Put business services in `InjectQ` instead of global variables.
   - Keep API/CLI graph modules storage-agnostic and wire dependencies through `10xgraph.json`.

4. Verify against source when implementation details matter. Public names and expected behavior should match https://10xgraph.com/; source under https://github.com/10xGraph/10xGraph (core), https://github.com/10xGraph/10xgraph-api (API/CLI), and https://github.com/10xGraph/10xgraph-client (TypeScript) explains how that behavior is implemented.

## Local Conventions

- A compiled graph is normally loaded once by the API server and reused per request.
- Public package naming matters: use `10xgraph`, `10xgraph-api`, and `10xgraph-client` in user-facing docs and examples, not repository folder names.
- Every persisted interaction should include `config.thread_id`.
- Tools need docstrings and type annotations so model-facing schemas are useful.
- Injectable tool and node parameters (`state`, `config`, `tool_call_id`) are hidden from the model schema.
- For production, avoid process-local storage for shared state; use durable checkpointer/store backends.
- Add observability or audit side effects by registering a `GraphLifecycleHook` on `CallbackManager` — do not wrap `ainvoke()` / `astream()` calls in application code to achieve the same result.
- `reasoning_config` is on by default at medium effort; disable explicitly with `reasoning_config=None` when not needed.
- Provider is auto-detected from the model name; use `base_url` for third-party OpenAI-compatible APIs (Ollama, DeepSeek, OpenRouter).
