# Agents and Tools

Use this when adding model behavior, tools, tool routing, provider options, retries, fallbacks, memory, skills, or structured output to an agent.

---

## Agent

`Agent` is a graph node that calls an LLM provider and appends the response to state. It supports OpenAI, Google GenAI, and any OpenAI-compatible API behind a unified interface.

### Constructor reference

```python
Agent(
    model: str,                                          # required
    output_type: str = "text",                           # "text" | "image" | "video" | "audio"
    system_prompt: list[dict] | None = None,
    tool_node: str | ToolNode | None = None,
    extra_messages: list[Message] | None = None,
    trim_context: bool = False,
    tools_tags: set[str] | None = None,
    reasoning_config: dict | bool | None = {"effort": "medium"},
    skills: SkillConfig | None = None,
    memory: MemoryConfig | None = None,
    retry_config: RetryConfig | bool | None = True,
    fallback_models: list[str | tuple[str, str]] | None = None,
    multimodal_config: MultimodalConfig | None = None,
    output_schema: type[BaseModel] | None = None,
    # extra kwargs forwarded to provider SDK:
    provider: str | None = None,       # "openai" | "google"; auto-detected if omitted
    base_url: str | None = None,
    api_style: str = "chat",           # "chat" | "responses" (OpenAI only)
    use_vertex_ai: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    # ...any other provider kwargs
)
```

### Model and provider

Provider is auto-detected from the model name. Three ways to set it:

```python
agent = Agent(model="gpt-4o")                     # auto → openai
agent = Agent(model="gemini-2.5-flash")            # auto → google
agent = Agent(model="openai/gpt-4o")              # prefix syntax
agent = Agent(model="gpt-4o", provider="openai")  # explicit kwarg
```

Third-party OpenAI-compatible APIs use `provider="openai"` + `base_url`:

```python
# Ollama (local)
Agent(model="llama3.2", provider="openai", base_url="http://localhost:11434/v1")

# DeepSeek
Agent(model="deepseek-chat", provider="openai", base_url="https://api.deepseek.com/v1")

# OpenRouter
Agent(model="anthropic/claude-3-5-sonnet", provider="openai", base_url="https://openrouter.ai/api/v1")
```

### System prompt

Pass a list of message dicts. `{field_name}` placeholders are replaced at runtime with values from `AgentState`:

```python
class MyState(AgentState):
    user_name: str = "Guest"
    language: str = "English"

agent = Agent(
    model="gpt-4o",
    system_prompt=[{"role": "system", "content": "Help {user_name}. Reply in {language}."}],
)
```

### Reasoning config

Reasoning is **on by default** at medium effort. Applies to both OpenAI and Google models.

```python
agent = Agent(model="gpt-4o")                                          # on, medium effort
agent = Agent(model="gpt-4o", reasoning_config={"effort": "high"})
agent = Agent(model="gpt-4o", reasoning_config=None)                   # off
agent = Agent(model="o4-mini", reasoning_config={"effort": "low", "summary": "auto"})  # OpenAI
agent = Agent(model="gemini-2.5-flash", reasoning_config={"thinking_budget": 5000})    # Google exact
```

Google effort → thinking_budget mapping:

| `effort` | `thinking_budget` |
|---|---|
| `"low"` | 512 |
| `"medium"` (default) | 8192 |
| `"high"` | 24576 |

### Retry config

Default: 3 retries, 1 s initial delay, 2x back-off, 30 s cap. Retries on HTTP 429, 500, 502, 503, 529.

```python
from tenxgraph.core.graph.agent_internal.constants import RetryConfig

agent = Agent(model="gpt-4o", retry_config=False)   # disable

agent = Agent(
    model="gpt-4o",
    retry_config=RetryConfig(max_retries=5, initial_delay=2.0, max_delay=60.0, backoff_factor=2.0),
)
```

`RetryConfig` fields: `max_retries` (3), `initial_delay` (1.0 s), `max_delay` (30.0 s), `backoff_factor` (2.0).

### Circuit breaker (opt-in)

Complements retry + `fallback_models`. Once a `(provider, model)` target fails
`circuit_breaker_threshold` times in a row, its circuit opens and that target is skipped (straight
to the next fallback) for `circuit_breaker_reset_timeout` seconds, instead of being retried on every
call. Configure on `RetryConfig`:

- `circuit_breaker_enabled: bool = False`
- `circuit_breaker_threshold: int = 5`
- `circuit_breaker_reset_timeout: float = 30.0`

```python
RetryConfig(circuit_breaker_enabled=True, circuit_breaker_threshold=3, circuit_breaker_reset_timeout=60.0)
```

### Fallback models

When the primary model exhausts all retries, 10xGraph tries each fallback in order:

```python
agent = Agent(
    model="gemini-2.5-flash",
    provider="google",
    fallback_models=[
        "gemini-2.0-flash",                 # inherit provider
        ("gpt-4o-mini", "openai"),          # explicit (model, provider) tuple
    ],
)
```

### LLM call timeout

All LLM clients apply a default request timeout (600 s) so a stalled provider cannot hang a run
indefinitely. Override globally via the `AGENTFLOW_LLM_TIMEOUT` env var (seconds), or
programmatically:

```python
from tenxgraph.core.llm import (
    set_default_llm_timeout, get_default_llm_timeout, DEFAULT_LLM_TIMEOUT_SECONDS,
)

set_default_llm_timeout(120.0)   # set
set_default_llm_timeout(None)    # reset to default
```

An explicit per-client `timeout=` still takes precedence.

### Structured output

`output_schema` with a Pydantic model forces JSON output. Requires `output_type="text"` (default).

```python
from pydantic import BaseModel

class ReviewAnalysis(BaseModel):
    sentiment: str
    score: float
    summary: str

agent = Agent(model="gpt-4o", output_schema=ReviewAnalysis)
```

### Extra messages

`extra_messages` are injected into every LLM call after the system prompt and before the context window. Use for few-shot examples or static instructions:

```python
agent = Agent(
    model="gpt-4o",
    extra_messages=[
        Message.text_message("Q: What is 2+2?", role="user"),
        Message.text_message("A: 4", role="assistant"),
    ],
)
```

### API style (OpenAI)

```python
agent = Agent(model="gpt-4o",  api_style="chat")       # default — Chat Completions
agent = Agent(model="o4-mini", api_style="responses")  # Responses API
```

---

## ToolNode

`ToolNode` registers and executes callable tools. It supports local Python functions, MCP tools, Composio integrations, and dynamically added tools.

```python
from tenxgraph.core.graph import ToolNode

tool_node = ToolNode([get_weather, safe_calculator])

# Add a tool after creation
tool_node.add_tool(search_db)
```

### Tool authoring rules

- Docstring → model-facing tool description. Required for useful behavior.
- Type annotations → parameter schema. Required for correct tool calls.
- Return a plain value for a normal tool result.
- Return `ToolResult` to update state fields and return a message to the model.
- Keep model-visible parameters separate from injected parameters.

### Injectable parameters

These are invisible to the model schema — `ToolNode` injects them automatically:

| Parameter | Type | What is injected |
|---|---|---|
| `state` | `AgentState \| None` | Current graph state |
| `tool_call_id` | `str \| None` | ID of this specific tool call |
| `config` | `dict` | Execution config (`thread_id`, `user_id`, `run_id`, …) |

```python
def get_weather(
    location: str,                         # from model
    state: AgentState | None = None,       # injected, hidden from schema
    config: dict | None = None,            # injected, hidden from schema
) -> str:
    """Get weather for a location."""
    user = config.get("user_id", "anon") if config else "anon"
    return f"Sunny in {location} (user: {user})"
```

### Returning state updates

Use `ToolResult` when a tool needs to update state fields and return a message:

```python
from tenxgraph.core.state.tool_result import ToolResult

class MyState(AgentState):
    selected_city: str = ""

def select_city(city: str) -> ToolResult:
    """Set the currently selected city."""
    return ToolResult(message=f"City set to '{city}'.", state={"selected_city": city})
```

### Filter tools by tag

`tools_tags` on `Agent` exposes only matching tools to the LLM:

```python
@tool(tags=["safe"])
def safe_search(query: str) -> str: ...

@tool(tags=["admin"])
def admin_action(cmd: str) -> str: ...

tool_node = ToolNode([safe_search, admin_action])
agent = Agent(model="gpt-4o", tool_node=tool_node, tools_tags={"safe"})
# admin_action is hidden from this agent's model schema
```

### MCP tools

```bash
pip install "10xgraph[mcp]"
```

```python
tool_node = ToolNode(
    [local_fn],
    client=mcp_client,               # fastmcp Client
    pass_user_info_to_mcp=True,      # send the signed-in user as request metadata
)
```

Server side, read the user from request metadata, never from a tool argument:

```python
from fastmcp import Context, FastMCP

mcp = FastMCP("support")

@mcp.tool
async def my_tickets(ctx: Context) -> list[dict]:
    """List the signed-in user's open tickets."""
    meta = ctx.request_context.meta
    if meta is not None and not isinstance(meta, dict):  # fastmcp 3.x: pydantic model
        meta = meta.model_dump()
    user = (meta or {}).get("user") or {}
    return await tickets_api.list_open(user_id=user["user_id"])
```

The server never takes `user_id` as a tool argument. Tool arguments are visible to and set by the model, so the model could forge them. Request metadata is set by 10xGraph, outside the model.

What `meta["user"]` holds:

- Behind the API, `config["user"]` is an `Identity`, sent as `Identity.model_dump(mode="json")`: `{"user_id": "alice", "kind": "user", "tenant": {"org_id": "acme"}, "roles": ["member"], "permissions": [...] or null, "reach": "own", "claims": {...}}`.
- With the SDK directly, a plain dict `config["user"]` is sent as is.
- With no user, it is `{"user_id": config["user_id"]}`.

The MCP server must authenticate the caller before it trusts this metadata. Send a service token from the client with `auth=`, for example `StreamableHttpTransport(url, auth="<token>")` (a string becomes a bearer token; an `httpx.Auth` object also works), and check it on the server.

---

## The `@tool` decorator

Attaches metadata to any function. Does not change injection behavior — enriches the model schema:

```python
from tenxgraph.utils import tool

@tool(
    name="web_search",
    description="Search the web for up-to-date information.",
    tags=["search", "web"],
    provider="custom",
    capabilities=["network_access"],
    metadata={"rate_limit": 100},
)
async def search_web(query: str, max_results: int = 5) -> list[str]:
    """Search the web."""
    ...
```

Without arguments: function name and docstring are used as defaults.

---

## ReAct loop

Standard routing pattern. The routing function inspects the last message:

```python
from tenxgraph.utils import END

def route(state: AgentState) -> str:
    if not state.context:
        return END
    last = state.context[-1]
    if hasattr(last, "tools_calls") and last.tools_calls and last.role == "assistant":
        return "TOOL"
    if last.role == "tool":
        return "MAIN"
    return END

graph.add_conditional_edges("MAIN", route, {"TOOL": "TOOL", END: END})
graph.add_edge("TOOL", "MAIN")
```

### Passing tool_node by name

Pass a string to share one `ToolNode` across multiple agents:

```python
agent = Agent(model="gpt-4o", tool_node="TOOL")
graph.add_node("MAIN", agent)
graph.add_node("TOOL", tool_node)
```

---

## Skills (Agent Skills specification)

`skills=SkillConfig(...)` loads skills that follow https://agentskills.io/specification: one
directory per skill with a `SKILL.md` (YAML frontmatter `name` + `description`, optional
`license`, `compatibility`, `allowed-tools`, `metadata`) and optional `scripts/`, `references/`,
`assets/`.

```python
from tenxgraph.core.skills import SkillConfig

agent = Agent(
    model="gpt-4o",
    tool_node=ToolNode([]),
    skills=SkillConfig(skills_dir=["./.agents/skills", "./shared-skills"]),
)
```

- On-demand mode (default) adds an `<available_skills>` catalog (name + description) to the
  system prompt and registers `activate_skill(skill_name)`. The result is the SKILL.md body in
  `<skill_content name="...">` tags plus a `<skill_resources>` file list.
- `read_skill_resource(skill_name, path)` is registered when any skill bundles files. It reads any
  file in the skill directory as text (markdown, `.py`, `.sh`, JSON, ...); nothing is executed.
- `mode="session", preload_from="FIELD"` injects the skill named by `state.FIELD` every call.
- Activated skills are recorded in `state.execution_meta.internal_data["active_skills"]` and are
  re-injected if context trimming drops their tool result.
- 10xGraph extensions live in `metadata` as strings: `triggers: "a; b"`, `tags: "x, y"`,
  `priority: "10"`.
- `SkillConfig` options: `skills_dir` (path or ordered list; earlier wins on name clashes),
  `inject_catalog`, `hot_reload`, `max_resource_bytes`, `include_skill_path`, `mode`,
  `preload_from`.
- Validate with `10xgraph skills --validate PATH` or `tenxgraph.core.skills.validate_skill`.

---

## Source map

- Agent: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/agent.py
- ToolNode: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/core/graph
- ToolResult: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/state/tool_result.py
- RetryConfig: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/agent_internal/constants.py
- `@tool` decorator: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/utils/decorators.py
- Skills: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/core/skills
- How-to (configure Agent): https://10xgraph.com/how-to/python/configure-agent
