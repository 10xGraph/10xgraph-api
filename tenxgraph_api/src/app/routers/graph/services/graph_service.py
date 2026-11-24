import asyncio

# Names this module offered before it was split stay importable from it.
import contextlib  # noqa: F401
import inspect  # noqa: F401
import sys
from collections.abc import (  # noqa: F401
    AsyncIterable,
    AsyncIterator,
    Iterator,
)
from dataclasses import (  # noqa: F401
    dataclass,
)
from dataclasses import (  # noqa: F401
    field as dataclass_field,
)
from datetime import datetime
from http import (  # noqa: F401
    HTTPStatus,
)
from typing import Any
from uuid import uuid4

from injectq import (  # noqa: F401
    InjectQ,
    inject,
    singleton,
)
from pydantic import (  # noqa: F401
    BaseModel,
    TypeAdapter,
    ValidationError,
)
from tenxgraph.core.authz import Identity, UserCredential, public_config
from tenxgraph.core.exceptions import (  # noqa: F401
    ModelNotFoundError,
)
from tenxgraph.core.graph import (  # noqa: F401
    CompiledGraph,
    ToolNode,
)
from tenxgraph.core.graph.utils.stream_handler import (  # noqa: F401
    is_pause_chunk,
)
from tenxgraph.core.state import AgentState, Message, StreamChunk, StreamEvent
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.utils import (  # noqa: F401
    ResponseGranularity,
)
from tenxgraph.utils.interrupt import pending_interrupt  # noqa: F401
from tenxgraph.utils.validators import (  # noqa: F401
    ValidationError as InputValidationError,
)

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.audit import (  # noqa: F401
    audit_event,
)
from tenxgraph_api.src.app.core.audit.tools import ToolAudit
from tenxgraph_api.src.app.core.config.graph_config import (  # noqa: F401
    GraphConfig,
    RunsConfig,
)
from tenxgraph_api.src.app.core.config.settings import (  # noqa: F401
    get_settings,
    is_production,
)
from tenxgraph_api.src.app.core.exceptions.client_errors import (  # noqa: F401
    RunTimeoutError,
    as_http_error,
    client_error,
    generic_error_text,
)
from tenxgraph_api.src.app.core.exceptions.client_errors import (  # noqa: F401
    resume_error_types as _resume_error_types,
)
from tenxgraph_api.src.app.core.leases import Lease
from tenxgraph_api.src.app.core.shutdown import (  # noqa: F401
    KEEPALIVE_SECONDS,
    cancel_and_wait,
    end_on_shutdown,
)
from tenxgraph_api.src.app.core.threads import (  # noqa: F401
    require_thread,
    set_user,
)
from tenxgraph_api.src.app.core.utils.container import lookup
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (  # noqa: F401
    ALWAYS_ALLOWED_CONFIG_KEYS,
    ClientInputError,
    InterruptConflictError,
    _field,
    _tool_calls,
    client_config,
    normalize_thread_id,
    owned_thread_info,
    reject_builtin_state_keys,
    reject_unlisted_keys,
    tool_result_ids,
    unanswered_tool_calls,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (  # noqa: F401
    GraphInputSchema,
    GraphInvokeOutputSchema,
    GraphSchema,
    GraphToolsSchema,
    ObservabilitySchema,
    ObsRunSchema,
    ToolNodeSchema,
    ToolSchema,
)
from tenxgraph_api.src.app.routers.graph.services.fix import FixMixin
from tenxgraph_api.src.app.routers.graph.services.introspection import IntrospectionMixin
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (  # noqa: F401
    check_external_media_urls,
    internal_media_keys,
    preprocess_multimodal_messages,
    resolve_media_policy,
)
from tenxgraph_api.src.app.routers.graph.services.naming import (  # noqa: F401
    THREAD_NAME_TIMEOUT_SECONDS,
    ThreadNamingMixin,
)
from tenxgraph_api.src.app.routers.graph.services.realtime import (  # noqa: F401
    REALTIME_INIT_FIELDS,
    RealtimeMixin,
)
from tenxgraph_api.src.app.routers.graph.services.run_checks import (  # noqa: F401
    MAX_THREAD_ID_ATTEMPTS,
    RunChecksMixin,
)
from tenxgraph_api.src.app.routers.graph.services.run_models import (  # noqa: F401
    _UNREAD,
    RunChecks,
    _resume_http_error,
    _Run,
    _run_tool_names,
)
from tenxgraph_api.src.app.routers.graph.services.stream_utils import (  # noqa: F401
    _ERROR_TEXT_FIELDS,
    _STREAM_DONE,
    SERVICE_UNAVAILABLE_CODE,
    _bounded_stream,
    _log_run_error,
    _stream_error_data,
    sanitize_error_chunk,
)
from tenxgraph_api.src.app.routers.graph.services.streaming import StreamingMixin
from tenxgraph_api.src.app.routers.graph.thread_lock import acquire_thread_lock
from tenxgraph_api.src.app.utils import ThreadNameGenerator
from tenxgraph_api.src.app.utils.parse_output import (  # noqa: F401
    client_chunk,
    client_message,
    parse_state_output,
)
from tenxgraph_api.src.app.utils.run_trace import (  # noqa: F401
    reconstruct_run,
    telemetry_record,
)
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore


@singleton
class GraphService(
    RunChecksMixin,
    StreamingMixin,
    RealtimeMixin,
    IntrospectionMixin,
    FixMixin,
    ThreadNamingMixin,
):
    """
    Service class for graph-related operations.

    This class acts as an intermediary between the controllers and the
    CompiledGraph, facilitating graph execution operations.
    """

    # The graph is fixed after boot: the model choices are read once, on first use.

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
        """
        return bool(self._graph.is_realtime())

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
            self._telemetry = lookup(TelemetryStore)
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
        user: Identity | None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Stop the graph execution for a specific thread.

        Core ``astop`` reads the identity from ``config["user"]``: it stops only runs whose
        owner the caller's reach covers and answers ``reason: "no-state"`` for a thread the
        caller cannot reach, exactly as for one with no run.

        Args:
            thread_id (str): The thread ID to stop
            user: The caller's identity, or None when auth is off
            config (dict, optional): Additional configuration for the stop operation

        Returns:
            dict: Stop result with status information

        Raises:
            HTTPException: If stop operation fails or user doesn't have permission.
        """
        try:
            logger.info("Stopping graph execution for thread: %s", log_safe(thread_id))
            logger.debug("User id: %s", log_safe(user.user_id if user else None))

            # Start with client config if provided, then overlay trusted attributes
            stop_config = client_config(config)
            stop_config["thread_id"] = thread_id
            set_user(stop_config, user)

            # Call the graph's astop method
            result = await self._graph.astop(stop_config)

            logger.info(
                "Graph stop completed for thread %s: %s", log_safe(thread_id), log_safe(result)
            )
            return result

        except Exception as e:
            what = f"Graph stop failed for thread {thread_id}"
            _log_run_error(e, what)
            raise as_http_error(e, what)

    async def invoke_graph(
        self,
        graph_input: GraphInputSchema,
        user: Identity | None,
        credential: UserCredential | None = None,
        checks: RunChecks | None = None,
    ) -> GraphInvokeOutputSchema:
        """
        Invokes the graph with the provided input and returns the final result.

        Args:
            graph_input (GraphInputSchema): The input data for graph execution.
            checks: What the router already checked (``check_run_input``); None checks all.

        Returns:
            GraphInvokeOutputSchema: The final result from graph execution.

        Raises:
            HTTPException: If graph execution fails.
        """
        run = _Run()
        try:
            logger.debug("Invoking graph with input: %s", log_safe(graph_input.messages))
            await self._start_run(run, graph_input, user, None, credential, checks)
            input_data, config, meta = run.input_data, run.config, run.meta
            inv_thread_id, inv_run_id, inv_started = run.thread_id, run.run_id, run.started

            # Execute the graph
            result = await self._ainvoke_bounded(
                input_data, config, graph_input.response_granularity
            )

            logger.info("Graph execution completed successfully")

            # Extract messages and state from result
            messages: list[Message] = result.get("messages", [])
            for msg in messages:
                await run.tools.record(msg)
            await run.tools.after_run(self.checkpointer, config)

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

            if meta["is_new_thread"] and self.names_threads:
                # Naming can be an extra model call; the caller should not wait for it. The
                # name is saved on the thread (GET /v1/threads) once it is ready.
                self._name_thread_in_background(
                    public_config(config),
                    config["thread_id"],
                    self.naming_texts(input_data["messages"], messages),
                )

            return GraphInvokeOutputSchema(
                messages=[client_message(m) for m in messages],
                state=parse_state_output(get_settings(), raw_state) if raw_state else None,
                context=[client_message(m) for m in context] if context else context,
                summary=context_summary,
                meta=meta,
            )

        except Exception as e:
            # The same mapping as the stream's error chunk: typed and HTTP errors reach their
            # registered handlers, the rest become an HTTPException.
            _log_run_error(e, "Graph execution failed")
            raise as_http_error(e, "Graph execution failed")
        finally:
            self._end_run(run)

    async def _start_run(
        self,
        run: _Run,
        graph_input: GraphInputSchema,
        user: Identity | None,
        server_config: dict[str, Any] | None,
        credential: UserCredential | None,
        checks: RunChecks | None,
    ) -> None:
        """The steps before every run (invoke, stream, WebSocket, AG-UI), filled into ``run``.

        Fills ``run`` as it goes, so the caller's ``_end_run`` frees whatever was taken when
        a step fails. The thread's state is read at most once (``RunChecks.state``).
        """
        checks = checks or RunChecks()
        run.input_data, run.config, run.meta = await self._prepare_input(
            graph_input, user, server_config, credential, checks
        )
        run.lock = await self._lock_new_thread(run.meta)
        await self._check_tool_results(run.input_data["messages"], run.config, checks)
        await self._check_resume_target(graph_input, run.config, checks)

        # Saved for existing threads too: it refreshes the last-access time, and True means
        # the checkpointer created the thread just now.
        if await self._save_thread(run.config, run.config["thread_id"]) is True:
            run.meta["is_new_thread"] = True

        # A telemetry run, so the observability endpoint can rebuild the trace.
        run.thread_id = str(run.config["thread_id"])
        run.run_id = str(run.config.get("run_id") or uuid4())
        run.config.setdefault("run_id", run.run_id)
        run.started = datetime.now().timestamp()
        if self.telemetry:
            self.telemetry.start_run(run.thread_id, run.run_id, run.started)
        run.tools = ToolAudit(user, run.thread_id, run.run_id)
        # The client's results for its own tools: those tools ran in the frontend.
        run.tools.skip.update(tool_result_ids(run.input_data["messages"]))
        if "resume" in run.input_data:
            state = await self._run_state(run.config, checks)
            await run.tools.approval(state, run.input_data["resume"])
            await run.tools.before_resume(self.checkpointer, run.config, state)

    def _end_run(self, run: _Run) -> None:
        """Close the run's trace if nothing else did, then free the lock on a new thread."""
        self._close_run_trace(run.thread_id, run.run_id)
        run.lock.release()

    @staticmethod
    async def _lock_new_thread(meta: dict[str, Any]) -> Lease:
        """Lock a thread this request just created. The router locks the ones a client names.

        Nobody is running on a new thread yet, but the client learns its id from the first
        chunk and could start a second run on it while this one is still going.
        """
        if not meta.get("is_new_thread"):
            return Lease(None)
        return await acquire_thread_lock(str(meta["thread_id"]))
