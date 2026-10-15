# Callbacks and Command

Use this when adding validation, tracing, invocation hooks, error recovery, or runtime graph jumps.

## CallbackManager

`CallbackManager` registers hooks around AI, tool, MCP, input validation, and skill invocations.

Invocation types:

- `InvocationType.AI`
- `InvocationType.TOOL`
- `InvocationType.MCP`
- `InvocationType.INPUT_VALIDATION`
- `InvocationType.SKILL`

Hook families:

- `register_before_invoke(invocation_type, callback)`: validate or transform input before invocation.
- `register_after_invoke(invocation_type, callback)`: inspect or transform output after invocation.
- `register_on_error(invocation_type, callback)`: log, recover, or return a recovery `Message`.
- `register_input_validator(validator)`: add a `BaseValidator` that validates incoming messages.

Callbacks can be callable objects or functions and may be sync or async. Error callbacks should return a `Message` recovery value or `None`.

## Validators

Use `BaseValidator` for input validation. Implement:

```python
async def validate(self, messages: list[Message]) -> bool:
    ...
```

Register validators on a `CallbackManager`, then pass it to graph compilation:

```python
callback_manager = CallbackManager()
callback_manager.register_input_validator(MyValidator())

app = graph.compile(callback_manager=callback_manager)
```

Use validators for safety checks, business rules, input policy, and prompt-injection detection.

## Graph Lifecycle Hooks

Graph lifecycle hooks fire at **graph orchestration level** — before/after the entire graph run, on checkpoints, on interrupts, on resume, and after each node transition. They complement the invocation-level hooks above (`before_invoke` / `after_invoke` / `on_error`), which fire inside a single node's AI/Tool/MCP call.

### GraphLifecycleContext

All 7 hooks receive this as their first argument. It provides run-identifying metadata.

```python
@dataclass
class GraphLifecycleContext:
    config: dict[str, Any]       # full config dict passed to invoke/stream
    timestamp: str               # ISO8601 start time from config["timestamp"]
    metadata: dict[str, Any] | None = None  # open-ended extra context

    @property
    def thread_id(self) -> str | None: ...

    @property
    def run_id(self) -> str | None: ...
```

### GraphLifecycleHook

Subclass `GraphLifecycleHook` and override only the methods you need. All methods are async and default to no-ops.

```python
from tenxgraph.utils.callbacks import GraphLifecycleHook, GraphLifecycleContext, CallbackManager
from tenxgraph.core.state import AgentState
from tenxgraph.core.state.message import Message

class GraphLifecycleHook(ABC):
    async def on_graph_start(
        self, context: GraphLifecycleContext, state: AgentState
    ) -> AgentState | None: ...
    # Fires: after state is loaded, before the first node executes.
    # Return modified AgentState to replace the initial state, or None.

    async def on_graph_end(
        self, context: GraphLifecycleContext, final_state: AgentState,
        messages: list[Message], total_steps: int
    ) -> AgentState | None: ...
    # Fires: after execution loop completes, before final persistence.
    # Return modified AgentState or None.

    async def on_graph_error(
        self, context: GraphLifecycleContext, error: Exception,
        partial_state: AgentState, messages: list[Message], step: int, node_name: str
    ) -> tuple[AgentState, str] | None: ...
    # Fires: when an unhandled exception escapes the graph loop.
    # Return (AgentState, error_message) to change persisted error snapshot, or None.
    # Cannot suppress the error — always re-raised after this hook.

    async def on_interrupt(
        self, context: GraphLifecycleContext, interrupted_node: str,
        interrupt_type: str,   # "before" | "after" | "stop" | "remote_tool"
        state: AgentState
    ) -> AgentState | None: ...
    # Fires: before interrupt state is persisted (covers before/after/stop/remote_tool types).
    # Return modified AgentState or None.

    async def on_resume(
        self, context: GraphLifecycleContext, resumed_node: str,
        state: AgentState, resume_data: dict[str, Any]
    ) -> AgentState | None: ...
    # Fires: when a paused graph is resumed, before clear_interrupt().
    # resume_data is mutable in-place. Return modified AgentState or None.

    async def on_checkpoint(
        self, context: GraphLifecycleContext, state: AgentState,
        messages: list[Message], is_context_trimmed: bool
    ) -> tuple[AgentState, list[Message]] | AgentState | None: ...
    # Fires: immediately before state/messages are persisted (every checkpoint, not just final).
    # Return (AgentState, messages), AgentState, or None.

    async def on_state_update(
        self, context: GraphLifecycleContext, node_name: str,
        old_state: AgentState, new_state: AgentState, step: int
    ) -> AgentState | None: ...
    # Fires: after each node produces a result and state is merged.
    # Return modified AgentState or None.
```

### Registration

```python
callback_mgr = CallbackManager()
callback_mgr.register_lifecycle_hook(MyHook())

# Combine with existing invocation-level hooks on the same manager:
callback_mgr.register_after_invoke(InvocationType.AI, my_ai_callback)

app = graph.compile(callback_manager=callback_mgr)
```

### Hook Summary

| Hook | Returns | Fires N times | Fire location |
|---|---|---|---|
| `on_graph_start` | `AgentState \| None` | 1 per run | After state load, before loop |
| `on_graph_end` | `AgentState \| None` | 1 per successful run | After `state.complete()`, before final `sync_data()` |
| `on_graph_error` | `tuple[AgentState, str] \| None` | 1 per failed run | In except block, before error `sync_data()` |
| `on_interrupt` | `AgentState \| None` | 0–N per run | Before interrupt checkpoint persistence |
| `on_resume` | `AgentState \| None` | 0–1 per call | Before `clear_interrupt()` |
| `on_checkpoint` | `(AgentState, list[Message]) \| AgentState \| None` | 1–N per run | Before every durable checkpoint write |
| `on_state_update` | `AgentState \| None` | N per run (once per node) | After each node result is merged |
| `on_turn_start` | `AgentState \| None` | N (realtime only) | Start of each model turn |
| `on_turn_end` | `AgentState \| None` | N (realtime only) | End of each model turn |

### Realtime hooks

The same `GraphLifecycleHook` fires for realtime (audio) graphs, with two methods that fire **only**
in realtime (no-ops for `invoke` / `stream`):

- `on_graph_start(ctx, state)` / `on_graph_end(ctx, final_state, messages, total_steps)` — once per
  session (the `LIVE` node *is* the graph); `total_steps` = number of turns.
- `on_turn_start(ctx, state, turn_index)` / `on_turn_end(ctx, state, turn_index)` — per model turn
  (1-based; a turn spans one model generation, bounded by `turn_complete` or a barge-in). A turn cut
  off by session end still gets a balanced `on_turn_end`.

Realtime has no `AI`-invocation callback or input-validator pass (no discrete LLM call); the per-turn
hooks are the observability stand-in. Tool/MCP `before/after/error` callbacks fire as usual. See
`realtime.md`.

### Example

```python
class ObservabilityHook(GraphLifecycleHook):
    async def on_graph_start(self, ctx, state):
        self._span = tracer.start_span(f"graph.run.{ctx.thread_id}")
        return None

    async def on_graph_end(self, ctx, final_state, messages, total_steps):
        self._span.set_attribute("steps", total_steps)
        self._span.end()

    async def on_graph_error(self, ctx, error, partial_state, messages, step, node_name):
        self._span.record_exception(error)
        self._span.end()
        alert_oncall(f"Graph failed at node {node_name}: {error}")
        return None

    async def on_interrupt(self, ctx, interrupted_node, interrupt_type, state):
        notify_frontend(ctx.thread_id, status="waiting_for_input", node=interrupted_node)
        return None

    async def on_resume(self, ctx, resumed_node, state, resume_data):
        notify_frontend(ctx.thread_id, status="resuming", node=resumed_node)
        return None

    async def on_checkpoint(self, ctx, state, messages, is_context_trimmed):
        metrics.increment("tenxgraph.checkpoints", tags={"thread": ctx.thread_id})
        return None

    async def on_state_update(self, ctx, node_name, old_state, new_state, step):
        diff = compute_diff(old_state, new_state)
        stream_diff_to_frontend(ctx.thread_id, diff)
        return None


callback_mgr = CallbackManager()
callback_mgr.register_lifecycle_hook(ObservabilityHook())
app = graph.compile(callback_manager=callback_mgr)
```

### Common Use Cases by Hook

- **`on_graph_start`**: inject trace IDs, pre-populate state from external DB, set rate-limit budgets, initialize OpenTelemetry spans.
- **`on_graph_end`**: send completion notifications (Slack/email), record step/message count metrics, archive transcripts, trigger downstream webhooks.
- **`on_graph_error`**: alert PagerDuty/Sentry, log structured failure diagnostics, close OTel spans with error status.
- **`on_interrupt`**: push "waiting for approval" notifications to frontend/mobile, start timeout timers, update task queue status.
- **`on_resume`**: cancel timeout timers, validate resume payload, record interrupt→resume cycle for audit trail.
- **`on_checkpoint`**: redact sensitive data before persistence, replicate to secondary store, invalidate caches, compliance audit logging (SOC2/HIPAA).
- **`on_state_update`**: real-time state diffing for frontend streaming, per-node invariant assertions, security scanning of state content.

---

## Command

`Command` lets a node combine state/message updates with control flow. Use it when the next node depends on runtime logic inside the node and is awkward to express as a static conditional edge.

Fields:

- `update`: state update, `Message`, string, converter, or `None`.
- `goto`: next node name or `END`.
- `graph`: optional graph target; `Command.PARENT` is reserved for parent graph navigation patterns.
- `state`: optional attached state.

Prefer conditional edges for normal routing because they are easier to visualize and test. Use `Command(goto=...)` for dynamic jumps, recovery branches, handoffs, or side-effect-dependent routing.

## Patterns

Callback for tracing:

```python
from tenxgraph.utils.callbacks import CallbackContext, CallbackManager, InvocationType

async def trace_after(context: CallbackContext, input_data, output_data):
    print(context.invocation_type, context.node_name)
    return output_data

callback_manager = CallbackManager()
callback_manager.register_after_invoke(InvocationType.TOOL, trace_after)
app = graph.compile(callback_manager=callback_manager)
```

Command for dynamic routing:

```python
from tenxgraph.utils import Command, END

def router_node(state, config):
    if state.context[-1].text() == "stop":
        return Command(goto=END)
    return Command(update={"route": "repair"}, goto="REPAIR")
```

## Rules

- Keep callback side effects bounded; they run inside graph execution paths.
- Avoid storing per-request mutable state globally inside callbacks.
- Use `CallbackContext` metadata to distinguish node/function/invocation details.
- Return transformed data from callbacks only when the downstream invocation expects that shape.
- Use `Command` sparingly; static and conditional edges remain the default graph structure.
- Always test command routes for recursion limits and missing destination nodes.

## Source Map

- Callback system (invocation hooks + graph lifecycle hooks): https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/utils/callbacks.py
- Default validators: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/utils/validators.py
- Graph compile callback argument: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/state_graph.py
- Command API: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/utils/command.py
- Command execution paths: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/compiled_graph.py
- Lifecycle hook fire points — invoke path: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/utils/invoke_handler.py
- Lifecycle hook fire points — stream path: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/utils/stream_handler.py
- Lifecycle hook fire points — interrupt/resume: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/utils/heandler_utils.py
- Lifecycle hook fire points — checkpoint: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/utils/utils.py
- Docs: https://10xgraph.com/docs/concepts/callbacks-and-command
