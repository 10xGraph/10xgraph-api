from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from tenxgraph.core.state import Message
from tenxgraph.utils import ResponseGranularity

from tenxgraph_api.src.app.core.auth.request_config import (
    client_message_error,
    ClientInputError,
    request_size_error,
    with_fresh_message_ids,
)


# Validation context key: keep the messages' own ids (the AG-UI adapter, whose converter
# dedups on them). Without it the server generates every id.
KEEP_MESSAGE_IDS = "keep_message_ids"


class GraphInputSchema(BaseModel):
    """
    Schema for graph input including messages and configuration.
    """

    messages: list[Message] = Field(
        default_factory=list,
        description=(
            "List of messages to process through the graph. Required unless `resume` is set."
        ),
    )
    resume: Any = Field(
        default=None,
        description=(
            "Answer for a thread paused by interrupt(); the paused node runs again and "
            "interrupt() returns this value. Send it, possibly as null, only to resume."
        ),
    )
    interrupt_id: str | None = Field(
        default=None,
        description=(
            "Id of the interrupt `resume` answers (`Interrupt.id`, from the run that paused). "
            "Optional. When sent, the thread must be paused at that interrupt, or the request "
            "is rejected with 409 before anything runs."
        ),
    )
    initial_state: dict[str, Any] | None = Field(
        default=None,
        description="Initial state for the graph execution",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description="Optional configuration for graph execution",
    )
    recursion_limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum recursion limit for graph execution",
    )
    on_busy: Literal["wait", "cancel"] = Field(
        default="wait",
        description=(
            "What to do when another run is active on the same thread. 'wait' (default) queues "
            "behind it for up to 30 seconds, then the request is rejected with 409 "
            "'thread is busy'. 'cancel' asks the active run to stop, then waits for it the "
            "same way and runs this request."
        ),
    )

    @field_validator("messages", mode="before")
    @classmethod
    def messages_within_limits(cls, v: Any) -> Any:
        # Before the messages are parsed into objects: an oversized request is refused cheaply.
        if error := request_size_error(v):
            raise ClientInputError(error)
        return v

    @field_validator("messages")
    @classmethod
    def client_messages_only(cls, v: list[Message], info: ValidationInfo) -> list[Message]:
        if error := client_message_error(v):
            raise ValueError(error)
        if (info.context or {}).get(KEEP_MESSAGE_IDS):
            return v
        return with_fresh_message_ids(v)

    @model_validator(mode="after")
    def messages_or_resume(self) -> "GraphInputSchema":
        if not self.messages and not self.is_resume:
            raise ValueError("messages must contain at least one message")
        return self

    @property
    def is_resume(self) -> bool:
        """Whether the request resumes an interrupt() (``resume`` was sent, even as null)."""
        return "resume" in self.model_fields_set

    response_granularity: ResponseGranularity = Field(
        default=ResponseGranularity.LOW,
        description="Granularity of the response (full, partial, low)",
    )


class GraphInvokeOutputSchema(BaseModel):
    """
    Schema for graph invoke output.
    """

    messages: list[Message] = Field(
        ...,
        description="Final processed messages from the graph",
    )
    state: dict[str, Any] | None = Field(
        default=None,
        description="State information from the graph execution",
    )
    context: list[Message] | None = Field(
        default=None,
        description="Context information from the graph execution",
    )
    summary: str | None = Field(
        default=None,
        description="Summary information from the graph execution",
    )
    meta: dict[str, Any] | None = Field(
        default=None,
        description="Meta information from the graph execution",
    )


# class GraphStreamChunkSchema(BaseModel):
#     """
#     Schema for individual stream chunks from graph execution.
#     """

#     data: dict[str, Any] = Field(..., description="Chunk data")
#     metadata: dict[str, Any] | None = Field(default=None, description="Chunk metadata")


class NodeSchema(BaseModel):
    """Schema for individual graph nodes."""

    id: str = Field(..., description="Unique identifier for the node")
    name: str = Field(..., description="Name of the node")


class EdgeSchema(BaseModel):
    """Schema for individual graph edges."""

    id: str = Field(..., description="Unique identifier for the edge")
    source: str = Field(..., description="Source node identifier")
    target: str = Field(..., description="Target node identifier")


class GraphInfoSchema(BaseModel):
    """Schema for graph metadata and configuration."""

    node_count: int = Field(..., description="Number of nodes in the graph")
    edge_count: int = Field(..., description="Number of edges in the graph")
    is_realtime: bool = Field(
        False,
        description="Whether the graph is a realtime/live agent (driven over /v1/graph/live)",
    )
    checkpointer: bool = Field(..., description="Whether checkpointer is enabled")
    checkpointer_type: str | None = Field(None, description="Type of checkpointer if enabled")
    publisher: bool = Field(..., description="Whether publisher is enabled")
    store: bool = Field(..., description="Whether store is enabled")
    interrupt_before: list[str] | None = Field(None, description="Nodes to interrupt before")
    interrupt_after: list[str] | None = Field(None, description="Nodes to interrupt after")
    context_type: str | None = Field(None, description="Type of context for the graph")
    id_generator: str | None = Field(None, description="ID generator type for the graph")
    id_type: str | None = Field(None, description="ID type for the graph")
    state_type: str | None = Field(None, description="State type for the graph")
    state_fields: list[str] | None = Field(None, description="State fields for the graph")


class GraphSchema(BaseModel):
    """Schema for the complete graph structure."""

    info: GraphInfoSchema = Field(..., description="Graph metadata and configuration")
    nodes: list[NodeSchema] = Field(..., description="List of nodes in the graph")
    edges: list[EdgeSchema] = Field(..., description="List of edges in the graph")


class ToolSchema(BaseModel):
    """Schema for a single tool exposed by a tool node."""

    name: str = Field(..., description="Name of the tool")
    description: str = Field("", description="Human-readable description of the tool")
    source: Literal["local", "mcp", "remote"] = Field(
        ...,
        description=(
            "Where the tool is defined: 'local' (a Python function on the node), "
            "'mcp' (provided by a connected MCP server), or 'remote' "
            "(client-side tool declared in 10xgraph.json)."
        ),
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema of the tool's parameters (OpenAI function-calling shape)",
    )


class ToolNodeSchema(BaseModel):
    """Schema for a tool node and the tools it exposes."""

    node_name: str = Field(..., description="Name of the tool node in the graph")
    tool_count: int = Field(..., description="Number of tools exposed by this node")
    tools: list[ToolSchema] = Field(default_factory=list, description="Tools exposed by this node")


class GraphToolsSchema(BaseModel):
    """Schema for all tool nodes and their tools across the graph."""

    node_count: int = Field(..., description="Number of tool nodes in the graph")
    tool_count: int = Field(..., description="Total number of tools across all tool nodes")
    nodes: list[ToolNodeSchema] = Field(
        default_factory=list, description="Tool nodes and their tools"
    )


class ObsSpanSchema(BaseModel):
    """A reconstructed span in the run trace."""

    id: str = Field(..., description="Span id (stable within the run)")
    name: str = Field(..., description="Display name, e.g. 'node: agent' or 'tool: get_weather'")
    kind: Literal["root", "node", "llm", "tool"] = Field(..., description="Span kind")
    parent: str | None = Field(None, description="Parent span id")
    start_ms: float = Field(..., description="Start offset from run start, in ms")
    duration_ms: float = Field(..., description="Duration in ms")
    model: str | None = Field(None, description="LLM model (llm spans)")
    input_tokens: int | None = Field(None, description="Prompt tokens (llm spans)")
    output_tokens: int | None = Field(None, description="Completion tokens (llm spans)")


class ObsEventSchema(BaseModel):
    """A reconstructed event in the run trace."""

    id: str = Field(..., description="Event id")
    type: str = Field(..., description="Event type: message | updates | state | error | result")
    node: str = Field("", description="Node the event is attributed to")
    offset_ms: float = Field(..., description="Offset from run start, in ms")
    summary: str = Field("", description="Human-readable one-line summary")


class ObsTokenUsageSchema(BaseModel):
    """Aggregated token usage for the run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


class ObsRunSchema(BaseModel):
    """A single reconstructed run trace."""

    run_id: str
    thread_id: str
    status: str = Field(..., description="running | done | error | stopped")
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: float = 0.0
    spans: list[ObsSpanSchema] = Field(default_factory=list)
    events: list[ObsEventSchema] = Field(default_factory=list)
    usage: ObsTokenUsageSchema = Field(default_factory=ObsTokenUsageSchema)
    llm_calls: int = 0
    tool_calls: int = 0
    iterations: int = 0


class ObservabilitySchema(BaseModel):
    """Observability payload for a thread: available runs + the selected run."""

    thread_id: str
    run_count: int = 0
    run_ids: list[str] = Field(default_factory=list)
    run: ObsRunSchema | None = Field(None, description="The requested/latest run, if any")


class GraphStopSchema(BaseModel):
    """Schema for stopping graph execution."""

    thread_id: str = Field(..., min_length=1, description="Thread ID to stop execution for")

    @field_validator("thread_id")
    @classmethod
    def thread_id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("thread_id cannot be empty or whitespace")
        return v.strip()

    config: dict[str, Any] | None = Field(
        default=None, description="Optional configuration for the stop operation"
    )


class FixGraphRequestSchema(BaseModel):
    """Schema for fixing graph state by removing messages with empty tool call content."""

    thread_id: str = Field(..., min_length=1, description="Thread ID to fix the graph state for")

    @field_validator("thread_id")
    @classmethod
    def thread_id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("thread_id cannot be empty or whitespace")
        return v.strip()

    config: dict[str, Any] | None = Field(
        default=None, description="Optional configuration for the fix operation"
    )


class FixGraphResponseSchema(BaseModel):
    """Schema for the fix graph operation response."""

    success: bool = Field(..., description="Whether the fix operation was successful")
    message: str = Field(..., description="Status message from the fix operation")
    removed_count: int = Field(
        default=0, description="Number of messages with empty tool calls that were removed"
    )
    state: dict[str, Any] | None = Field(
        default=None, description="Updated state after fixing the graph"
    )


class WsGraphInputSchema(BaseModel):
    """
    WebSocket graph input schema.

    Extends ``GraphInputSchema`` with an ``invoke_type`` discriminator so the
    server can log the intent and validate fields appropriately without
    inspecting the stream.

    ``invoke_type="fresh"``
        Start a new run.  ``messages`` must contain at least one user message.
        ``config.thread_id`` is optional — omitting it starts a new thread.

    ``invoke_type="resume"``
        Continue after a remote tool call.  ``tool_result`` must contain the
        tool-result messages.  ``config.thread_id`` is required so the server
        can resume the correct checkpointed thread.  ``messages`` is unused.
    """

    invoke_type: Literal["fresh", "resume"] = Field(
        default="fresh",
        description=(
            "Run type: 'fresh' to start a new run; 'resume' to continue after a remote tool call."
        ),
    )

    # ── Fresh fields ─────────────────────────────────────────────────────
    messages: list[Message] = Field(
        default_factory=list,
        description="User messages — required and non-empty for 'fresh' runs.",
    )

    # ── Resume fields ─────────────────────────────────────────────────────
    tool_result: list[Message] | None = Field(
        default=None,
        description=(
            "Tool-result messages to inject into the graph state for 'resume' runs. "
            "The server passes these to stream_graph as the input messages so the "
            "graph checkpointer picks them up and the graph continues from where it "
            "left off.  Ignored for 'fresh' runs."
        ),
    )

    resume: Any = Field(
        default=None,
        description=(
            "Answer for a thread paused by interrupt(), with invoke_type 'fresh' and no "
            "messages needed. Send it, possibly as null, only to resume."
        ),
    )
    interrupt_id: str | None = Field(
        default=None,
        description=(
            "Id of the interrupt `resume` answers. Optional; when sent and the thread is not "
            "paused at it, an error chunk is sent before anything runs."
        ),
    )

    # ── Shared fields (mirror GraphInputSchema) ───────────────────────────
    initial_state: dict[str, Any] | None = Field(
        default=None,
        description="Initial state for the graph execution.",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description="Optional configuration.  Must include 'thread_id' for resume runs.",
    )
    recursion_limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description="Maximum recursion limit for graph execution.",
    )
    on_busy: Literal["wait", "cancel"] = Field(
        default="wait",
        description=(
            "What to do when another run is active on the same thread. 'wait' (default) queues "
            "behind it for up to 30 seconds, then the request is rejected with 409 "
            "'thread is busy'. 'cancel' asks the active run to stop, then waits for it the "
            "same way and runs this request."
        ),
    )
    response_granularity: ResponseGranularity = Field(
        default=ResponseGranularity.LOW,
        description="Granularity of the response (full, partial, low).",
    )

    @field_validator("messages", "tool_result", mode="before")
    @classmethod
    def messages_within_limits(cls, v: Any, info: ValidationInfo) -> Any:
        if error := request_size_error(v, info.field_name or "messages"):
            raise ClientInputError(error)
        return v

    @model_validator(mode="after")
    def validate_by_invoke_type(self) -> "WsGraphInputSchema":
        if self.invoke_type == "fresh":
            if not self.messages and "resume" not in self.model_fields_set:
                raise ValueError("messages must not be empty for invoke_type='fresh'")
        elif self.invoke_type == "resume":
            if not self.tool_result:
                raise ValueError("tool_result must not be empty for invoke_type='resume'")
            if not (self.config or {}).get("thread_id"):
                raise ValueError("config.thread_id is required for invoke_type='resume'")
        # Resume sends tool *results*; neither run type may carry a tool call.
        for messages in (self.messages, self.tool_result):
            if error := client_message_error(messages):
                raise ValueError(error)
        return self

    def to_graph_input(self) -> "GraphInputSchema":
        """
        Convert to a ``GraphInputSchema`` for ``GraphService.stream_graph``.

        For *fresh* runs the user's ``messages`` are passed directly.
        For *resume* runs the ``tool_result`` messages are used — the graph
        loads the saved checkpoint (identified by ``config.thread_id``) and
        the tool-result messages are appended to the state, which the graph
        then processes to continue execution.
        """
        msgs = self.messages if self.invoke_type == "fresh" else (self.tool_result or [])
        resume = {"resume": self.resume} if "resume" in self.model_fields_set else {}
        return GraphInputSchema(
            **resume,
            interrupt_id=self.interrupt_id,
            messages=msgs,
            initial_state=self.initial_state,
            config=self.config,
            recursion_limit=self.recursion_limit,
            on_busy=self.on_busy,
            response_granularity=self.response_granularity,
        )
