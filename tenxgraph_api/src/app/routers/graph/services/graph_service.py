import asyncio
import contextlib
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from injectq import InjectQ, inject, singleton
from pydantic import BaseModel
from tenxgraph.core.exceptions.media_exceptions import UnsupportedMediaInputError
from tenxgraph.core.graph import CompiledGraph
from tenxgraph.core.state import AgentState, Message, StreamChunk, StreamEvent
from tenxgraph.storage.checkpointer import BaseCheckpointer

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import (
    ALWAYS_ALLOWED_CONFIG_KEYS,
    ClientInputError,
    client_config,
    client_error_detail,
    normalize_thread_id,
    owned_thread_info,
    reject_unlisted_keys,
    tool_result_ids,
    unanswered_tool_calls,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig
from tenxgraph_api.src.app.core.utils.log_sanitizer import sanitize_for_logging
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    GraphInvokeOutputSchema,
    GraphSchema,
    GraphToolsSchema,
    ObservabilitySchema,
    ObsEventSchema,
    ObsRunSchema,
    ObsSpanSchema,
    ObsTokenUsageSchema,
    ToolNodeSchema,
    ToolSchema,
)
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    preprocess_multimodal_messages,
)
from tenxgraph_api.src.app.utils import DefaultThreadNameGenerator, ThreadNameGenerator
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
        super().__init__(f"Graph run timed out after {timeout:g}s")
        self.timeout = timeout


_STREAM_DONE = object()


def _stream_error_reason(exc: Exception) -> str:
    """The client-facing reason for a failed stream.

    The global error handlers never see a stream failure (it is caught inside the generator),
    so sanitize here too. Otherwise a driver/DB exception embedding a connection string or
    internal path would stream verbatim in production, bypassing MODE=production redaction.
    """
    if isinstance(exc, RunTimeoutError):
        return str(exc)  # our own message, nothing internal to hide
    # Imported lazily: a top-level import of handle_errors creates a circular import through
    # the shared utils/exceptions modules.
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.exceptions.handle_errors import _sanitize_error_message

    is_production = get_settings().MODE == "production"
    return _sanitize_error_message(str(exc), "GRAPH_STREAM_ERROR", is_production)


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
            logger.info(f"Generated thread name: {thread_name} for thread_id: {thread_id}")

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
                logger.warning("Thread naming failed for thread %s: %s", thread_id, exc)

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
        task = asyncio.create_task(name_thread())
        tasks: set[asyncio.Task[None]] = self.__dict__.setdefault("_naming_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

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

    @staticmethod
    def _telemetry_record(chunk: Any) -> dict[str, Any]:
        """Flatten a StreamChunk into a compact record for the telemetry store."""
        md = getattr(chunk, "metadata", None) or {}
        node = md.get("node") or md.get("current_node") or getattr(chunk, "node_name", "") or ""

        usages = None
        message = getattr(chunk, "message", None)
        if message is not None:
            u = getattr(message, "usages", None)
            if u is not None:
                try:
                    usages = u.model_dump() if hasattr(u, "model_dump") else dict(u)
                except Exception:
                    usages = None

        # Tool call / tool result hints for span reconstruction.
        content_kinds: list[str] = []
        tool_names: list[str] = []
        if message is not None:
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    btype = getattr(block, "type", None) or (
                        block.get("type") if isinstance(block, dict) else None
                    )
                    if btype:
                        content_kinds.append(btype)
                    if btype in ("tool_call", "tool_result"):
                        bname = getattr(block, "name", None) or (
                            block.get("name") if isinstance(block, dict) else None
                        )
                        if bname:
                            tool_names.append(bname)

        event = getattr(chunk, "event", None)
        return {
            "event": str(event.value) if hasattr(event, "value") else str(event or ""),
            "node": node,
            "timestamp": getattr(chunk, "timestamp", None),
            "is_delta": bool(getattr(message, "delta", False)) if message else False,
            "role": getattr(message, "role", None) if message else None,
            "usages": usages,
            "content_kinds": content_kinds,
            "tool_names": tool_names,
            "is_error": bool(getattr(chunk, "is_error", False)),
        }

    def _record_chunk(self, thread_id: str, run_id: str, chunk: Any) -> None:
        store = self.telemetry
        if not store:
            return
        try:
            store.record(thread_id, run_id, self._telemetry_record(chunk))
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
            logger.info(f"Stopping graph execution for thread: {thread_id}")
            logger.debug(f"User info: {sanitize_for_logging(user)}")

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

            logger.info(f"Graph stop completed for thread {thread_id}: {result}")
            return result

        except ValueError as e:
            logger.warning(f"Graph stop input validation failed for thread {thread_id}: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            _reraise_framework_errors(e)
            logger.error(f"Graph stop failed for thread {thread_id}: {e}")
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
        if not isinstance(node_name, str):
            return
        state_graph = getattr(self._graph, "_state_graph", None)
        nodes = getattr(state_graph, "nodes", {}) if state_graph else {}
        node = nodes.get(node_name)
        if node is not None and isinstance(getattr(node, "func", None), ToolNode):
            raise ClientInputError(
                f"current_node '{node_name}' is a tool node; runs cannot start there"
            )

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

        # check recursion limit set or not
        config["recursion_limit"] = graph_input.recursion_limit or 25

        # Prepare the input for the graph
        # Preprocess multimodal messages (resolve file_id → cached text, etc.)
        preprocessed = await preprocess_multimodal_messages(
            graph_input.messages,
            self.media_service,
            user_id,
        )
        input_data: dict = {
            "messages": preprocessed,
        }
        if graph_input.is_resume:
            input_data["resume"] = graph_input.resume
        if graph_input.initial_state:
            self._reject_tool_start_node(graph_input.initial_state)
            input_data["state"] = graph_input.initial_state

        return (
            input_data,
            config,
            {
                "is_new_thread": is_new_thread,
                "thread_id": str(thread_id),
            },
        )

    async def invoke_graph(
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
        try:
            logger.debug(f"Invoking graph with input: {graph_input.messages}")

            # Prepare the input
            input_data, config, meta = await self._prepare_input(graph_input, user.get("user_id"))
            # add user inside config
            config["user"] = user
            config["user_id"] = user.get("user_id", "anonymous")
            await self._check_tool_results(input_data["messages"], config)

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
                for msg in messages:
                    self._record_chunk(
                        inv_thread_id,
                        inv_run_id,
                        StreamChunk(
                            event=StreamEvent.MESSAGE,
                            message=msg,
                            metadata={"node": getattr(msg, "node", "") or ""},
                            timestamp=datetime.now().timestamp(),
                        ),
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

        except UnsupportedMediaInputError as e:
            logger.warning("Unsupported media input: %s", e.message)
            raise HTTPException(status_code=422, detail=e.message)
        except RunTimeoutError as e:
            logger.warning("Graph invoke cancelled: %s", e)
            raise HTTPException(status_code=504, detail=str(e))
        except ValueError as e:
            logger.warning(f"Graph input validation failed: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            # Let typed/HTTP errors reach their registered handlers instead of
            # collapsing every failure into a generic 500.
            _reraise_framework_errors(e)
            logger.error(f"Graph execution failed: {e}")
            raise HTTPException(status_code=500, detail=f"Graph execution failed: {e!s}")

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
        async for chunk in self.stream_chunks(graph_input, user):
            yield chunk.model_dump_json(serialize_as_any=True) + "\n"

    async def stream_chunks(
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
        try:
            logger.debug(f"Streaming graph with input: {graph_input.messages}")

            # Prepare the config
            input_data, config, meta = await self._prepare_input(
                graph_input, user.get("user_id"), server_config
            )
            # add user inside config
            config["user"] = user
            config["user_id"] = user.get("user_id", "anonymous")
            await self._check_tool_results(input_data["messages"], config)

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
            async for chunk in self._astream_bounded(
                input_data, config, graph_input.response_granularity
            ):
                mt = chunk.metadata or {}
                mt.update(meta)
                chunk.metadata = mt
                self._record_chunk(thread_id, run_id, chunk)
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

            logger.info("Graph streaming completed successfully")

            if self.telemetry:
                self.telemetry.finish_run(thread_id, run_id, datetime.now().timestamp(), run_status)

            if meta["is_new_thread"] and self.config.thread_name_generator_path:
                thread_name = await self._save_thread_name(
                    config, config["thread_id"], messages_str
                )
                meta["thread_name"] = thread_name

                yield StreamChunk(
                    event=StreamEvent.UPDATES,
                    data={"status": "completed"},
                    metadata=meta,
                )

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
                data={"reason": _stream_error_reason(e)},
                metadata=meta,
            )

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
        logger.info("Realtime graph session starting: thread_id=%s", thread_id)

        async for event in self._graph.arealtime(input_queue, config):
            yield event

        logger.info("Realtime graph session completed: thread_id=%s", thread_id)

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

    async def graph_details(self) -> GraphSchema:
        try:
            logger.info("Getting graph details")
            res = self._graph.generate_graph()
            # Surface live/realtime capability so clients can route to /v1/graph/live
            # and gate their UI. Not part of core's generate_graph() output.
            info = res.get("info")
            if isinstance(info, dict):
                info["is_realtime"] = self.is_live_agent
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
            return ObservabilitySchema(thread_id=str(thread_id), run_count=0)

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
        records = list(trace.records)
        started = trace.started_at
        # Fall back to the first record's timestamp if start wasn't stamped.
        first_ts = next((r.get("timestamp") for r in records if r.get("timestamp")), started)
        base = started or first_ts or 0.0
        last_ts = max([r.get("timestamp") or base for r in records] + [trace.finished_at or base])
        total_ms = max(0.0, (last_ts - base) * 1000.0)

        def off_ms(ts: float | None) -> float:
            return max(0.0, ((ts or base) - base) * 1000.0)

        usage = ObsTokenUsageSchema()
        events: list[ObsEventSchema] = []
        spans: list[ObsSpanSchema] = []

        # Root span spans the whole run.
        spans.append(
            ObsSpanSchema(
                id="root",
                name="graph",
                kind="root",
                parent=None,
                start_ms=0.0,
                duration_ms=total_ms,
            )
        )

        # Walk records to build node spans (by node transitions) + llm/tool spans.
        node_spans: dict[str, ObsSpanSchema] = {}
        node_order: list[str] = []
        llm_calls = 0
        tool_calls = 0
        span_seq = 0

        def new_span_id() -> str:
            nonlocal span_seq
            span_seq += 1
            return f"s{span_seq}"

        for idx, rec in enumerate(records):
            node = rec.get("node") or "—"
            ts = rec.get("timestamp")
            ev_type = rec.get("event") or "message"

            # Node span: open on first sight of a node, extend its end as records arrive.
            if node and node != "—":
                span = node_spans.get(node)
                if span is None:
                    span = ObsSpanSchema(
                        id=new_span_id(),
                        name=f"node: {node}",
                        kind="node",
                        parent="root",
                        start_ms=off_ms(ts),
                        duration_ms=0.0,
                    )
                    node_spans[node] = span
                    node_order.append(node)
                    spans.append(span)
                else:
                    span.duration_ms = max(span.duration_ms, off_ms(ts) - span.start_ms)

            # LLM span: a non-delta assistant message with usages under this node.
            u = rec.get("usages")
            if u:
                usage.prompt_tokens += int(u.get("prompt_tokens", 0) or 0)
                usage.completion_tokens += int(u.get("completion_tokens", 0) or 0)
                usage.reasoning_tokens += int(u.get("reasoning_tokens", 0) or 0)
                usage.total_tokens += int(
                    u.get("total_tokens", 0)
                    or (u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0)
                )
                llm_calls += 1
                parent = node_spans.get(node)
                spans.append(
                    ObsSpanSchema(
                        id=new_span_id(),
                        name="llm.generate",
                        kind="llm",
                        parent=parent.id if parent else "root",
                        start_ms=off_ms(ts),
                        duration_ms=0.0,
                        model=(rec.get("model") or None),
                        input_tokens=int(u.get("prompt_tokens", 0) or 0),
                        output_tokens=int(u.get("completion_tokens", 0) or 0),
                    )
                )

            # Tool spans: from tool_call / tool_result content blocks.
            for tname in rec.get("tool_names", []) or []:
                if "tool_call" in (rec.get("content_kinds") or []):
                    tool_calls += 1
                    parent = node_spans.get(node)
                    spans.append(
                        ObsSpanSchema(
                            id=new_span_id(),
                            name=f"tool: {tname}",
                            kind="tool",
                            parent=parent.id if parent else "root",
                            start_ms=off_ms(ts),
                            duration_ms=0.0,
                        )
                    )

            # Event row.
            events.append(
                ObsEventSchema(
                    id=f"e{idx}",
                    type=ev_type,
                    node=node if node != "—" else "",
                    offset_ms=off_ms(ts),
                    summary=self._event_summary(rec),
                )
            )

        # Newest-first events for the pane.
        events.reverse()

        return ObsRunSchema(
            run_id=trace.run_id,
            thread_id=trace.thread_id,
            status=trace.status,
            started_at=trace.started_at,
            finished_at=trace.finished_at,
            duration_ms=total_ms,
            spans=spans,
            events=events,
            usage=usage,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            iterations=len(node_order),
        )

    @staticmethod
    def _event_summary(rec: dict[str, Any]) -> str:
        kinds = rec.get("content_kinds") or []
        tools = rec.get("tool_names") or []
        if rec.get("is_error"):
            return "error"
        if tools and ("tool_call" in kinds or "tool_result" in kinds):
            label = "tool_call" if "tool_call" in kinds else "tool_result"
            return f"{label} {', '.join(tools)}"
        if rec.get("usages"):
            u = rec["usages"]
            return f"llm.generate · {u.get('total_tokens', 0)} tokens"
        if rec.get("is_delta"):
            return "delta"
        if kinds:
            return f"{rec.get('role') or 'message'}: {', '.join(kinds)}"
        return rec.get("event") or "chunk"

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

    def _has_empty_tool_call(self, msg: Message) -> bool:
        """Return True if any tool call on the message has empty content.

        A tool call is considered empty if its ``content`` attribute/key is ``None`` or
        an empty string. Tool calls may be dict-like or objects with a ``content`` attribute.
        """
        tool_calls = getattr(msg, "tools_calls", None)
        if not tool_calls:
            return False
        for tool_call in tool_calls:
            content = (
                tool_call.get("content")
                if isinstance(tool_call, dict)
                else getattr(tool_call, "content", None)
            )
            if content in (None, ""):
                return True
        return False

    async def fix_graph(
        self,
        thread_id: str,
        user: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Fix graph state by removing messages with empty tool call content.

        This method retrieves the current state from the checkpointer, identifies messages
        with tool calls that have empty content, removes those messages, and updates the
        state.

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
            logger.info(f"Starting fix graph operation for thread: {thread_id}")
            logger.debug(f"User info: {sanitize_for_logging(user)}")

            # Start with client config if provided, then overlay trusted attributes
            fix_config = client_config(config)
            fix_config.update(
                {"thread_id": thread_id, "user": user, "user_id": user.get("user_id", "anonymous")}
            )

            logger.debug("Fetching current state from checkpointer")
            state: AgentState | None = await self.checkpointer.aget_state(fix_config)
            if not state:
                logger.warning(f"No state found for thread: {thread_id}")
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
                    "state": state.model_dump_json(serialize_as_any=True),
                }

            filtered = [m for m in messages if not self._has_empty_tool_call(m)]
            removed_count = len(messages) - len(filtered)

            if removed_count:
                state.context = filtered
                await self.checkpointer.aput_state(fix_config, state)
                message = f"Successfully removed {removed_count} message(s)"
            else:
                message = "No messages with empty tool calls found"

            return {
                "success": True,
                "message": message,
                "removed_count": removed_count,
                "state": state.model_dump_json(serialize_as_any=True),
            }
        except ValueError as e:
            logger.warning(f"Fix graph input validation failed: {e}")
            raise HTTPException(status_code=422, detail=client_error_detail(e))
        except Exception as e:
            _reraise_framework_errors(e)
            logger.error(f"Fix graph operation failed: {e}")
            raise HTTPException(status_code=500, detail=f"Fix graph operation failed: {e!s}")
