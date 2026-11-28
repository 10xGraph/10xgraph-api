"""The streaming run: the chunk loop of ``stream_graph`` and its bounded-time wrappers."""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from typing import Any

from tenxgraph.core.authz import Identity, UserCredential, public_config
from tenxgraph.core.graph.utils.stream_handler import is_pause_chunk
from tenxgraph.core.state import StreamChunk, StreamEvent
from tenxgraph.utils import ResponseGranularity
from tenxgraph.utils.interrupt import pending_interrupt
from tenxgraph.utils.validators import ValidationError as InputValidationError

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.audit import audit_event
from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    RunTimeoutError,
)
from tenxgraph_api.src.app.core.shutdown import (
    KEEPALIVE_SECONDS,
    end_on_shutdown,
)
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.run_models import RunChecks, _Run
from tenxgraph_api.src.app.routers.graph.services.stream_utils import (
    _bounded_stream,
    _stream_error_data,
    sanitize_error_chunk,
)
from tenxgraph_api.src.app.utils.parse_output import (
    client_chunk,
)


class StreamingMixin:
    """``GraphService`` methods that stream a run."""

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
        user: Identity | None,
        credential: UserCredential | None = None,
        checks: RunChecks | None = None,
    ) -> AsyncIterable[str]:
        """
        Streams the graph execution with the provided input.

        Args:
            graph_input (GraphInputSchema): The input data for graph execution.
            checks: What the router already checked (``check_run_input``); None checks all.

        Yields:
            str: Individual JSON chunks from graph execution with newline delimiters.
        """

        async def lines() -> AsyncIterable[str]:
            last: StreamChunk | None = None
            errored = False
            async for chunk in self.stream_chunks(
                graph_input, user, credential=credential, checks=checks
            ):
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
        user: Identity | None,
        server_config: dict[str, Any] | None = None,
        credential: UserCredential | None = None,
        checks: RunChecks | None = None,
    ) -> AsyncIterable[StreamChunk]:
        """
        Streams the graph execution as ``StreamChunk`` objects.

        Shared by the NDJSON stream and protocol adapters such as AG-UI. The graph reuses one
        chunk object for successive messages, so consume each chunk before asking for the next.
        ``server_config`` carries server-owned run config (for example per-run
        ``remote_tools``) that a client could not set through ``graph_input.config``.
        ``credential`` is the caller's run credential (``config["credential"]``), None for none.
        ``checks`` is what the router already checked (``check_run_input``).

        Yields:
            StreamChunk: Graph output; failures arrive as an ``ERROR`` chunk, never raised.
        """
        run = _Run()
        naming: tuple[dict[str, Any], Any, list[str]] | None = None
        try:
            logger.debug("Streaming graph with input: %s", log_safe(graph_input.messages))
            await self._start_run(run, graph_input, user, server_config, credential, checks)
            input_data, config, meta = run.input_data, run.config, run.meta
            thread_id, run_id = run.thread_id, run.run_id
            messages_str = []
            run_status = "done"

            # Stream the graph execution
            saw_pause = False
            async for raw_chunk in self._astream_bounded(
                input_data, config, graph_input.response_granularity
            ):
                chunk = client_chunk(sanitize_error_chunk(raw_chunk), is_production())
                mt = chunk.metadata or {}
                mt.update(meta)
                chunk.metadata = mt
                self._record_chunk(thread_id, run_id, chunk, datetime.now().timestamp())
                if is_pause_chunk(chunk):
                    saw_pause = True
                if getattr(chunk, "is_error", False) or getattr(chunk, "event", None) in (
                    "error",
                    StreamEvent.ERROR,
                ):
                    run_status = "error"
                if chunk.event in (StreamEvent.MESSAGE, StreamEvent.ERROR):
                    # The raw chunk: production sanitizing replaces a denial's reason. Before
                    # the yield, so a client that disconnects here still leaves the record.
                    await run.tools.record(raw_chunk)
                yield chunk
                if (
                    self.names_threads
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

            await run.tools.after_run(self.checkpointer, config)
            logger.info("Graph streaming completed successfully")

            if self.telemetry:
                self.telemetry.finish_run(thread_id, run_id, datetime.now().timestamp(), run_status)

            if meta["is_new_thread"] and self.names_threads:
                naming = (
                    public_config(config),
                    config["thread_id"],
                    self.naming_texts(input_data["messages"]) + messages_str,
                )

        except Exception as e:
            # Never raise HTTPException from inside an async generator that is
            # consumed by StreamingResponse.  By the time any exception is raised
            # from within the generator body, Starlette has already committed to a
            # 200 OK response and cannot replace it with a 4xx/5xx.  Raising here
            # would cause: RuntimeError("Caught handled exception, but response
            # already started.")  Instead, we yield a structured error chunk so
            # the client can detect and display the failure.
            if isinstance(e, InputValidationError):
                logger.warning("Graph stream rejected by an input validator: %s", log_safe(e))
                await audit_event("input", "denied", reason="validation")
            else:
                logger.error(f"Graph streaming failed: {e}")
            try:
                if self.telemetry and run.thread_id and run.run_id:
                    self.telemetry.finish_run(
                        run.thread_id, run.run_id, datetime.now().timestamp(), "error"
                    )
            except Exception as telemetry_exc:
                logger.debug("Telemetry finish_run failed on stream error: %s", telemetry_exc)
            yield StreamChunk(
                event=StreamEvent.ERROR,
                data=_stream_error_data(e),
                metadata=run.meta,
            )
        finally:
            self._end_run(run)
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
        pending = pending_interrupt(state)
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
