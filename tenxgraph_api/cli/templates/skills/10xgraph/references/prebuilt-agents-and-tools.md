# Prebuilt Agents and Tools

Use this when a task asks for ready-made agent patterns, common tools, or multi-agent handoff.

## Public surface

Import from `tenxgraph.prebuilt`, `tenxgraph.prebuilt.agent`, or `tenxgraph.prebuilt.tools`.

```python
from tenxgraph.prebuilt.agent import (
    ReactAgent,
    PlanActReflectAgent,
    StructuredOutputAgent,
    SupervisorTeamAgent,
    SwarmAgent,
    RAGAgent,
    AudioAgent,
)
```

All prebuilt agents expose the same `compile()` interface as a raw `StateGraph`. You get a `CompiledGraph` you can `invoke()` or `astream()`.

---

## Prebuilt agents

### ReactAgent

Standard tool-calling loop. The LLM calls tools in a loop until it has enough information to answer. Use this for most tasks.

```python
from tenxgraph.prebuilt.agent import ReactAgent
from tenxgraph.prebuilt.tools import fetch_url, safe_calculator

agent = ReactAgent(
    model="gpt-4o",
    tools=[fetch_url, safe_calculator],
    system_prompt=[{"role": "system", "content": "You are a research assistant."}],
)
app = agent.compile()
result = app.invoke(
    {"messages": [Message.text_message("What is 1234 * 5678?")]},
    config={"thread_id": "react-1"},
)
```

Full constructor:

```python
ReactAgent(
    model: str,
    state: StateT | None = None,
    context_manager: BaseContextManager | None = None,
    publisher: BasePublisher | None = None,
    id_generator: BaseIDGenerator = DefaultIDGenerator(),
    container: InjectQ | None = None,
    *,
    output_type: str = "text",
    system_prompt: list[dict] | None = None,
    tools: Iterable[Callable] | None = None,
    client: Any = None,                    # FastMCP client for MCP tools
    pass_user_info_to_mcp: bool = False,
    extra_messages: list[Message] | None = None,
    trim_context: bool = False,
    tools_tags: set[str] | None = None,
    reasoning_config: dict | bool | None = True,
    skills: SkillConfig | None = None,
    memory: MemoryConfig | None = None,
    retry_config: RetryConfig | bool = True,
    fallback_models: list[str | tuple[str, str]] | None = None,
    multimodal_config: MultimodalConfig | None = None,
    output_schema: type[BaseModel] | None = None,
    main_node_name: str = "MAIN",
    tool_node_name: str = "TOOL",
    **agent_kwargs,
)
```

`ReactAgent.compile()` accepts: `checkpointer`, `store`, `interrupt_before`, `interrupt_after`, `callback_manager`, `media_store`, `shutdown_timeout`.

### ReactAgent with MCP

```python
from fastmcp import Client

agent = ReactAgent(
    model="gpt-4o",
    tools=[],
    client=Client("path/to/mcp/server"),
    pass_user_info_to_mcp=True,
)
```

---

### PlanActReflectAgent

Breaks complex tasks into a Plan → Act → Reflect loop. The planner creates a step-by-step plan; the actor executes each step using tools; the reflector evaluates success and decides whether to replan. Good for multi-step research or tasks requiring self-correction.

```python
from tenxgraph.prebuilt.agent import PlanActReflectAgent
from tenxgraph.prebuilt.tools import fetch_url, google_web_search

agent = PlanActReflectAgent(
    model="gpt-4o",
    tools=[fetch_url, google_web_search],
    system_prompt=[{"role": "system", "content": "You are a thorough research agent."}],
)
app = agent.compile()
```

---

### StructuredOutputAgent

Forces the response to be a JSON object matching a Pydantic schema. Use for data extraction, classification, and form filling.

```python
from pydantic import BaseModel
from tenxgraph.prebuilt.agent import StructuredOutputAgent

class ProductReview(BaseModel):
    sentiment: str
    score: float
    summary: str
    key_points: list[str]

agent = StructuredOutputAgent(
    model="gpt-4o",
    output_schema=ProductReview,
    system_prompt=[{"role": "system", "content": "Extract structured product review data."}],
)
app = agent.compile()
result = app.invoke(
    {"messages": [Message.text_message("This laptop is amazing! 5 stars.")]},
    config={"thread_id": "struct-1"},
)
# result["messages"][-1].content is a JSON string conforming to ProductReview
```

---

### SupervisorTeamAgent

A central supervisor LLM routes tasks to specialist worker agents. Each worker is a full `ReactAgent` with its own model and tools.

```python
from tenxgraph.prebuilt.agent import SupervisorTeamAgent, WorkerConfig

agent = SupervisorTeamAgent(
    model="gpt-4o",
    workers=[
        WorkerConfig(
            name="researcher",
            model="gpt-4o-mini",
            tools=[fetch_url, google_web_search],
            description="Searches the web and fetches URLs.",
        ),
        WorkerConfig(
            name="analyst",
            model="gpt-4o",
            tools=[safe_calculator],
            description="Performs calculations and data analysis.",
        ),
    ],
    system_prompt=[{"role": "system", "content": "Delegate tasks to the right specialist."}],
)
```

`WorkerConfig` fields:

```python
WorkerConfig(
    name: str,                          # worker node name
    model: str,
    tools: list[Callable] = [],
    description: str = "",              # shown to supervisor to aid routing
    system_prompt: list[dict] | None = None,
    **agent_kwargs,
)
```

---

### SwarmAgent

Agents hand off directly to each other — no central supervisor. Each member decides who handles the task next via `create_handoff_tool`.

```python
from tenxgraph.prebuilt.agent import SwarmAgent, SwarmMemberConfig

agent = SwarmAgent(
    members=[
        SwarmMemberConfig(name="triage",   model="gpt-4o-mini", description="Classifies and routes requests."),
        SwarmMemberConfig(name="billing",  model="gpt-4o",      description="Handles billing questions."),
        SwarmMemberConfig(name="technical",model="gpt-4o",      tools=[fetch_url], description="Handles tech support."),
    ],
    entry_member="triage",
)
app = agent.compile()
```

`SwarmMemberConfig` fields:

```python
SwarmMemberConfig(
    name: str,
    model: str,
    description: str = "",
    tools: list[Callable] = [],
    system_prompt: list[dict] | None = None,
    **agent_kwargs,
)
```

---

### RAGAgent

Retrieves relevant documents from a vector store before each LLM call and injects them into context. Supports rerankers.

```python
from tenxgraph.prebuilt.agent import RAGAgent
from tenxgraph.storage.store import create_local_qdrant_store, OpenAIEmbedding

store = create_local_qdrant_store("./qdrant_data", OpenAIEmbedding())

agent = RAGAgent(
    model="gpt-4o",
    store=store,
    system_prompt=[{"role": "system", "content": "Answer using the provided document context."}],
)
app = agent.compile()
```

With reranker:

```python
from tenxgraph.prebuilt.agent import RAGAgent, CohereReranker

agent = RAGAgent(model="gpt-4o", store=store, reranker=CohereReranker(api_key="..."))
```

### AudioAgent

Realtime, full-duplex voice agent (Gemini Live). React-style builder that wraps a `LiveAgent` as the
graph root. Requires `pip install "10xgraph[realtime]"` and `GEMINI_API_KEY`. Driven by
`arealtime()` / `realtime()`, not `invoke` / `stream`.

```python
from tenxgraph.prebuilt.agent import AudioAgent
from tenxgraph.core.realtime import RealtimeConfig, LiveInputQueue

app = AudioAgent(
    "gemini-live-2.5-flash-preview",
    realtime_config=RealtimeConfig(model="gemini-live-2.5-flash-preview", voice="Puck"),
    tools=[my_tool],          # React-style tool calling, including barge-in
).compile()

queue = LiveInputQueue()
queue.send_audio(pcm16_bytes)
async for event in app.arealtime(queue, {"thread_id": "t1"}):
    ...
```

`compile()` takes `checkpointer`, `store`, `callback_manager`, `shutdown_timeout` only (no
`media_store` / `interrupt_*`). `system_prompt`, `skills`, and `memory` work like `ReactAgent`. See
`realtime.md` for the full surface (events, reconnection, lifecycle hooks, WebSocket bridge).

---

## Compile options (all prebuilt agents)

```python
app = agent.compile(
    checkpointer=None,
    store=None,
    interrupt_before=[],
    interrupt_after=[],
    callback_manager=CallbackManager(),
    media_store=None,
    shutdown_timeout=30.0,
)
```

---

## Prebuilt tools

Import from `tenxgraph.prebuilt.tools`:

| Tool | Description | Tags |
|---|---|---|
| `fetch_url` | Fetch text content from any public HTTP/HTTPS URL. Blocks private IPs. | `["web", "fetch", "network"]` |
| `safe_calculator` | Evaluate arithmetic expressions without exposing `__builtins__`. | `["math", "calculator"]` |
| `file_read` | Read a file within the working directory. | `["file", "read"]` |
| `file_write` | Write or append to a file within the working directory. | `["file", "write"]` |
| `file_search` | Search files by glob pattern. | `["file", "search"]` |
| `google_web_search` | Call Google Custom Search API. Needs `GOOGLE_API_KEY` + `GOOGLE_CSE_ID`. | `["search", "web", "google"]` |
| `vertex_ai_search` | Call Google Vertex AI Search. Needs GCP credentials + `VERTEX_AI_DATA_STORE_ID`. | `["search", "vertex", "google"]` |
| `make_user_memory_tool` | Factory for per-user memory tools (used by `MemoryConfig`). | — |
| `make_agent_memory_tool` | Factory for agent-scoped memory tools (used by `MemoryConfig`). | — |
| `create_handoff_tool` | Creates a tool that transfers control to a named agent node. | — |
| `is_handoff_tool` | Predicate to test whether a tool is a handoff tool. | — |

### fetch_url schema

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `url` | `str` | required | Public HTTP/HTTPS URL. |
| `timeout` | `float` | `10.0` | Max 30 s. |
| `max_chars` | `int` | `20000` | Response truncated to this length. |

Returns `{"url", "status_code", "content_type", "content", "truncated"}` as JSON string.

### file_read / file_write / file_search

All three enforce paths are within the working directory or an explicit allowed root.

`file_read`: `path`, `encoding` (default `"utf-8"`) → file content as string.
`file_write`: `path`, `content`, `mode` (`"w"` or `"a"`) → success/error message.
`file_search`: `pattern` (glob), `root` → JSON list of matching paths.

### google_web_search schema

`query` (str), `num_results` (int, default 5, max 10) → JSON list of `{"title", "url", "snippet"}`.

### Memory tools

The typical pattern is `Agent(..., memory=MemoryConfig(...))` — in `postload` mode this registers `user_memory_tool` and `agent_memory_tool` on the Agent's `ToolNode` automatically. Call `make_user_memory_tool` or `make_agent_memory_tool` yourself only when you need explicit control. There is no general-purpose `memory_tool`.

### create_handoff_tool

```python
from tenxgraph.prebuilt.tools import create_handoff_tool

handoff = create_handoff_tool(
    agent_name="billing",
    description="Transfer to billing agent for payment questions.",
)
tool_node = ToolNode([handoff])
```

### Filter tools by tag

```python
# Expose only search-tagged tools to this agent
agent = Agent(model="gpt-4o", tool_node=tool_node, tools_tags={"search"})
```

### Compose prebuilt and custom tools

```python
from tenxgraph.prebuilt.tools import fetch_url, safe_calculator
from tenxgraph.utils.decorators import tool

@tool(name="get_exchange_rate", tags=["finance"])
async def get_exchange_rate(from_currency: str, to_currency: str) -> str:
    """Get the current exchange rate between two currencies."""
    ...

tool_node = ToolNode([fetch_url, safe_calculator, get_exchange_rate])
```

---

## Handoff

`create_handoff_tool(target_agent_name)` creates a tool named `transfer_to_<agent_name>` that returns a graph navigation command. Use for agent-to-agent delegation in swarm and supervisor patterns.

Rules:
- Add handoff tools to the source agent's `ToolNode`.
- Mention available transfers in the agent's system prompt.
- Handoff target names must match graph node names.
- Use `Command(goto=...)` for explicit runtime routing when not using the handoff helper.

---

## Rules

- Prefer a prebuilt agent for common patterns before hand-writing a graph loop.
- Use `ReactAgent` by default; escalate to `PlanActReflectAgent` only when self-reflection is needed.
- `StructuredOutputAgent` requires `output_schema`; do not combine with `output_type != "text"`.
- `SupervisorTeamAgent` and `SwarmAgent` both compile into multi-node graphs — `SwarmAgent` adds handoff tools automatically.
- Treat exported `__all__` names as the stable public surface; do not reference internal modules.

## Source map

- Prebuilt agent exports: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/prebuilt/agent/__init__.py
- Prebuilt tool exports: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/prebuilt/tools/__init__.py
- Handoff tool: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/prebuilt/tools/handoff.py
- How-to (agents): https://10xgraph.com/how-to/python/use-prebuilt-agents
- How-to (tools): https://10xgraph.com/how-to/python/use-prebuilt-tools
