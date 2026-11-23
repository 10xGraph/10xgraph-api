"""Run the graph for one AG-UI request and stream the result as AG-UI events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from tenxgraph.core.authz import Identity, UserCredential
from tenxgraph.utils import ResponseGranularity
from tenxgraph.utils.interrupt import pending_interrupt

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.exceptions.client_errors import client_error
from tenxgraph_api.src.app.core.threads import set_user
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError, tool_result_ids
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    KEEP_MESSAGE_IDS,
    GraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService, RunChecks

from .converter import select_new_messages, to_tenxgraph_messages
from .event_mapper import AgUiEventMapper


# Client state keys that would overwrite the conversation or execution metadata.
_RESERVED_STATE_KEYS = frozenset({"context", "context_summary", "execution_meta"})


async def server_tool_names(service: GraphService) -> set[str]:
    """Names of the tools the server runs itself (local and MCP), for the client tool check.

    ``mcp_tools`` fills on a ToolNode's first MCP listing; a node with an MCP client and no names
    yet is listed here through ``all_tools()`` (the node's cached list, so no network call once
    listed). A failed listing is skipped: the run reports it as before.
    """
    for _, tool_node in service.tool_nodes():
        if tool_node._client and not tool_node.mcp_tools:
            try:
                await tool_node.all_tools()
            except Exception as exc:
                logger.warning("Could not list MCP tools to check client tools: %s", exc)
    return service._server_tool_names()


async def stream_ag_ui(
    service: GraphService,
    run_input: RunAgentInput,
    user: Identity | None,
    encoder: EventEncoder,
    allow_client_tools: bool = True,
    credential: UserCredential | None = None,
    checks: RunChecks | None = None,
) -> AsyncIterator[str]:
    """Stream one AG-UI run as encoded events.

    With ``allow_client_tools`` the tools the client sends are offered to the model for this run
    (see ``ag_ui.allow_client_tools``); without it only ``remote_tools`` from 10xgraph.json are.
    ``credential`` goes into the run config for the graph's tools. ``checks`` is what the
    router already checked (``GraphService.check_run_input``).

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
            service, run_input, user, mapper, initial_state, allow_client_tools, credential, checks
        ):
            yield encoder.encode(event)
    except ValueError as exc:
        # A resume that does not fit the open interrupt, or other input the run refused.
        # The message follows the HTTP rule: kept when written for the client, else generic.
        logger.warning("AG-UI run rejected: %s", log_safe(exc))
        for event in mapper.fail(client_error(exc).message, "INVALID_INPUT"):
            yield encoder.encode(event)
    except Exception as exc:
        # The response has already started, so the failure has to travel in the stream. The
        # message is the HTTP routes' text for the same error (generic in production).
        logger.error("AG-UI run failed: %s", log_safe(exc))
        for event in mapper.fail(client_error(exc, "AG-UI run failed").message, "AG_UI_ERROR"):
            yield encoder.encode(event)

    for event in mapper.finish():
        yield encoder.encode(event)


async def _run_events(
    service: GraphService,
    run_input: RunAgentInput,
    user: Identity | None,
    mapper: AgUiEventMapper,
    initial_state: dict[str, Any] | None,
    allow_client_tools: bool,
    credential: UserCredential | None,
    checks: RunChecks | None = None,
) -> AsyncIterator[Any]:
    """The events between ``RUN_STARTED`` and the terminal event of one run."""
    checkpoint_state = await _checkpoint_state(service, run_input.thread_id, user)
    # Read under the thread lock: the run's checks use this read instead of their own.
    checks = checks or RunChecks()
    checks.state = checkpoint_state
    context = list(getattr(checkpoint_state, "context", None) or [])
    new_messages = select_new_messages(
        run_input.messages,
        known_ids={str(m.message_id) for m in context},
        answered_call_ids=set(tool_result_ids(context)),
    )
    messages = to_tenxgraph_messages(new_messages)
    paused_at = pending_interrupt(checkpoint_state) if checkpoint_state is not None else None
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

    graph_input, server_config = _graph_request(
        run_input, messages, initial_state, resume, allow_client_tools
    )
    async for chunk in service.stream_chunks(graph_input, user, server_config, credential, checks):
        for event in mapper.map(chunk):
            yield event


def _client_state(state: Any) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    cleaned = {k: v for k, v in state.items() if k not in _RESERVED_STATE_KEYS}
    return cleaned or None


async def _checkpoint_state(service: GraphService, thread_id: str, user: Identity | None) -> Any:
    if service.checkpointer is None:
        return None
    config: dict[str, Any] = {"thread_id": thread_id}
    set_user(config, user)
    return await service.checkpointer.aget_state(config)


# Marks "the client sent no answer for the open interrupt" (a None answer means cancelled).
_NO_RESUME: Any = object()

# ``remote_tools`` run-config key read by the tenxgraph ToolNode for per-run client tools.
RUN_REMOTE_TOOLS_KEY = "remote_tools"


def _resume_value(paused_at: Any, entries: list[Any]) -> Any:
    """The graph resume value for the AG-UI ``resume`` entries.

    Returns ``_NO_RESUME`` when nothing answers the open interrupt. A ``cancelled`` entry resumes
    with ``None``.

    Raises:
        ClientInputError: ``resume`` was sent but the thread is not paused at that interrupt.
    """
    if not entries:
        return _NO_RESUME
    if paused_at is None:
        raise ClientInputError("Nothing to resume: this thread is not paused at an interrupt")
    for entry in entries:
        if entry.interrupt_id == paused_at.id:
            return entry.payload if entry.status == "resolved" else None
    raise ClientInputError(f"resume does not answer the open interrupt '{paused_at.id}'")


def _graph_request(
    run_input: RunAgentInput,
    messages: list[Any],
    initial_state: dict[str, Any] | None,
    resume: Any,
    allow_client_tools: bool,
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
    if not allow_client_tools:
        return graph_input, {}
    # The client's own tools, offered to the model for this run only.
    server_config = {
        RUN_REMOTE_TOOLS_KEY: [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in run_input.tools or []
        ],
    }
    return graph_input, server_config
