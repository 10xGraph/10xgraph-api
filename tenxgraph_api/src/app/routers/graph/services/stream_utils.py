"""Helpers of the stream path: error chunks, their sanitizing, and the bounded stream."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.settings import is_production
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    RunTimeoutError,
    client_error,
    generic_error_text,
)
from tenxgraph_api.src.app.core.shutdown import (
    cancel_and_wait,
)
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe


_STREAM_DONE = object()


# ``data.code`` of the error chunk for a run refused while the graph is closing.
SERVICE_UNAVAILABLE_CODE = "SERVICE_UNAVAILABLE"


def _stream_error_data(exc: Exception) -> dict[str, Any]:
    """The ``data`` of a stream's terminal ERROR chunk: the code and message of the HTTP answer.

    The global error handlers never see a stream failure (it is caught inside the generator),
    so the chunk takes the same ``client_error`` mapping as invoke: the same code and message
    on every transport, generic in production unless the message is written for the client.
    """
    error = client_error(exc, "Graph execution failed")
    data: dict[str, Any] = {"reason": error.message, "code": error.error_code}
    if error.retry_after is not None:
        data["retry_after_seconds"] = error.retry_after
    return data


def _log_run_error(exc: Exception, what: str) -> None:
    """A client mistake is one WARNING line; anything else is an ERROR."""
    if client_error(exc, what).status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
        logger.warning("%s: %s", what, log_safe(exc))
    else:
        logger.error("%s: %s", what, log_safe(exc))


# Chunk ``data`` fields that carry exception text. ``message`` is what a tool's own
# ``emitter.error()`` sends.
_ERROR_TEXT_FIELDS = ("reason", "error", "message")


def sanitize_error_chunk(chunk: StreamChunk) -> StreamChunk:
    """In production, replace the exception text in an ``ERROR`` chunk with a generic message.

    Core puts the raw exception in ``data.reason`` / ``data.error`` (and ``data.message`` for a
    tool's own error), and in ``state.execution_meta.internal_data["error"]``. The plain HTTP
    invoke path is already generic; this is the one step the stream, WebSocket and AG-UI
    paths share (they all read ``stream_chunks``). Development, and every other chunk, passes
    through unchanged. Returns a copy: the graph reuses and checkpoints its own objects.
    """
    if chunk.event != StreamEvent.ERROR or not is_production():
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
            # Waits for the run's cancel save before the caller frees the thread lock.
            await cancel_and_wait(producer)
