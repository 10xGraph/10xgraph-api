"""Run the graph for one AG-UI request and stream the result as AG-UI events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from tenxgraph.utils import ResponseGranularity

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import tool_result_ids
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    KEEP_MESSAGE_IDS,
    GraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

from .converter import select_new_messages, to_tenxgraph_messages
from .event_mapper import AgUiEventMapper


# Graph config key holding the AG-UI request extras (frontend context, tools, forwarded props),
# so graph nodes and tools can read them from ``config["ag_ui"]``.
AG_UI_CONFIG_KEY = "ag_ui"

# Client state keys that would overwrite the conversation or execution metadata.
_RESERVED_STATE_KEYS = frozenset({"context", "context_summary", "execution_meta"})


async def stream_ag_ui(
    service: GraphService,
    run_input: RunAgentInput,
    user: dict[str, Any],
    encoder: EventEncoder,
    allow_client_tools: bool = True,
) -> AsyncIterator[str]:
    """Stream one AG-UI run as encoded events.

    With ``allow_client_tools`` the tools the client sends are offered to the model for this run
    (see ``ag_ui.allow_client_tools``); without it only ``remote_tools`` from 10xgraph.json are.

    The thread's checkpoint is the record of the conversation: only client messages it has not
    seen are sent to the graph. A run with nothing new (a client syncing state) still gets a
    complete ``RUN_STARTED`` / ``RUN_FINISHED`` pair, with the thread's state.
    """
    initial_state = _client_state(run_input.state)
    mapper = AgUiEventMapper(run_input.thread_id, run_input.run_id, initial_state=initial_state)
    for event in mapper.start():
        yield encoder.encode(event)

    try:
        async for event in _run_events(
            service, run_input, user, mapper, initial_state, allow_client_tools
        ):
            yield encoder.encode(event)
    except ValueError as exc:
        logger.warning("AG-UI run rejected: %s", exc)
        for event in mapper.fail(str(exc), "INVALID_INPUT"):
            yield encoder.encode(event)
    except Exception as exc:
        # The response has already started, so the failure has to travel in the stream.
        logger.error("AG-UI run failed: %s", exc)
        for event in mapper.fail("AG-UI run failed", "AG_UI_ERROR"):
            yield encoder.encode(event)

    for event in mapper.finish():
        yield encoder.encode(event)


async def _run_events(
    service: GraphService,
    run_input: RunAgentInput,
    user: dict[str, Any],
    mapper: AgUiEventMapper,
    initial_state: dict[str, Any] | None,
    allow_client_tools: bool,
) -> AsyncIterator[Any]:
    """The events between ``RUN_STARTED`` and the terminal event of one run."""
    checkpoint_state = await _checkpoint_state(service, run_input.thread_id, user)
    context = list(getattr(checkpoint_state, "context", None) or [])
    new_messages = select_new_messages(
        run_input.messages,
        known_ids={str(m.message_id) for m in context},
        answered_call_ids=set(tool_result_ids(context)),
    )
    messages = to_tenxgraph_messages(new_messages)
    paused_at = _pending_interrupt(checkpoint_state)
    resume = _resume_value(paused_at, run_input.resume or [])

    if paused_at is not None and resume is _NO_RESUME:
        # A thread paused at interrupt() only moves on with an answer; report the pause again
        # instead of running the graph.
        mapper.report_interrupt(paused_at.model_dump(mode="json"))
        return
    if not messages and resume is _NO_RESUME:
        if checkpoint_state is not None:
            for event in mapper.map_state(checkpoint_state):
                yield event
        return

    graph_input, server_config = _graph_request(run_input, messages, initial_state, resume)
    if not allow_client_tools:
        server_config = {}
    async for chunk in service.stream_chunks(graph_input, user, server_config):
        for event in mapper.map(chunk):
            yield event


def _client_state(state: Any) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    cleaned = {k: v for k, v in state.items() if k not in _RESERVED_STATE_KEYS}
    return cleaned or None


async def _checkpoint_state(service: GraphService, thread_id: str, user: dict[str, Any]) -> Any:
    if service.checkpointer is None:
        return None
    config = {
        "thread_id": thread_id,
        "user": user,
        "user_id": user.get("user_id", "anonymous"),
    }
    return await service.checkpointer.aget_state(config)


# Marks "the client sent no answer for the open interrupt" (a None answer means cancelled).
_NO_RESUME: Any = object()

# ``remote_tools`` run-config key read by the tenxgraph ToolNode for per-run client tools.
RUN_REMOTE_TOOLS_KEY = "remote_tools"


def _pending_interrupt(state: Any) -> Any:
    """The interrupt() the thread is paused at, or ``None`` (and on cores without interrupts)."""
    if state is None:
        return None
    try:
        from tenxgraph.utils.interrupt import pending_interrupt
    except ImportError:  # tenxgraph core older than interrupt()
        return None
    return pending_interrupt(state)


def _resume_value(paused_at: Any, entries: list[Any]) -> Any:
    """The graph resume value for the AG-UI ``resume`` entries.

    Returns ``_NO_RESUME`` when nothing answers the open interrupt. A ``cancelled`` entry resumes
    with ``None``.

    Raises:
        ValueError: ``resume`` was sent but the thread is not paused at that interrupt.
    """
    if not entries:
        return _NO_RESUME
    if paused_at is None:
        raise ValueError("Nothing to resume: this thread is not paused at an interrupt")
    for entry in entries:
        if entry.interrupt_id == paused_at.id:
            return entry.payload if entry.status == "resolved" else None
    raise ValueError(f"resume does not answer the open interrupt '{paused_at.id}'")


def _graph_request(
    run_input: RunAgentInput,
    messages: list[Any],
    initial_state: dict[str, Any] | None,
    resume: Any,
) -> tuple[GraphInputSchema, dict[str, Any]]:
    """The graph input for one AG-UI run, and the server-owned run config that goes with it."""
    extra: dict[str, Any] = {} if resume is _NO_RESUME else {"resume": resume}
    # AG-UI keeps the client's message ids: select_new_messages dedups on them.
    graph_input = GraphInputSchema.model_validate(
        {
            "messages": messages,
            "initial_state": initial_state,
            "config": {"thread_id": run_input.thread_id, "run_id": run_input.run_id},
            "response_granularity": ResponseGranularity.FULL,
            **extra,
        },
        context={KEEP_MESSAGE_IDS: True},
    )
    # Server-owned keys go in after the client allow-list check (``runs.config_keys``), so that
    # setting cannot reject them, and clients of the plain graph routes cannot set them.
    server_config = {
        AG_UI_CONFIG_KEY: {
            "context": [c.model_dump(mode="json") for c in run_input.context or []],
            "tools": [t.model_dump(mode="json") for t in run_input.tools or []],
            "forwarded_props": run_input.forwarded_props,
        },
        # The client's own tools, offered to the model for this run only.
        RUN_REMOTE_TOOLS_KEY: [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in run_input.tools or []
        ],
    }
    return graph_input, server_config
