"""Opt-in ``Idempotency-Key`` for graph runs (invoke, stream, AG-UI and WebSocket).

A gateway that retries a request after a timeout would run the tools twice. A client that sends
an ``Idempotency-Key`` header (WebSocket: ``idempotency_key`` in the run frame) gets this
instead, per user and key:

- the first request runs normally and holds a lease while it runs;
- the same key while that run is still going: 409 with ``Retry-After``;
- the same key after it finished: invoke returns the stored result; stream replays the stored
  final state and the ``done`` chunk as NDJSON; AG-UI and WebSocket answer 409 (their output is
  an event sequence that is not stored);
- the same key with a different request body: 422;
- a run that failed stores nothing, so the same key may be retried.

No header, no change. Records live in Redis when it is configured (the client of the shared
concurrency limiter, keys under ``REDIS_KEY_PREFIX``), else in this process. The running state
is the same kind of lease the thread lock uses, so a crashed worker's key frees itself. A Redis
error is logged and the request runs without idempotency, as the concurrency limiter fails open.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import ClientInputError
from tenxgraph_api.src.app.core.concurrency import (
    Lease,
    RedisConcurrencyLimiter,
    concurrency_limiter,
)
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.redis_client import key_part, prefixed


IDEMPOTENCY_HEADER = "Idempotency-Key"
KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,255}$")
RETRY_AFTER_SECONDS = 5
# A result larger than this is not stored; the key then answers 409 instead of replaying.
MAX_STORED_BYTES = 1024 * 1024
MAX_MEMORY_RECORDS = 10_000

HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422


def idempotency_key_error(value: Any) -> str | None:
    """Why ``value`` is not an acceptable key (1 to 255 printable ASCII characters), or None."""
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value):
        return f"{IDEMPOTENCY_HEADER} must be 1 to 255 printable ASCII characters"
    return None


def fingerprint(kind: str, body: Any) -> str:
    """A stable hash of the request body (JSON is canonicalised), per route kind."""
    if isinstance(body, bytes | bytearray):
        try:
            body = json.loads(body)
        except ValueError:
            body = bytes(body).decode("utf-8", "replace")
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{kind}\n{canonical}".encode()).hexdigest()


class IdempotencyConflict(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=HTTP_CONFLICT,
            detail=detail,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )


class IdempotencyMismatch(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=HTTP_UNPROCESSABLE,
            detail=f"{IDEMPOTENCY_HEADER} was already used with a different request",
        )


_memory: dict[str, tuple[float, str]] = {}


def _redis_client() -> Any | None:
    try:
        from tenxgraph_api.src.app.core.concurrency import shared_concurrency_limiter

        limiter = shared_concurrency_limiter()
    except Exception:
        return None
    return limiter.client if isinstance(limiter, RedisConcurrencyLimiter) else None


async def _load(name: str) -> dict[str, Any] | None:
    client = _redis_client()
    raw: Any = None
    if client is not None:
        try:
            raw = await client.get(name)
        except Exception as exc:
            logger.warning("Idempotency lookup failed; running without it: %s", exc)
            return None
    else:
        entry = _memory.get(name)
        if entry and entry[0] > time.monotonic():
            raw = entry[1]
        elif entry:
            _memory.pop(name, None)
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


async def _save(name: str, record: dict[str, Any]) -> None:
    text = json.dumps(record, separators=(",", ":"), default=str)
    ttl = get_settings().IDEMPOTENCY_TTL_SECONDS
    client = _redis_client()
    if client is not None:
        try:
            await client.set(name, text, ex=ttl)
        except Exception as exc:
            logger.warning("Idempotency record could not be stored: %s", exc)
        return
    now = time.monotonic()
    if len(_memory) >= MAX_MEMORY_RECORDS:
        for stale in [k for k, (exp, _) in _memory.items() if exp <= now]:
            _memory.pop(stale, None)
        while len(_memory) >= MAX_MEMORY_RECORDS:  # still full: drop the oldest
            _memory.pop(next(iter(_memory)))
    _memory[name] = (now + ttl, text)


@dataclass
class IdempotentRun:
    """A request that took its key: finish it with :meth:`complete`, always :meth:`release`."""

    name: str
    fp: str
    lease: Lease
    replay: dict[str, Any] | None = None

    async def complete(self, result: str | None) -> None:
        """Store the finished run's ``result`` (JSON text) so a repeat can be answered.

        ``None``, or a result over ``MAX_STORED_BYTES``, stores a marker only: a repeat then
        answers 409 because there is nothing safe to replay.
        """
        record: dict[str, Any] = {"fp": self.fp, "stored": False}
        if result is not None and len(result) <= MAX_STORED_BYTES:
            record = {"fp": self.fp, "stored": True, "result": result}
        await _save(self.name, record)

    def release(self) -> None:
        self.lease.release()


async def check(
    user: dict[str, Any], key: str, kind: str, body: Any
) -> tuple[IdempotentRun | None, dict[str, Any] | None]:
    """Look the key up. Returns ``(run, None)`` to proceed, or ``(None, record)`` to replay.

    Raises :class:`IdempotencyConflict` (409) while the first run is going, or when it finished
    but nothing is stored; :class:`IdempotencyMismatch` (422) for a different body; and
    ``ClientInputError`` (422) for a malformed key.
    """
    if problem := idempotency_key_error(key):
        raise ClientInputError(problem)
    user_id = user.get("user_id") if isinstance(user, dict) else None
    ident = f"{key_part(user_id or 'anonymous')}:{key_part(key)}"
    name = f"{prefixed('10xgraph:idempotency')}:{ident}"
    fp = fingerprint(kind, body)

    def _answer(record: dict[str, Any]) -> tuple[None, dict[str, Any]]:
        if record.get("fp") != fp:
            raise IdempotencyMismatch
        return None, record

    if (record := await _load(name)) is not None:
        return _answer(record)
    lease = await concurrency_limiter().acquire(f"runs:idem:{ident}", 1)
    if lease is None:
        raise IdempotencyConflict("a request with this Idempotency-Key is still running")
    if (record := await _load(name)) is not None:  # finished while we took the lease
        lease.release()
        return _answer(record)
    return IdempotentRun(name=name, fp=fp, lease=lease), None


def refuse_repeat() -> IdempotencyConflict:
    """The 409 for a finished key on a route that does not replay (AG-UI, WebSocket)."""
    return IdempotencyConflict(
        "a run with this Idempotency-Key already completed; its output is not replayable here"
    )


async def _record_stream(lines: AsyncIterator[str], run: IdempotentRun) -> AsyncIterator[str]:
    """Pass NDJSON ``lines`` through; remember the final state and ``done`` chunk on success.

    A run that errored, was cut off or never sent ``done`` stores nothing and frees the key.
    """
    last_state: str | None = None
    done: str | None = None
    errored = False
    try:
        async for line in lines:
            if line.startswith('{"event":"state"'):
                last_state = line
            elif line.startswith('{"event":"error"'):
                errored = True
            elif '"status":"done"' in line[:200] and line.startswith('{"event":"updates"'):
                done = line
            yield line
        if done is not None and not errored:
            if last_state is not None and len(last_state) > MAX_STORED_BYTES:
                await run.complete(None)
            else:
                await run.complete(json.dumps({"state": last_state, "done": done}))
    finally:
        run.release()


def replay_lines(record: dict[str, Any]) -> list[str]:
    """The NDJSON lines for a stored stream result: final state (if any), then ``done``."""
    stored = json.loads(record["result"])
    return [line for line in (stored.get("state"), stored["done"]) if line]


async def _mark_stream(
    stream: AsyncIterator[Any], run: IdempotentRun, *, failed_marker: str | None = None
) -> AsyncIterator[Any]:
    """Pass ``stream`` through and mark the key completed (no replayable output stored).

    For AG-UI, whose output is an SSE event sequence. An item containing ``failed_marker``
    means the run failed: nothing is marked and the key is free to retry.
    """
    failed = False
    try:
        async for item in stream:
            if failed_marker and isinstance(item, str) and failed_marker in item:
                failed = True
            yield item
        if not failed:
            await run.complete(None)
    finally:
        run.release()


def stream_with_record(lines: AsyncIterator[str], run: IdempotentRun) -> AsyncIterator[str]:
    """``lines`` that remember their final state on success; the key is freed however it ends."""
    wrapped = _record_stream(lines, run)
    weakref.finalize(wrapped, run.release)  # dropped before it was ever iterated
    return wrapped


def mark_when_done(
    stream: AsyncIterator[Any], run: IdempotentRun, *, failed_marker: str | None = None
) -> AsyncIterator[Any]:
    """``stream`` that marks the key completed (nothing replayable) when it ends without error."""
    wrapped = _mark_stream(stream, run, failed_marker=failed_marker)
    weakref.finalize(wrapped, run.release)
    return wrapped
