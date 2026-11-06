"""``ctx.emit``: progress from inside a task.

Shaped like core's ``StreamEmitter`` (``tenxgraph/core/state/stream_emitter.py``): plain sync
methods that are safe from the event loop and from threads. Calls only write to an in-memory
buffer under a lock; the worker takes the buffer with ``take()`` and flushes it to the store
(fenced, at most once a second per run, and once more when the attempt ends).

What becomes an event: every ``steps``, ``eta``, ``log`` call and every step start and end.
``progress`` calls are coalesced into one ``progress`` event per flush (the latest message and
percent), so a tight loop does not fill the 500-event log.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any

from .models import EventIn


LEVELS = ("debug", "info", "warning", "error")
FULL = 100.0


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class TaskEmitter:
    """Buffered progress recorder for one attempt."""

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._events: list[EventIn] = []
        self._pending: dict[str, Any] | None = None
        self._message: str | None = None
        self._percent: float | None = None
        self._eta: float | None = None
        self._current: str | None = None
        self._steps: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._closed = False

    # ------------------------------------------------------------------ public API

    def steps(self, names: Sequence[str]) -> None:
        """Declare the planned steps up front; they show as pending."""
        with self._lock:
            if self._closed:
                return
            for name in names:
                self._step_record(str(name))
            self._event("steps", {"steps": [str(n) for n in names]})

    def step(self, name: str, weight: float | None = None) -> _Step:
        """Context manager (``with`` or ``async with``) recording a step's start, end,
        duration and outcome. With weights, percent follows the finished weights."""
        if weight is not None and not 0 <= weight <= 1:
            raise ValueError("step weight must be between 0 and 1")
        return _Step(self, str(name), weight)

    def progress(
        self,
        message: str | None = None,
        percent: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Latest message and percent (0 to 100, never lower than the last value shown)."""
        with self._lock:
            if self._closed:
                return
            if message is not None:
                self._message = str(message)
            if percent is not None:
                self._raise_percent(float(percent))
            self._pending = {"message": self._message, "percent": self._percent}
            if data is not None:
                self._pending["data"] = _json(data)
            self._dirty = True

    def eta(self, seconds: float) -> None:
        """Estimated seconds left."""
        with self._lock:
            if self._closed:
                return
            self._eta = max(0.0, float(seconds))
            self._event("eta", {"seconds": self._eta})

    def log(self, message: str, level: str = "info", data: dict[str, Any] | None = None) -> None:
        """A free event line."""
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        with self._lock:
            if self._closed:
                return
            payload: dict[str, Any] = {"message": str(message), "level": level}
            if data is not None:
                payload["data"] = _json(data)
            self._event("log", payload)

    # ------------------------------------------------------------------ worker side

    @property
    def dirty(self) -> bool:
        return self._dirty

    def take(self) -> tuple[list[EventIn], dict[str, Any]] | None:
        """Pending events and the current snapshot, clearing the buffer; None if clean."""
        with self._lock:
            if not self._dirty:
                return None
            events = self._events
            if self._pending is not None:
                events.append(EventIn(type="progress", at=self._clock(), data=self._pending))
            self._events = []
            self._pending = None
            self._dirty = False
            return events, self._snapshot()

    def close(self) -> None:
        """Ignore every later call (a straggling thread after the attempt ended)."""
        with self._lock:
            self._closed = True

    # ------------------------------------------------------------------ internals

    def _snapshot(self) -> dict[str, Any]:
        return {
            "message": self._message,
            "percent": self._percent,
            "step": self._current,
            "steps": [dict(s) for s in self._steps.values()],
            "eta": self._eta,
        }

    def _event(self, kind: str, data: dict[str, Any]) -> None:
        self._events.append(EventIn(type=kind, at=self._clock(), data=data))
        self._dirty = True

    def _raise_percent(self, value: float) -> None:
        value = max(0.0, min(FULL, value))
        if self._percent is None or value > self._percent:
            self._percent = value

    def _step_record(self, name: str) -> dict[str, Any]:
        record = self._steps.get(name)
        if record is None:
            record = {
                "name": name,
                "state": "pending",
                "weight": None,
                "started_at": None,
                "finished_at": None,
                "duration": None,
            }
            self._steps[name] = record
        return record

    def _start(self, name: str, weight: float | None) -> None:
        with self._lock:
            if self._closed:
                return
            record = self._step_record(name)
            if weight is not None:
                record["weight"] = weight
            record["state"] = "running"
            record["started_at"] = self._clock()
            self._current = name
            self._event("step.start", {"step": name})

    def _end(self, name: str, failed: bool) -> None:
        with self._lock:
            if self._closed:
                return
            record = self._step_record(name)
            now = self._clock()
            record["state"] = "failed" if failed else "succeeded"
            record["finished_at"] = now
            started = record["started_at"]
            record["duration"] = None if started is None else max(0.0, now - started)
            if self._current == name:
                self._current = None
            weights = [s["weight"] for s in self._steps.values() if s["weight"] is not None]
            if weights:
                done = sum(
                    s["weight"]
                    for s in self._steps.values()
                    if s["weight"] is not None and s["state"] == "succeeded"
                )
                self._raise_percent(done * FULL)
            self._event(
                "step.end",
                {
                    "step": name,
                    "outcome": record["state"],
                    "duration": record["duration"],
                    "percent": self._percent,
                },
            )


class _Step:
    """``with ctx.emit.step(name):`` or ``async with ctx.emit.step(name):``."""

    def __init__(self, emitter: TaskEmitter, name: str, weight: float | None) -> None:
        self._emitter = emitter
        self._name = name
        self._weight = weight

    def __enter__(self) -> _Step:
        self._emitter._start(self._name, self._weight)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._emitter._end(self._name, failed=exc_type is not None)

    async def __aenter__(self) -> _Step:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)
