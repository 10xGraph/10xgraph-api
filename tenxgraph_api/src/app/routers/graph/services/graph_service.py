import asyncio
import contextlib
import re
import sys
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from injectq import InjectQ, inject, singleton
from pydantic import BaseModel, TypeAdapter, ValidationError
from tenxgraph.core.exceptions import GraphClosedError
from tenxgraph.core.exceptions.media_exceptions import UnsupportedMediaInputError
from tenxgraph.core.graph import CompiledGraph
from tenxgraph.core.state import AgentState, Message, StreamChunk, StreamEvent
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.utils import ResponseGranularity

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import (
    ALWAYS_ALLOWED_CONFIG_KEYS,
    ClientInputError,
    InterruptConflictError,
    _field,
    _tool_calls,
    client_config,
    client_error_detail,
    normalize_thread_id,
    owned_thread_info,
    reject_builtin_state_keys,
    reject_unlisted_keys,
    tool_result_ids,
    unanswered_tool_calls,
)
from tenxgraph_api.src.app.core.concurrency import Lease
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig
from tenxgraph_api.src.app.core.shutdown import KEEPALIVE_SECONDS, end_on_shutdown
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe, sanitize_for_logging
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    GraphInvokeOutputSchema,
    GraphSchema,
    GraphToolsSchema,
    ObservabilitySchema,
    ObsRunSchema,
    ToolNodeSchema,
    ToolSchema,
)
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    MediaURLBlockedError,
    check_external_media_urls,
    internal_media_keys,
    preprocess_multimodal_messages,
    resolve_media_policy,
)
from tenxgraph_api.src.app.routers.graph.thread_lock import acquire_thread_lock
from tenxgraph_api.src.app.utils import DefaultThreadNameGenerator, ThreadNameGenerator
from tenxgraph_api.src.app.utils.run_trace import (
    event_summary,
    reconstruct_run,
    telemetry_record,
)
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore


def _run_tool_names(config: dict[str, Any]) -> set[str]:
    """Names of the per-run client tools in ``config["remote_tools"]`` (server-owned key)."""
    names: set[str] = set()
    for item in config.get("remote_tools") or []:
        if isinstance(item, dict):
            spec = item.get("function") if isinstance(item.get("function"), dict) else item
            if isinstance(spec.get("name"), str):
                names.add(spec["name"])
    return names


def _core_error(name: str) -> tuple[type[Exception], ...]:
    """The core exception class ``name`` as a tuple for ``isinstance``/``except``, or ``()``.

    Looked up by name so an older core without it still works; imported lazily for the same
    circular-import reason as in :func:`_reraise_framework_errors`.
    """
    from tenxgraph.core import exceptions

    cls = getattr(exceptions, name, None)
    return (cls,) if isinstance(cls, type) and issubclass(cls, Exception) else ()


def _resume_error_types() -> tuple[type[Exception], ...]:
    """Every error that means "this resume does not fit the open interrupt"."""
    return (
        InterruptConflictError,
        *_core_error("InterruptMismatchError"),
        *_core_error("NothingToResumeError"),
        *_core_error("InterruptPendingError"),
        *_core_error("ResumeValidationError"),
    )


def _resume_http_error(exc: Exception) -> HTTPException:
    """409 for a resume aimed at the wrong (or no) interrupt, 422 for an answer that fails the
    interrupt's schema. Both messages are written for the client, so production keeps them."""
    conflict = (
        InterruptConflictError,
        *_core_error("InterruptMismatchError"),
        *_core_error("NothingToResumeError"),
        *_core_error("InterruptPendingError"),
    )
    return HTTPException(
        status_code=409 if isinstance(exc, conflict) else 422,
        detail=str(getattr(exc, "message", None) or exc),
    )


def _reraise_framework_errors(exc: Exception) -> None:
    """Re-raise typed errors that have dedicated handlers instead of masking them.

    ``HTTPException`` (already carrying the right status) and the framework's typed
    exceptions have registered handlers in ``handle_errors.py`` that map them to the
    correct 4xx/5xx. Swallowing them into a generic 500 turns client-caused failures
    (recursion-limit exhaustion, bad tool args) into server errors.

    The tenxgraph exceptions are imported lazily here: a top-level import of the
    ``tenxgraph.core.exceptions`` package triggers a circular import through the core
    media modules during test collection.
    """
    if isinstance(exc, HTTPException):
        raise exc
    from tenxgraph.core.exceptions import (
        GraphError,
        GraphRecursionError,
        MetricsError,
        NodeError,
        SchemaVersionError,
        SerializationError,
        StorageError,
        TransientStorageError,
    )

    typed = (
        GraphError,
        GraphRecursionError,
        NodeError,
        StorageError,
        TransientStorageError,
        SchemaVersionError,
        SerializationError,
        MetricsError,
    )
    if isinstance(exc, typed):
        raise exc


class RunTimeoutError(TimeoutError):
    """A graph run passed ``runs.timeout`` and was cancelled."""

    def __init__(self, timeout: float) -> None:
        super().__init__(
            f"Graph run passed the runs.timeout limit of {timeout:g}s and was cancelled. "
            'Raise "runs.timeout" in 10xgraph.json to allow longer runs.'
        )
        self.timeout = timeout


_STREAM_DONE = object()

# ``data.code`` of the error chunk for a run refused while the graph is closing.
SERVICE_UNAVAILABLE_CODE = "SERVICE_UNAVAILABLE"


def _stream_error_reason(exc: Exception) -> str:
    """The client-facing reason for a failed stream.

    The global error handlers never see a stream failure (it is caught inside the generator),
    so sanitize here too. Otherwise a driver/DB exception embedding a connection string or
    internal path would stream verbatim in production, bypassing MODE=production redaction.
    """
    if isinstance(exc, RunTimeoutError):
        return str(exc)  # our own message, nothing internal to hide
    if isinstance(exc, _resume_error_types()):
        return str(getattr(exc, "message", None) or exc)  # written for the client
    # Imported lazily: a top-level import of handle_errors creates a circular import through
    # the shared utils/exceptions modules.
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.exceptions.handle_errors import _sanitize_error_message

    is_production = get_settings().MODE == "production"
    return _sanitize_error_message(str(exc), "GRAPH_STREAM_ERROR", is_production)


def _stream_error_data(exc: Exception) -> dict[str, Any]:
    """The ``data`` of a stream's terminal ERROR chunk.

    A run refused because the graph is closing carries the same code and retry hint as the
    HTTP 503 (``SERVICE_UNAVAILABLE``), so a client can tell it from a failure and retry.
    """
    if isinstance(exc, GraphClosedError):
        from tenxgraph_api.src.app.core.exceptions.server_errors import RETRY_AFTER_SECONDS

        return {
            "reason": "The server is shutting down. Retry the request.",
            "code": SERVICE_UNAVAILABLE_CODE,
            "retry_after_seconds": RETRY_AFTER_SECONDS,
        }
    return {"reason": _stream_error_reason(exc)}


# Chunk ``data`` fields that carry exception text. ``message`` is what a tool's own
# ``emitter.error()`` sends.
_ERROR_TEXT_FIELDS = ("reason", "error", "message")
_ERROR_CODE = re.compile(r"^\[([A-Z][A-Z_]*_\d+)\]")
# Core's own text for an unexpected tool failure: a tool name and a log reference, no detail.
_SAFE_TOOL_FAILURE = re.compile(r"failed with an internal error \(reference [0-9a-f]{8}\)\.$")


def generic_error_text(text: Any) -> Any:
    """The production form of an error text: a generic message keeping only its error code.

    Core's own safe tool-failure text (a tool name and a log reference) and non-strings pass
    through unchanged.
    """
    from tenxgraph_api.src.app.core.exceptions.handle_errors import _sanitize_error_message

    if not isinstance(text, str) or _SAFE_TOOL_FAILURE.search(text):
        return text
    code = _ERROR_CODE.match(text)
    return _sanitize_error_message(text, code.group(1) if code else "GRAPH_STREAM_ERROR", True)


_PAUSE_KINDS = ("interrupt", "before", "after", "remote_tool")


def _is_pause_update(chunk: StreamChunk) -> bool:
    data = chunk.data
    return (
        chunk.event == StreamEvent.UPDATES
        and isinstance(data, dict)
        and (
            data.get("status") == "interrupted"
            or "interrupt" in data
            or data.get("pause") in _PAUSE_KINDS
        )
    )


def sanitize_error_chunk(chunk: StreamChunk) -> StreamChunk:
    """In production, replace the exception text in an ``ERROR`` chunk with a generic message.

    Core puts the raw exception in ``data.reason`` / ``data.error`` (and ``data.message`` for a
    tool's own error), and in ``state.execution_meta.internal_data["error"]``. The plain HTTP
    invoke path is already generic; this is the one step the stream, WebSocket and AG-UI
    paths share (they all read ``stream_chunks``). Development, and every other chunk, passes
    through unchanged. Returns a copy: the graph reuses and checkpoints its own objects.
    """
    if chunk.event != StreamEvent.ERROR:
        return chunk
    from tenxgraph_api.src.app.core.config.settings import get_settings

    if get_settings().MODE != "production":
        return chunk

    generic = generic_error_text
    update: dict[str, Any] = {}
    if isinstance(chunk.data, dict):
        update["data"] = {
            key: generic(value) if key in _ERROR_TEXT_FIELDS else value
            for key, value in chunk.data.items()
        }
    meta = getattr(chunk.state, "execution_meta", None)
    internal = getattr(meta, "internal_data", None)
    if isinstance(internal, dict) and "error" in internal:
        safe_meta = meta.model_copy(
            update={"internal_data": {**internal, "error": generic(internal["error"])}}
        )
        update["state"] = chunk.state.model_copy(update={"execution_meta": safe_meta})
    return chunk.model_copy(update=update) if update else chunk


async def _bounded_stream(stream: AsyncIterator[Any], timeout: float) -> AsyncIterator[Any]:
    """Yield from ``stream``, cancelling it once ``timeout`` seconds have passed.

    The stream runs in its own task under ``asyncio.timeout``. Cancelling the consumer's task
    instead would land wherever that task happens to be waiting, often outside this generator
    (sending a chunk to the client), where it cannot be told apart from a disconnect. One task
    also keeps the graph's context variables intact across steps. Chunks are handed over one
    at a time because the graph reuses chunk objects: it must not produce the next chunk until
    the consumer is done with the current one.
    """
    handoff: asyncio.Queue[Any] = asyncio.Queue()
    consumed = asyncio.Event()

    async def produce() -> None:
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                async for item in stream:
                    consumed.clear()
                    handoff.put_nowait(item)
                    await consumed.wait()
        except TimeoutError as exc:
            # Only our deadline is a run timeout; a TimeoutError from inside the graph (a
            # provider call, say) is reported as itself.
            handoff.put_nowait(RunTimeoutError(timeout) if deadline.expired() else exc)
        except Exception as exc:
            handoff.put_nowait(exc)
        else:
            handoff.put_nowait(_STREAM_DONE)
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()

    producer = asyncio.create_task(produce())
    try:
        while True:
            item = await handoff.get()
            if item is _STREAM_DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
            consumed.set()
    finally:
        if not producer.done():
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer


# Realtime init-frame key -> RealtimeConfig field name.
REALTIME_INIT_FIELDS = {
    "model": "model",
    "voice": "voice",
    "modalities": "response_modalities",
    "vad": "vad",
    "system_prompt": "system_instruction",
    "tools_tags": "tools_tags",
}

# Longest a background thread-naming call may run (it is a model call).
THREAD_NAME_TIMEOUT_SECONDS = 60.0

# Naming tasks kept by the service itself (no core task manager bound) are capped; over it the
# thread simply stays unnamed, as the core manager does with its own limit.
MAX_PENDING_THREAD_NAMES = 100

# A taken generated id is retried with a fresh one; each retry is another prediction the
# claimer would have had to get right.
MAX_THREAD_ID_ATTEMPTS = 5


@singleton
class GraphService:
    """
    Service class for graph-related operations.

    This class acts as an intermediary between the controllers and the
    CompiledGraph, facilitating graph execution operations.
    """

    @inject
    def __init__(
        self,
        graph: CompiledGraph,
        checkpointer: BaseCheckpointer,
        config: GraphConfig,
        thread_name_generator: ThreadNameGenerator | None = None,
    ):
        """
        Initializes the GraphService with a CompiledGraph instance.

        Args:
            graph (CompiledGraph): An instance of CompiledGraph for
                                   graph execution operations.
        """
        self._graph = graph
        self.config = config
        self.checkpointer = checkpointer
        self.thread_name_generator = thread_name_generator

        # Telemetry store is optional (bound in the loader). Resolve lazily so unit
        # tests that construct GraphService directly don't require the binding.
        self._telemetry: TelemetryStore | None = None

        # Lazy import to avoid circular dependency
        self._media_service = None

    @property
    def is_live_agent(self) -> bool:
        """Whether the configured graph is a realtime (live) agent.

        A live graph must be driven over the ``/v1/graph/live`` realtime WebSocket; a
        non-live (turn-based) graph must use ``/v1/graph/ws``. The WebSocket handlers use
        this to reject the wrong agent type up front.

        Prefers the public ``CompiledGraph.is_realtime()`` (newer core releases) and falls
        back to the internal ``_find_live_nodes()`` probe on releases that predate it.
        """
        is_realtime = getattr(self._graph, "is_realtime", None)
        if callable(is_realtime):
            return bool(is_realtime())
        find_live_nodes = getattr(self._graph, "_find_live_nodes", None)
        if callable(find_live_nodes):
            return bool(find_live_nodes())
        return False

    @property
    def media_service(self):
        if self._media_service is None:
            try:
                container = InjectQ.get_instance()
                from tenxgraph_api.src.app.routers.media import MediaService

                self._media_service = container.try_get(MediaService)
            except Exception:
                self._media_service = None
        return self._media_service

    @property
    def media_policy(self) -> Any:
        """The graph's media fetch policy (``None`` when the installed core has none)."""
        return resolve_media_policy(self._graph)

    def check_media_urls(self, messages: list[Any]) -> None:
        """Reject external media URLs the graph's fetch policy blocks (400, no network)."""
        check_external_media_urls(messages, self.media_policy)

    async def _save_thread_name(
        self,
        config: dict[str, Any],
        thread_id: int,
        messages: list[str],
    ) -> str:
        """
        Save the generated thread name to the database.
        """
        if not self.thread_name_generator:
            thread_name = await DefaultThreadNameGenerator().generate_name([])
            logger.debug("No thread name generator configured, using default generator.")
            return thread_name

        thread_name = await self.thread_name_generator.generate_name(messages)

        res = await self.checkpointer.aput_thread(
            config,
            owned_thread_info(config, thread_id, thread_name=thread_name),
        )
        if res:
            logger.info(
                "Generated thread name: %s for thread_id: %s",
                log_safe(thread_name),
                log_safe(thread_id),
            )

        return thread_name

    def _name_thread_in_background(
        self, config: dict[str, Any], thread_id: Any, messages: list[str]
    ) -> None:
        """Generate and save a new thread's name without holding up the response.

        Runs on the core ``BackgroundTaskManager`` when one is bound, so shutdown drains it;
        otherwise on a task this service keeps a reference to. A failure is logged, never
        raised: the run itself already succeeded.
        """

        async def name_thread() -> None:
            try:
                await self._save_thread_name(config, thread_id, messages)
            except Exception as exc:
                logger.warning("Thread naming failed for thread %s: %s", log_safe(thread_id), exc)

        from tenxgraph.utils.background_task_manager import BackgroundTaskManager

        # Only a bound manager (the graph binds its own): try_get on an unbound concrete class
        # would build a fresh, untracked one that shutdown never drains.
        container = InjectQ.get_instance()
        manager = (
            container.try_get(BackgroundTaskManager) if BackgroundTaskManager in container else None
        )
        if isinstance(manager, BackgroundTaskManager):
            manager.create_task(
                name_thread(), name="thread_name", timeout=THREAD_NAME_TIMEOUT_SECONDS
            )
            return
        tasks: set[asyncio.Task[None]] = self.__dict__.setdefault("_naming_tasks", set())
        if len(tasks) >= MAX_PENDING_THREAD_NAMES:
            logger.warning(
                "Too many thread names pending (%d); leaving thread %s unnamed",
                len(tasks),
                log_safe(thread_id),
            )
            return
        task = asyncio.create_task(name_thread())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def drain_naming_tasks(self) -> None:
        """Wait for the naming tasks this service holds itself (call at shutdown).

        Tasks on the core ``BackgroundTaskManager`` are drained with that manager. There is
        no timeout: a naming model call is the developer's own.
        """
        tasks = list(self.__dict__.get("_naming_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _save_thread(self, config: dict[str, Any], thread_id: str):
        """
        Save the generated thread name to the database.
        """
        return await self.checkpointer.aput_thread(config, owned_thread_info(config, thread_id))

    def _extract_context_info(
        self, raw_state, result: dict[str, Any]
    ) -> tuple[list[Message] | None, str | None]:
        """Extract context and context_summary from result or state."""
        context: list[Message] | None = result.get("context")
        context_summary: str | None = result.get("context_summary")

        # If not found, try reading from state (supports both dict and model)
        if not context_summary and raw_state is not None:
            try:
                if isinstance(raw_state, dict):
                    context_summary = raw_state.get("context_summary")
                else:
                    context_summary = getattr(raw_state, "context_summary", None)
            except Exception:
                context_summary = None

        if not context and raw_state is not None:
            try:
                if isinstance(raw_state, dict):
                    context = raw_state.get("context")
                else:
                    context = getattr(raw_state, "context", None)
            except Exception:
                context = None

        return context, context_summary

    @property
    def telemetry(self) -> TelemetryStore | None:
        """The bound TelemetryStore, if any (resolved lazily, cached)."""
        if getattr(self, "_telemetry", None) is None:
            try:
                self._telemetry = InjectQ.get_instance().try_get(TelemetryStore)
            except Exception:
                self._telemetry = None
        return self._telemetry

    def _close_run_trace(self, thread_id: str | None, run_id: str | None) -> None:
        """Close a run's trace that nothing else closed: cancelled, disconnected or failed."""
        store = self.telemetry
        if not store or not thread_id or not run_id:
            return
        exc = sys.exc_info()[1]
        status = "stopped" if isinstance(exc, asyncio.CancelledError | GeneratorExit) else "error"
        if exc is None:
            status = "stopped"
        try:
            store.close_run(thread_id, run_id, datetime.now().timestamp(), status)
        except Exception as e:  # - telemetry must never break a run
            logger.debug("Telemetry close_run failed: %s", e)

    @staticmethod
    def _telemetry_record(chunk: Any, ts: float | None = None) -> dict[str, Any]:
        """Flatten a StreamChunk into a compact record for the telemetry store."""
        return telemetry_record(chunk, ts)

    def _record_chunk(
        self, thread_id: str, run_id: str, chunk: Any, ts: float | None = None
    ) -> None:
        store = self.telemetry
        if not store:
            return
        try:
            store.record(thread_id, run_id, self._telemetry_record(chunk, ts))
        except Exception as e:  # - telemetry must never break a run
            logger.debug("Telemetry record failed: %s", e)

    async def stop_graph(
        self,
        thread_id: str,
        user: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Stop the graph execution for a specific thread.

        Args:
            thread_id (str): The thread ID to stop
            user (dict): User information for context
            config (dict, optional): Additional configuration for the stop operation

        Returns:
            dict: Stop result with status information

        Raises:
            HTTPException: If stop operation fails or user doesn't have permission.
        """
        try:
            logger.info("Stopping graph execution for thread: %s", log_safe(thread_id))
            logger.debug("User info: %s", log_safe(sanitize_for_logging(user)))

            # Start with client config if provided, then overlay trusted attributes
            stop_config = client_config(config)
            stop_config.update(
                {
                    "thread_id": thread_id,
                    "user": user,
                    "user_id": user.get("user_id", "anonymous"),
                }
            )

            # Call the graph's astop method
            result = await self._graph.astop(stop_config)

            logger.info(
                "Graph stop completed for thread %s: %s", log_safe(thread_id), log_safe(result)
            )
            return result

        except ValueError as e:
            logger.warning(
                "Graph stop input validation failed for thread %s: %s",
                log_safe(thread_id),
                log_safe(e),
            )
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            _reraise_framework_errors(e)
            logger.error("Graph stop failed for thread %s: %s", log_safe(thread_id), log_safe(e))
            raise HTTPException(
                status_code=500, detail=f"Graph stop failed for thread {thread_id}: {e!s}"
            )

    def _reject_tool_start_node(self, initial_state: dict[str, Any]) -> None:
        """Allow a run to start on any node except a ``ToolNode``.

        Clients may pick the start node, e.g. to jump straight to one agent. A ``ToolNode``
        runs the tool calls in the last message of the saved context without asking the
        model, so starting there would execute tools the model never requested.
        """
        from tenxgraph.core.graph import ToolNode

        node_name = initial_state.get("current_node")
        if node_name is None:
            return
        if not isinstance(node_name, str):
            raise ClientInputError("current_node must be a node name")
        state_graph = getattr(self._graph, "_state_graph", None)
        nodes = getattr(state_graph, "nodes", {}) if state_graph else {}
        # START and END are nodes too; core also maps the legacy "start" / "end" spellings.
        if nodes and node_name not in nodes and node_name not in ("start", "end"):
            raise ClientInputError(f"unknown node '{node_name}'")
        node = nodes.get(node_name)
        if node is not None and isinstance(getattr(node, "func", None), ToolNode):
            raise ClientInputError(
                f"current_node '{node_name}' is a tool node; runs cannot start there"
            )

    def _validate_state_types(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """Check client state values against the state model's field types.

        Core stores a value on the state as given, so a wrong type (a string for an ``int``
        field) is saved and crashes a later run. Fields the model does not declare are ignored
        by core and pass. Returns the state with the validated (coerced) values.
        """
        state_graph = getattr(self._graph, "_state_graph", None)
        fields = getattr(type(getattr(state_graph, "_state", None)), "model_fields", None) or {}
        checked = dict(initial_state)
        for name, value in initial_state.items():
            field = fields.get(name)
            if field is None or field.annotation is None:
                continue
            try:
                checked[name] = TypeAdapter(field.annotation).validate_python(value)
            except ValidationError as exc:
                reason = exc.errors()[0]["msg"] if exc.errors() else "invalid value"
                raise ClientInputError(f"initial_state.{name} is invalid: {reason}") from exc
        return checked

    def _remote_tool_names(self) -> set[str]:
        """Names of the tools the client runs (remote tools), across every ToolNode."""
        from tenxgraph.core.graph import ToolNode

        state_graph = getattr(self._graph, "_state_graph", None)
        nodes = getattr(state_graph, "nodes", {}) if state_graph else {}
        names: set[str] = set()
        for node in nodes.values():
            tool_node = getattr(node, "func", None)
            if isinstance(tool_node, ToolNode):
                names.update(n for n in getattr(tool_node, "remote_tool_names", None) or [] if n)
        return names

    def _server_tool_names(self) -> set[str]:
        """Names of the tools the server runs itself (local and MCP), across every ToolNode."""
        from tenxgraph.core.graph import ToolNode

        state_graph = getattr(self._graph, "_state_graph", None)
        nodes = getattr(state_graph, "nodes", {}) if state_graph else {}
        names: set[str] = set()
        for node in nodes.values():
            tool_node = getattr(node, "func", None)
            if isinstance(tool_node, ToolNode):
                names.update(getattr(tool_node, "_funcs", None) or {})
                names.update(getattr(tool_node, "mcp_tools", None) or [])
        return names

    async def _check_tool_results(self, messages: list[Any], config: dict[str, Any]) -> None:
        """Accept a tool result only as the answer to a remote tool call awaiting one.

        Remote tools run on the client, which sends their results back to resume the run.
        Anything else is a forged result: an answer to a call the model never made, a second
        answer to the same call, or an answer to a server tool the client never ran.
        """
        result_ids = tool_result_ids(messages)
        if not result_ids:
            return
        if len(set(result_ids)) != len(result_ids):
            raise ClientInputError("Each tool call can be answered only once")

        state = await self.checkpointer.aget_state(config) if self.checkpointer else None
        pending = unanswered_tool_calls(getattr(state, "context", None))
        # A client tool can never carry a server tool's name: otherwise a client could declare
        # it and post a "result" for a server tool call (one paused for approval, say).
        remote = self._remote_tool_names() | (_run_tool_names(config) - self._server_tool_names())
        for call_id in result_ids:
            if call_id not in pending or pending[call_id] not in remote:
                raise ClientInputError(f"No remote tool call '{call_id}' is waiting for a result")

    def runs_policy(self) -> RunsConfig:
        """The ``runs`` policy from 10xgraph.json (defaults when the config has none)."""
        runs = getattr(self.config, "runs", None)
        return runs if isinstance(runs, RunsConfig) else RunsConfig()

    async def new_thread_id(self) -> str:
        """A fresh server-made thread id (public name of :meth:`_new_thread_id`)."""
        return await self._new_thread_id()

    async def _new_thread_id(self) -> str:
        """Generate a thread id that nobody owns yet.

        The permission check approved "a new thread", so the id handed out here must really
        be new. Generated ids (Snowflake by default) are predictable, and a client may create
        a thread under any id it likes, so another user could have claimed the next id
        first; running on it would put this user's messages in their thread.
        """
        for _ in range(MAX_THREAD_ID_ATTEMPTS):
            thread_id = str(await InjectQ.get_instance().atry_get("generated_id") or uuid4())
            try:
                owner = await self.checkpointer.aget_thread_owner(thread_id)
            except NotImplementedError:
                return thread_id  # the checkpointer cannot tell; keep the old behaviour
            if owner is None:
                return thread_id
            logger.warning("Generated thread id %s is already taken; generating another", thread_id)
        raise RuntimeError("Could not generate an unused thread id")

    async def _check_resume_target(
        self, graph_input: GraphInputSchema, config: dict[str, Any]
    ) -> None:
        """Refuse a resume that names an interrupt the thread is not paused at.

        Only when the client sent ``interrupt_id``; without it the resume answers whatever the
        thread is paused at, as before.

        Raises:
            InterruptConflictError: The thread is not paused, or is paused at another interrupt.
        """
        if not graph_input.is_resume or graph_input.interrupt_id is None:
            return
        state = await self.checkpointer.aget_state(config) if self.checkpointer else None
        pending = None
        if state is not None:
            try:
                from tenxgraph.utils.interrupt import pending_interrupt
            except ImportError:  # core older than interrupt()
                pass
            else:
                pending = pending_interrupt(state)
        if pending is None:
            raise InterruptConflictError(
                "Nothing to resume: this thread is not paused at an interrupt"
            )
        if pending.id != graph_input.interrupt_id:
            raise InterruptConflictError(
                f"interrupt_id '{graph_input.interrupt_id}' does not match the open "
                f"interrupt '{pending.id}'"
            )

    async def check_resume_target(
        self, graph_input: GraphInputSchema, user: dict[str, Any]
    ) -> None:
        """409 for a resume aimed at the wrong interrupt, before a stream response starts.

        A stream answers 200 as soon as it starts, so the router asks this first, as invoke
        gets the same check inside its run. Nothing happens unless the request is a resume
        that names an ``interrupt_id``.

        Raises:
            HTTPException: 409 when the thread is not paused at that interrupt.
        """
        if not graph_input.is_resume or graph_input.interrupt_id is None:
            return
        thread_id = normalize_thread_id((graph_input.config or {}).get("thread_id"))
        user_id = user.get("user_id")
        config = {"thread_id": thread_id, "user_id": user_id if user_id else "anonymous"}
        config["user"] = user
        try:
            if thread_id is None:  # a new thread has nothing to resume
                raise InterruptConflictError(
                    "Nothing to resume: this thread is not paused at an interrupt"
                )
            await self._check_resume_target(graph_input, config)
        except _resume_error_types() as exc:
            raise _resume_http_error(exc) from exc

    async def _is_static_pause(self, config: dict[str, Any]) -> bool:
        """Whether the thread is paused by ``interrupt_before`` / ``interrupt_after``.

        Such a pause has nothing to answer: core continues it from any input that carries no
        ``resume`` key, so an empty resume must not reach core (it would fail with "Nothing to
        resume"). A pause on an ``interrupt()`` or on a client-side tool call is not static.
        """
        state = await self.checkpointer.aget_state(config) if self.checkpointer else None
        if state is None or not state.is_interrupted():
            return False
        try:
            from tenxgraph.utils.interrupt import pending_interrupt
        except ImportError:  # core older than interrupt(): every pause is static
            pending = None
        else:
            pending = pending_interrupt(state)
        reason = getattr(state.execution_meta, "interrupt_reason", None)
        return pending is None and reason != "remote_tool_call"

    async def _prepare_input(
        self,
        graph_input: GraphInputSchema,
        user_id: str | None = None,
        server_config: dict[str, Any] | None = None,
    ):
        is_new_thread = False
        config = client_config(graph_input.config)
        runs = self.runs_policy()
        reject_unlisted_keys(config, runs.config_keys, "config", ALWAYS_ALLOWED_CONFIG_KEYS)
        reject_builtin_state_keys(graph_input.initial_state, "initial_state")
        reject_unlisted_keys(graph_input.initial_state, runs.state_keys, "initial_state")
        # Server-owned keys set by a trusted caller (a protocol adapter), after the client's
        # own config has been stripped of them.
        config.update(server_config or {})
        # Same normalisation as the permission check, so the checked thread is the one used.
        thread_id = normalize_thread_id(config.get("thread_id"))
        if thread_id is None:
            thread_id = await self._new_thread_id()
            is_new_thread = True

        # update thread id
        config["thread_id"] = str(thread_id)

        # The authenticated user, before any checkpointer read below: PgCheckpointer refuses a
        # config without ``user_id``. Never taken from the request (``client_config`` strips it).
        config["user_id"] = user_id if user_id else "anonymous"

        # check recursion limit set or not
        config["recursion_limit"] = graph_input.recursion_limit or 25

        # Server-owned cap on parallel tool calls per model turn (clients cannot set it)
        if runs.max_parallel_tool_calls is not None:
            config["max_parallel_tool_calls"] = runs.max_parallel_tool_calls

        # Prepare the input for the graph
        # External media URLs first: the checks are cheap and need no network.
        self.check_media_urls(graph_input.messages)
        # Preprocess multimodal messages (resolve file_id → cached text, etc.)
        preprocessed = await preprocess_multimodal_messages(
            graph_input.messages,
            self.media_service,
            user_id,
        )
        # The accepted messages now use these files: record the thread on each (owned files
        # only; the ownership check above already ran).
        if user_id and self.media_service is not None:
            keys = [k for m in preprocessed for k in internal_media_keys(m)]
            if keys:
                await self.media_service.link_threads(keys, str(thread_id), str(user_id))
        input_data: dict = {
            "messages": preprocessed,
        }
        if graph_input.is_resume and not await self._is_static_pause(config):
            input_data["resume"] = graph_input.resume
            if graph_input.interrupt_id is not None:
                # Read by core 1.0+ (checks the id again and validates the answer); an older
                # core ignores unknown input keys.
                input_data["resume_interrupt_id"] = graph_input.interrupt_id
        if graph_input.initial_state:
            self._reject_tool_start_node(graph_input.initial_state)
            input_data["state"] = self._validate_state_types(graph_input.initial_state)

        return (
            input_data,
            config,
            {
                "is_new_thread": is_new_thread,
                "thread_id": str(thread_id),
            },
        )

    async def invoke_graph(  # noqa: PLR0912, PLR0915
        self,
        graph_input: GraphInputSchema,
        user: dict[str, Any],
    ) -> GraphInvokeOutputSchema:
        """
        Invokes the graph with the provided input and returns the final result.

        Args:
            graph_input (GraphInputSchema): The input data for graph execution.

        Returns:
            GraphInvokeOutputSchema: The final result from graph execution.

        Raises:
            HTTPException: If graph execution fails.
        """
        thread_lock = Lease(None)
        inv_thread_id: str | None = None
        inv_run_id: str | None = None
        try:
            logger.debug("Invoking graph with input: %s", log_safe(graph_input.messages))

            # Prepare the input
            input_data, config, meta = await self._prepare_input(graph_input, user.get("user_id"))
            thread_lock = await self._lock_new_thread(meta)
            # add user inside config
            config["user"] = user
            config["user_id"] = user.get("user_id", "anonymous")
            await self._check_tool_results(input_data["messages"], config)
            await self._check_resume_target(graph_input, config)

            # Try to save thread info in the db even for existing threads
            # this will help in updating last accessed time
            # and get is thread newly created or not, this way it's consistent
            is_new_thread = await self._save_thread(config, config["thread_id"])
            if is_new_thread and type(is_new_thread) is bool:
                meta["is_new_thread"] = True

            # Telemetry run (invoke has no chunk stream; we record from the final
            # messages so cost/usage still shows on the observability page).
            inv_thread_id = str(config["thread_id"])
            inv_run_id = str(config.get("run_id") or uuid4())
            config.setdefault("run_id", inv_run_id)
            inv_started = datetime.now().timestamp()
            if self.telemetry:
                self.telemetry.start_run(inv_thread_id, inv_run_id, inv_started)

            # Execute the graph
            result = await self._ainvoke_bounded(
                input_data, config, graph_input.response_granularity
            )

            logger.info("Graph execution completed successfully")

            # Extract messages and state from result
            messages: list[Message] = result.get("messages", [])

            # Record final messages into telemetry, then close the run.
            if self.telemetry:
                inv_now = datetime.now().timestamp()
                for msg in messages:
                    msg_ts = getattr(msg, "timestamp", None)
                    if not isinstance(msg_ts, int | float) or not inv_started <= msg_ts <= inv_now:
                        msg_ts = inv_now
                    self._record_chunk(
                        inv_thread_id,
                        inv_run_id,
                        StreamChunk(
                            event=StreamEvent.MESSAGE,
                            message=msg,
                            metadata={"node": getattr(msg, "node", "") or ""},
                            timestamp=msg_ts,
                        ),
                        msg_ts,
                    )
                self.telemetry.finish_run(
                    inv_thread_id, inv_run_id, datetime.now().timestamp(), "done"
                )
            raw_state: AgentState | None = result.get("state", None)

            # Extract context information using helper method
            context, context_summary = self._extract_context_info(raw_state, result)

            if meta["is_new_thread"] and self.config.thread_name_generator_path:
                # Naming is an extra model call; the caller should not wait for it. The name
                # is saved on the thread (GET /v1/threads) once it is ready.
                self._name_thread_in_background(
                    dict(config), config["thread_id"], [msg.text() for msg in messages]
                )

            return GraphInvokeOutputSchema(
                messages=messages,
                state=raw_state.model_dump(serialize_as_any=True) if raw_state else None,
                context=context,
                summary=context_summary,
                meta=meta,
            )

        except _resume_error_types() as e:
            logger.warning("Resume rejected: %s", log_safe(str(e)))
            raise _resume_http_error(e)
        except UnsupportedMediaInputError as e:
            logger.warning("Unsupported media input: %s", e.message)
            raise HTTPException(status_code=422, detail=e.message)
        except RunTimeoutError as e:
            logger.warning("Graph invoke cancelled: %s", e)
            raise HTTPException(status_code=504, detail=str(e))
        except MediaURLBlockedError as e:
            logger.warning("Blocked external media URL: %s", log_safe(str(e)))
            raise
        except ValueError as e:
            logger.warning(f"Graph input validation failed: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            # Let typed/HTTP errors reach their registered handlers instead of
            # collapsing every failure into a generic 500.
            _reraise_framework_errors(e)
            logger.error(f"Graph execution failed: {e}")
            raise HTTPException(status_code=500, detail=f"Graph execution failed: {e!s}")
        finally:
            self._close_run_trace(inv_thread_id, inv_run_id)
            thread_lock.release()

    @staticmethod
    async def _lock_new_thread(meta: dict[str, Any]) -> Lease:
        """Lock a thread this request just created. The router locks the ones a client names.

        Nobody is running on a new thread yet, but the client learns its id from the first
        chunk and could start a second run on it while this one is still going.
        """
        if not meta.get("is_new_thread"):
            return Lease(None)
        return await acquire_thread_lock(str(meta["thread_id"]))

    async def _ainvoke_bounded(
        self, input_data: dict, config: dict[str, Any], granularity: Any
    ) -> dict[str, Any]:
        """``ainvoke``, cancelled once it runs past ``runs.timeout`` (``None``: no limit)."""
        timeout = self.runs_policy().timeout
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                return await self._graph.ainvoke(
                    input_data, config=config, response_granularity=granularity
                )
        except TimeoutError as exc:
            if timeout is None or not deadline.expired():
                raise  # a timeout from inside the graph, not ours
            if self.telemetry:
                self.telemetry.finish_run(
                    str(config["thread_id"]),
                    str(config.get("run_id")),
                    datetime.now().timestamp(),
                    "error",
                )
            raise RunTimeoutError(timeout) from exc

    def _astream_bounded(
        self, input_data: dict, config: dict[str, Any], granularity: Any
    ) -> AsyncIterator[StreamChunk]:
        """``astream``, cancelled once it runs past ``runs.timeout`` (``None``: no limit)."""
        stream = self._graph.astream(input_data, config=config, response_granularity=granularity)
        timeout = self.runs_policy().timeout
        return stream if timeout is None else _bounded_stream(stream, timeout)

    async def stream_graph(
        self,
        graph_input: GraphInputSchema,
        user: dict[str, Any],
    ) -> AsyncIterable[str]:
        """
        Streams the graph execution with the provided input.

        Args:
            graph_input (GraphInputSchema): The input data for graph execution.
            stream_mode (str): The stream mode ("values", "updates", "messages", etc.).

        Yields:
            str: Individual JSON chunks from graph execution with newline delimiters.
        """

        async def lines() -> AsyncIterable[str]:
            last: StreamChunk | None = None
            errored = False
            async for chunk in self.stream_chunks(graph_input, user):
                errored = errored or chunk.event == StreamEvent.ERROR
                yield chunk.model_dump_json(serialize_as_any=True) + "\n"
                # The graph reuses one chunk object, so keep only the ids, not the object.
                last = StreamChunk(
                    event=StreamEvent.UPDATES,
                    thread_id=chunk.thread_id,
                    run_id=chunk.run_id,
                )
            if not errored:
                # The documented terminal chunk: a client that waits for it can tell a
                # finished stream from a cut one. WebSocket runs send the same chunk.
                done = StreamChunk(
                    event=StreamEvent.UPDATES,
                    data={"status": "done"},
                    thread_id=last.thread_id if last else None,
                    run_id=last.run_id if last else None,
                )
                yield done.model_dump_json(serialize_as_any=True) + "\n"

        # On server shutdown the stream ends with a clear final chunk, not a cut connection.
        async for line in end_on_shutdown(lines(), keepalive_seconds=KEEPALIVE_SECONDS):
            yield line

    async def stream_chunks(  # noqa: PLR0912, PLR0915
        self,
        graph_input: GraphInputSchema,
        user: dict[str, Any],
        server_config: dict[str, Any] | None = None,
    ) -> AsyncIterable[StreamChunk]:
        """
        Streams the graph execution as ``StreamChunk`` objects.

        Shared by the NDJSON stream and protocol adapters such as AG-UI. The graph reuses one
        chunk object for successive messages, so consume each chunk before asking for the next.
        ``server_config`` carries server-owned run config (for example per-run
        ``remote_tools``) that a client could not set through ``graph_input.config``.

        Yields:
            StreamChunk: Graph output; failures arrive as an ``ERROR`` chunk, never raised.
        """
        # Initialize meta here so it is available in the except blocks even if
        # _prepare_input raises before assigning it.
        meta: dict[str, Any] = {}
        thread_id: str | None = None
        run_id: str | None = None
        naming: tuple[dict[str, Any], Any, list[str]] | None = None
        thread_lock = Lease(None)
        try:
            logger.debug("Streaming graph with input: %s", log_safe(graph_input.messages))

            # Prepare the config
            input_data, config, meta = await self._prepare_input(
                graph_input, user.get("user_id"), server_config
            )
            thread_lock = await self._lock_new_thread(meta)
            # add user inside config
            config["user"] = user
            config["user_id"] = user.get("user_id", "anonymous")
            await self._check_tool_results(input_data["messages"], config)
            await self._check_resume_target(graph_input, config)

            # Try to save thread info in the db even for existing threads
            # this will help in updating last accessed time
            # and get is thread newly created or not, this way it's consistent
            is_new_thread = await self._save_thread(config, config["thread_id"])
            if is_new_thread and type(is_new_thread) is bool:
                meta["is_new_thread"] = True

            messages_str = []

            # Begin a telemetry run so the observability endpoint can rebuild the trace.
            thread_id = str(config["thread_id"])
            run_id = str(config.get("run_id") or uuid4())
            config.setdefault("run_id", run_id)
            started_at = datetime.now().timestamp()
            if self.telemetry:
                self.telemetry.start_run(thread_id, run_id, started_at)
            run_status = "done"

            # Stream the graph execution
            saw_pause = False
            async for raw_chunk in self._astream_bounded(
                input_data, config, graph_input.response_granularity
            ):
                chunk = sanitize_error_chunk(raw_chunk)
                mt = chunk.metadata or {}
                mt.update(meta)
                chunk.metadata = mt
                self._record_chunk(thread_id, run_id, chunk, datetime.now().timestamp())
                if _is_pause_update(chunk):
                    saw_pause = True
                if getattr(chunk, "is_error", False) or getattr(chunk, "event", None) in (
                    "error",
                    StreamEvent.ERROR,
                ):
                    run_status = "error"
                yield chunk
                if (
                    self.config.thread_name_generator_path
                    and meta["is_new_thread"]
                    and chunk.event == StreamEvent.MESSAGE
                    and chunk.message
                    and not chunk.message.delta
                ):
                    messages_str.append(chunk.message.text())

            if not saw_pause and graph_input.response_granularity != ResponseGranularity.FULL:
                pause = await self._missing_pause_chunk(config, meta)
                if pause is not None:
                    self._record_chunk(thread_id, run_id, pause, datetime.now().timestamp())
                    yield pause

            logger.info("Graph streaming completed successfully")

            if self.telemetry:
                self.telemetry.finish_run(thread_id, run_id, datetime.now().timestamp(), run_status)

            if meta["is_new_thread"] and self.config.thread_name_generator_path:
                naming = (dict(config), config["thread_id"], messages_str)

        except Exception as e:
            # Never raise HTTPException from inside an async generator that is
            # consumed by StreamingResponse.  By the time any exception is raised
            # from within the generator body, Starlette has already committed to a
            # 200 OK response and cannot replace it with a 4xx/5xx.  Raising here
            # would cause: RuntimeError("Caught handled exception, but response
            # already started.")  Instead, we yield a structured error chunk so
            # the client can detect and display the failure.
            logger.error(f"Graph streaming failed: {e}")
            try:
                if self.telemetry and thread_id and run_id:
                    self.telemetry.finish_run(
                        thread_id, run_id, datetime.now().timestamp(), "error"
                    )
            except Exception as telemetry_exc:
                logger.debug("Telemetry finish_run failed on stream error: %s", telemetry_exc)
            yield StreamChunk(
                event=StreamEvent.ERROR,
                data=_stream_error_data(e),
                metadata=meta,
            )
        finally:
            self._close_run_trace(thread_id, run_id)
            thread_lock.release()
            if naming is not None:
                # An extra model call: started once the thread is free, never awaited here.
                self._name_thread_in_background(*naming)

    async def _missing_pause_chunk(
        self, config: dict[str, Any], meta: dict[str, Any]
    ) -> StreamChunk | None:
        """The pause update, rebuilt from the checkpoint, when core did not send it.

        Core older than the fix drops it at ``low`` and ``partial`` granularity. A thread that
        is paused after the run (``interrupt()``, interrupt_before/after, remote-tool wait) is
        the signal; nothing is sent otherwise.
        """
        if not self.checkpointer:
            return None
        try:
            state = await self.checkpointer.aget_state(config)
        except Exception as e:  # the store is unavailable
            logger.debug("Pause lookup failed: %s", e)
            return None
        if state is None:
            return None
        try:
            from tenxgraph.utils.interrupt import pending_interrupt

            pending = pending_interrupt(state)
        except Exception:  # core older than interrupt()
            pending = None
        step = getattr(getattr(state, "execution_meta", None), "step", None)
        if pending is not None:
            data: dict[str, Any] = {
                "status": "interrupted",
                "node": pending.node,
                "step": step,
                "reason": "Graph execution paused by interrupt()",
                "pause": "interrupt",
                "interrupt": pending.model_dump(mode="json"),
            }
        else:
            data = self._static_pause_data(state, step)
            if data is None:
                return None
        return StreamChunk(
            event=StreamEvent.UPDATES,
            data=data,
            thread_id=config.get("thread_id"),
            run_id=config.get("run_id"),
            metadata=dict(meta),
        )

    @staticmethod
    def _static_pause_data(state: Any, step: int | None) -> dict[str, Any] | None:
        """Pause data for interrupt_before / interrupt_after / remote-tool waits, from the state."""
        meta = getattr(state, "execution_meta", None)
        status = str(getattr(getattr(meta, "status", None), "value", getattr(meta, "status", "")))
        node = getattr(meta, "interrupted_node", None)
        if status not in ("interrupted_before", "interrupted_after") or not node:
            return None
        if getattr(meta, "interrupt_reason", None) == "remote_tool_call":
            pause, name, reason = (
                "remote_tool",
                "node_invoked",
                "Remote tool call - graph interrupted",
            )
        elif status == "interrupted_before":
            pause, name = "before", "invoking_node"
            reason = "Graph execution interrupted before node execution"
        else:
            pause, name = "after", "node_invoked"
            reason = "Graph execution interrupted after node execution"
        return {"status": name, "node": node, "step": step, "reason": reason, "pause": pause}

    async def realtime_graph(
        self,
        input_queue: Any,
        init: dict[str, Any],
        user: dict[str, Any],
    ):
        """Bridge a realtime (audio) session over the compiled graph.

        Thin wrapper over ``CompiledGraph.arealtime``: builds the per-session config from
        the init control frame + authenticated user, persists thread info, and yields the
        normalized RealtimeEvents. The compiled graph must be rooted at a LiveAgent (e.g.
        an ``AudioAgent``); otherwise ``arealtime`` raises.
        """
        thread_id = normalize_thread_id(init.get("thread_id")) or await self._new_thread_id()
        config: dict[str, Any] = {
            "thread_id": thread_id,
            "user": user,
            "user_id": user.get("user_id", "anonymous"),
        }
        # Map the client init frame onto RealtimeConfig field names so the live agent can
        # apply per-session overrides (model/voice/modalities/vad/...). Only present keys
        # are forwarded; absent ones fall back to the agent's build-time config.
        realtime = self._restrict_realtime_overrides(self._realtime_overrides(init))
        if realtime:
            config["realtime"] = realtime
        await self._save_thread(config, thread_id)
        logger.info("Realtime graph session starting: thread_id=%s", log_safe(thread_id))

        async for event in self._graph.arealtime(input_queue, config):
            yield event

        logger.info("Realtime graph session completed: thread_id=%s", log_safe(thread_id))

    def _realtime_overrides(self, init: dict[str, Any]) -> dict[str, Any]:
        """Translate the client init frame into RealtimeConfig field overrides.

        ``model`` and ``tools_tags`` are always mapped and then restricted by
        :meth:`_restrict_realtime_overrides`. The other options are mapped only when listed in
        ``websocket.realtime_client_overrides``; the rest are dropped, so by default a client
        cannot replace the agent's system prompt.
        """
        allowed = set(self.config.websocket.realtime_client_overrides) | {"model", "tools_tags"}
        overrides: dict[str, Any] = {}
        for init_key, field in REALTIME_INIT_FIELDS.items():
            value = init.get(init_key)
            if value is None:
                continue
            if init_key not in allowed:
                logger.info(
                    "Realtime init option %r is not in websocket.realtime_client_overrides; "
                    "ignored",
                    init_key,
                )
                continue
            overrides[field] = value
        # Clients commonly send a single modality as a bare string ("AUDIO"); RealtimeConfig
        # expects a list. Coerce so the shorthand doesn't trip the one-modality validator.
        if isinstance(overrides.get("response_modalities"), str):
            overrides["response_modalities"] = [overrides["response_modalities"]]
        return overrides

    def _live_tools_tags(self) -> set[str] | None:
        """The tag filter the live agent was built with, or ``None`` when it has none."""
        find_live_nodes = getattr(self._graph, "_find_live_nodes", None)
        for _name, node in find_live_nodes() if callable(find_live_nodes) else []:
            realtime_config = getattr(getattr(node, "func", None), "realtime_config", None)
            tags = getattr(realtime_config, "tools_tags", None)
            if tags:
                return {str(tag) for tag in tags}
        return None

    def _restrict_realtime_overrides(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Drop the init-frame overrides a client is not allowed to make.

        ``model`` is honoured only when listed in ``websocket.realtime_models``. ``tools_tags``
        may only narrow the agent's own filter: widening it, or clearing it (an empty filter
        advertises every tool), would expose tools the agent was built to hide.
        """
        restricted = dict(overrides)
        model = restricted.get("model")
        if model is not None and model not in self.config.websocket.realtime_models:
            logger.info(
                "Realtime init model %r is not in websocket.realtime_models; ignored", model
            )
            restricted.pop("model")

        if "tools_tags" in restricted:
            requested = restricted.pop("tools_tags")
            if isinstance(requested, str):
                requested = [requested]
            if not isinstance(requested, list):
                requested = []
            base = self._live_tools_tags()
            tags = [str(tag) for tag in requested if base is None or str(tag) in base]
            if tags:
                restricted["tools_tags"] = tags
        return restricted

    def _realtime_options(self) -> dict[str, Any]:
        """The /v1/graph/live init options this server applies (see ``_realtime_overrides``)."""
        ws = self.config.websocket
        return {
            "client_overrides": list(ws.realtime_client_overrides),
            "models": list(ws.realtime_models),
            "model_selectable": bool(ws.realtime_models),
            "tools_tags": True,
        }

    async def graph_details(self) -> GraphSchema:
        try:
            logger.info("Getting graph details")
            res = self._graph.generate_graph()
            # Surface live/realtime capability so clients can route to /v1/graph/live
            # and gate their UI. Not part of core's generate_graph() output.
            info = res.get("info")
            if isinstance(info, dict):
                info["is_realtime"] = self.is_live_agent
                info["realtime_options"] = self._realtime_options() if self.is_live_agent else None
            return GraphSchema(**res)
        except ValueError as e:
            logger.warning(f"Graph details validation failed: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            logger.error(f"Failed to get graph details: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to get graph details: {e!s}")

    async def get_tools(self) -> GraphToolsSchema:
        """Collect the tools exposed by every ToolNode in the graph.

        Walks the compiled graph's nodes, and for each ``ToolNode`` calls
        ``all_tools()`` (which resolves local functions, MCP tools from any
        connected server, and remote/client-registered tools). Each tool is
        tagged with its ``source`` so the UI can group them.

        A failing MCP server on one node does not break the endpoint: that node's
        tools are collected best-effort and the error is logged.
        """
        # Local import to avoid a hard dependency at module import time.
        from tenxgraph.core.graph import ToolNode

        state_graph = getattr(self._graph, "_state_graph", None)
        nodes = getattr(state_graph, "nodes", {}) if state_graph else {}

        node_schemas: list[ToolNodeSchema] = []
        total_tools = 0

        for node_name, node in nodes.items():
            tool_node = getattr(node, "func", None)
            if not isinstance(tool_node, ToolNode):
                continue

            try:
                raw_tools = await tool_node.all_tools()
            except Exception as e:  # - one bad MCP server shouldn't 500 the page
                logger.warning("Failed to list tools for node '%s': %s", node_name, e)
                raw_tools = []

            # After all_tools(), these hold the names by origin.
            local_names = set(getattr(tool_node, "_funcs", {}).keys())
            mcp_names = set(getattr(tool_node, "mcp_tools", []) or [])
            remote_names = set(getattr(tool_node, "remote_tool_names", []) or [])

            tools: list[ToolSchema] = []
            for entry in raw_tools:
                fn = entry.get("function", {}) if isinstance(entry, dict) else {}
                name = fn.get("name", "")
                if name in mcp_names:
                    source = "mcp"
                elif name in remote_names:
                    source = "remote"
                elif name in local_names:
                    source = "local"
                else:
                    # Unknown origin — default to local (a plain function schema).
                    source = "local"

                tools.append(
                    ToolSchema(
                        name=name,
                        description=fn.get("description", "") or "",
                        source=source,
                        parameters=fn.get("parameters", {}) or {},
                    )
                )

            total_tools += len(tools)
            node_schemas.append(
                ToolNodeSchema(
                    node_name=node_name,
                    tool_count=len(tools),
                    tools=tools,
                )
            )

        return GraphToolsSchema(
            node_count=len(node_schemas),
            tool_count=total_tools,
            nodes=node_schemas,
        )

    async def get_observability(
        self,
        thread_id: str,
        run_id: str | None = None,
    ) -> "ObservabilitySchema":
        """Reconstruct an observability trace for a thread from captured run events.

        Rebuilds spans (root → node → llm/tool), an event list, and aggregated
        token usage from the telemetry records a run emitted. Returns the latest
        run by default, or the requested ``run_id``.
        """
        store = self.telemetry
        if not store:
            from tenxgraph_api.src.app.core.config.settings import get_settings

            reason = (
                "disabled_in_production"
                if get_settings().MODE == "production"
                else "store_unavailable"
            )
            return ObservabilitySchema(
                thread_id=str(thread_id), enabled=False, reason=reason, run_count=0
            )

        runs = store.get_runs(thread_id)
        run_ids = [r.run_id for r in runs]

        trace = None
        if run_id:
            trace = store.get_run(thread_id, run_id)
        elif runs:
            trace = runs[-1]

        if trace is None:
            return ObservabilitySchema(
                thread_id=str(thread_id),
                run_count=len(runs),
                run_ids=run_ids,
                run=None,
                reason="no_runs",
            )

        run_schema = self._reconstruct_run(trace)
        return ObservabilitySchema(
            thread_id=str(thread_id),
            run_count=len(runs),
            run_ids=run_ids,
            run=run_schema,
        )

    def _reconstruct_run(self, trace: Any) -> "ObsRunSchema":
        """Turn a captured RunTrace into spans + events + usage."""
        return reconstruct_run(trace)

    @staticmethod
    def _event_summary(rec: dict[str, Any]) -> str:
        return event_summary(rec)

    async def get_state_schema(self) -> dict:
        try:
            logger.info("Getting state schema")
            res: BaseModel = self._graph._state
            return res.model_json_schema()
        except ValueError as e:
            logger.warning(f"State schema validation failed: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            logger.error(f"Failed to get state schema: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to get state schema: {e!s}")

    def _protected_tool_calls(self, state: Any, messages: list[Any]) -> set[str]:
        """Ids of the unanswered tool calls the thread is legitimately waiting on.

        Only the latest assistant message can be waiting. Its unanswered calls are pending
        work, not broken history, when the thread is paused (``interrupt()``, or HITL
        ``interrupt_before`` / ``interrupt_after``: ``execution_meta`` reports an interrupted
        status) or when a call belongs to a remote tool the client has yet to answer.
        """
        assistants = [m for m in messages if _field(m, "role") == "assistant"]
        if not assistants:
            return set()
        waiting = unanswered_tool_calls(messages)
        mine = {cid: name for cid, name in _tool_calls(assistants[-1]) if cid in waiting}
        if not mine:
            return set()
        if state.is_interrupted() or any(
            name in self._remote_tool_names() for name in mine.values()
        ):
            return set(mine)
        return set()

    def _drop_broken_tool_calls(self, state: Any, messages: list[Any]) -> list[Any]:
        """``messages`` without unanswered assistant tool calls and orphaned tool results."""
        keep_pending: set[str] = set()
        broken = set(unanswered_tool_calls(messages))
        if broken:
            keep_pending = self._protected_tool_calls(state, messages)
        kept = [
            m
            for m in messages
            if not (
                _field(m, "role") == "assistant"
                and any(cid in broken and cid not in keep_pending for cid, _ in _tool_calls(m))
            )
        ]
        live = {cid for m in kept if _field(m, "role") == "assistant" for cid, _ in _tool_calls(m)}
        return [
            m
            for m in kept
            if not ((ids := tool_result_ids([m])) and not any(i in live for i in ids))
        ]

    async def fix_graph(
        self,
        thread_id: str,
        user: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Repair a thread whose history has a tool call that never got its result.

        A run that dies after the model asked for tools but before the results were saved
        leaves an assistant message with an unanswered tool call, which providers reject.
        This method removes every assistant message that has a tool call with no matching
        tool result (matched by call id), then removes tool results whose call is gone, and
        saves the state. Messages without tool calls are never removed. The unanswered calls
        of the latest assistant message are kept while the thread is paused or waiting on a
        client tool, because they are pending work.

        Args:
            thread_id (str): The thread ID to fix the graph state for
            user (dict): User information for context
            config (dict, optional): Additional configuration for the operation

        Returns:
            dict: Result dictionary containing:
                - success (bool): Whether the operation was successful
                - message (str): Status message
                - removed_count (int): Number of messages removed
                - state (dict): Updated state after fixing

        Raises:
            HTTPException: If the operation fails
        """
        try:
            logger.info("Starting fix graph operation for thread: %s", log_safe(thread_id))
            logger.debug("User info: %s", log_safe(sanitize_for_logging(user)))

            # Start with client config if provided, then overlay trusted attributes
            fix_config = client_config(config)
            fix_config.update(
                {"thread_id": thread_id, "user": user, "user_id": user.get("user_id", "anonymous")}
            )

            logger.debug("Fetching current state from checkpointer")
            state: AgentState | None = await self.checkpointer.aget_state(fix_config)
            if not state:
                logger.warning("No state found for thread: %s", log_safe(thread_id))
                return {
                    "success": False,
                    "message": f"No state found for thread: {thread_id}",
                    "removed_count": 0,
                    "state": None,
                }

            messages: list[Message] = list(state.context or [])
            logger.debug(f"Found {len(messages)} messages in state")
            if not messages:
                return {
                    "success": True,
                    "message": "No messages found in state",
                    "removed_count": 0,
                    "state": state.model_dump(serialize_as_any=True),
                }

            filtered = self._drop_broken_tool_calls(state, messages)
            removed_count = len(messages) - len(filtered)

            if removed_count:
                state.context = filtered
                await self.checkpointer.aput_state(fix_config, state)
                message = f"Successfully removed {removed_count} message(s)"
            else:
                message = "No unanswered tool calls or orphaned tool results found"

            return {
                "success": True,
                "message": message,
                "removed_count": removed_count,
                "state": state.model_dump(serialize_as_any=True),
            }
        except ValueError as e:
            logger.warning(f"Fix graph input validation failed: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            _reraise_framework_errors(e)
            logger.error(f"Fix graph operation failed: {e}")
            raise HTTPException(status_code=500, detail=f"Fix graph operation failed: {e!s}")
