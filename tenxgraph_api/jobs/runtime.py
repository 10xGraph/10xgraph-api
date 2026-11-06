"""The jobs runtime: the store, transport, clock and hooks enqueue and the worker use.

Sprint 3 builds it from the ``worker`` section of ``10xgraph.json``; until then it is bound by
``jobs_harness`` or by hand with ``use_runtime``.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from .clock import SystemClock
from .errors import JobsNotConfigured
from .hooks import HookRunner
from .store.base import BaseRunStore
from .transport.base import BaseTransport


DEFAULT_MAX_KWARGS_BYTES = 256 * 1024
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
DEFAULT_RESULT_TTL = 604800.0
DEFAULT_LEASE_TTL = 60.0


def _new_run_id() -> str:
    return uuid.uuid4().hex


class JobsRuntime(BaseModel):
    """Everything the jobs code needs at run time.

    ``clock`` is a sync clock for timestamps written outside the store (progress events); the
    store keeps its own clock for leases and ``available_at``. ``hooks`` are
    ``TaskLifecycleHook`` instances or any object with some of its methods.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: BaseRunStore
    transport: BaseTransport
    hooks: Sequence[Any] = ()
    clock: Callable[[], float] = Field(default_factory=SystemClock)
    max_kwargs_bytes: int = DEFAULT_MAX_KWARGS_BYTES
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    result_ttl: float | None = DEFAULT_RESULT_TTL
    lease_ttl: float = DEFAULT_LEASE_TTL
    new_run_id: Callable[[], str] = _new_run_id

    _hook_runner: HookRunner = PrivateAttr()

    def model_post_init(self, context: Any, /) -> None:
        self._hook_runner = HookRunner(self.hooks)

    @property
    def hook_runner(self) -> HookRunner:
        return self._hook_runner


_current: JobsRuntime | None = None


def get_runtime() -> JobsRuntime:
    if _current is None:
        raise JobsNotConfigured(
            "jobs are not configured: add a `worker` section to 10xgraph.json, or use "
            "tenxgraph_api.jobs.testing.jobs_harness() in tests"
        )
    return _current


def set_runtime(runtime: JobsRuntime | None) -> JobsRuntime | None:
    """Bind ``runtime`` process-wide and return the previous one."""
    global _current  # noqa: PLW0603 - one runtime per process by design
    previous = _current
    _current = runtime
    return previous


@contextlib.contextmanager
def use_runtime(runtime: JobsRuntime) -> Iterator[JobsRuntime]:
    """Bind ``runtime`` for the ``with`` block, then restore the previous one."""
    previous = set_runtime(runtime)
    try:
        yield runtime
    finally:
        set_runtime(previous)
