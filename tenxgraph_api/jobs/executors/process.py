"""``process`` executor: one supervised child process per slot (``--processes``).

Why not ``ProcessPoolExecutor``: killing one pool worker breaks the whole pool (JOBS_SPRINT0.md
section 5). Each slot here owns one ``multiprocessing.Process`` and one ``Pipe``:

- Start method: ``forkserver`` with ``set_forkserver_preload(preload)`` (the task modules, so a
  new child costs a few ms), ``spawn`` where forkserver is missing (macOS, Windows). Never
  ``fork``: the worker has a loop and threads.
- A task runs by registry name: the child imports ``fn.__module__`` and resolves
  ``fn.__qualname__`` to the ``Task``. Kwargs and results cross the pipe in JSON form; the
  child loads kwargs with ``task.load_kwargs`` and dumps the result with ``task.dump_result``.
- ``ctx`` in the child is a ``ProcessTaskContext``: ``run_id``, ``attempt``, ``task``,
  ``queue``, ``cancelled``, ``emit``, ``thread_id()``. No ``Inject``, no ``invoke_graph``.
  Emit calls go over the pipe and are replayed into the attempt's ``TaskEmitter``.
- Cancel sets a flag in shared memory (a lock-free ``RawValue``: an ``mp.Event`` holds a lock
  that a killed child could leave taken). The child is killed ``cancel_grace`` s later if it
  is still running.
- Timeout, ``kill``, memory limit: ``Process.kill()`` (SIGKILL), the pipe is discarded, a new
  child is started on the next run. A child that dies by itself (exit, segfault) fails that
  attempt with ``crashed`` and the slot restarts.
- Recycling: after ``max_tasks_per_child`` runs, or when the child's resident memory is over
  ``max_memory_per_child`` MB after a run, it is asked to exit (``None`` message) and replaced.
  Over the limit during a run it is killed (``memory_limit``). Memory is read from
  ``/proc/<pid>/statm`` (Linux), psutil elsewhere, else the limit is off (one warning).
- A child that cannot start (dies or reports an error before it is ready) fails the attempt
  with ``child_start``; the slot waits 1 s, then 2, 4 ... up to 60 s before trying again and
  fails runs at once meanwhile, so a broken deploy does not loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import importlib
import json
import logging
import math
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Sequence
from multiprocessing.connection import Connection
from pathlib import Path
from types import TracebackType
from typing import Any

from tenxgraph_api.jobs.context import THREAD_NAMESPACE
from tenxgraph_api.jobs.control import Fail, RetryAfter
from tenxgraph_api.jobs.emit import LEVELS
from tenxgraph_api.jobs.errors import PayloadTooLarge, TaskDefinitionError, TaskValidationError
from tenxgraph_api.jobs.task import Task

from .base import (
    ChildTaskError,
    ExecutionHandle,
    ExecutionResult,
    ExecutionSpec,
    Executor,
    error_dict,
)


logger = logging.getLogger("tenxgraph_api.jobs")

#: Always preloaded: the child entry point and what it imports.
CHILD_MODULE = __name__
#: Seconds between memory and liveness checks while a run is in a child.
MEMORY_POLL = 0.05
ALIVE_POLL = 0.5
#: A coalesced ``progress`` call is sent at most this often (other emit calls go at once).
PROGRESS_SEND_INTERVAL = 0.1
STARTUP_TIMEOUT = 60.0
STOP_TIMEOUT = 5.0
RESTART_BACKOFF_MAX = 60.0


# ====================================================================== shared helpers


def default_start_method() -> str:
    """``forkserver`` when the platform has it, else ``spawn``. Never ``fork``."""
    return "forkserver" if "forkserver" in mp.get_all_start_methods() else "spawn"


def resolve_qualname(module_name: str, qualname: str) -> Any:
    module = importlib.import_module(module_name)
    obj: Any = module
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def resolve_task(module_name: str, qualname: str, name: str) -> Task:
    """The ``Task`` that ``module:qualname`` names. Raises ``TaskDefinitionError``."""
    try:
        obj = resolve_qualname(module_name, qualname)
    except Exception as exc:
        raise TaskDefinitionError(
            f"process task {name!r}: cannot import {module_name}:{qualname}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if isinstance(obj, Task) and obj.name == name:
        return obj
    raise TaskDefinitionError(
        f"process task {name!r}: {module_name}:{qualname} is not that task "
        f"(got {type(obj).__name__}); a process task must be a module-level @task function"
    )


_rss_warned = False


def rss_mb(pid: int) -> float | None:
    """Resident memory of ``pid`` in MB, None when it cannot be read."""
    global _rss_warned  # noqa: PLW0603 - one warning per process
    try:
        pages = int(Path(f"/proc/{pid}/statm").read_bytes().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.Process(pid).memory_info().rss) / (1024 * 1024)
    except ImportError:
        if not _rss_warned:
            _rss_warned = True
            logger.warning("max_memory_per_child is off: no /proc and psutil is not installed")
        return None
    except Exception:
        return None


def describe_exit(code: int | None) -> str:
    if code is None:
        return "is not running"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"was killed by {name} ({code})"
    return f"exited with code {code}"


# ====================================================================== child side


class ChildEmitter:
    """``ctx.emit`` in a child: the ``TaskEmitter`` API, each call sent to the parent."""

    def __init__(self, send: Any) -> None:
        self._send = send
        self._progress: dict[str, Any] | None = None
        self._progress_sent = 0.0

    def steps(self, names: Sequence[str]) -> None:
        self._out("steps", [str(n) for n in names])

    def step(self, name: str, weight: float | None = None) -> _ChildStep:
        if weight is not None and not 0 <= weight <= 1:
            raise ValueError("step weight must be between 0 and 1")
        return _ChildStep(self, str(name), weight)

    def progress(
        self,
        message: str | None = None,
        percent: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        pending = self._progress or {"message": None, "percent": None, "data": None}
        if message is not None:
            pending["message"] = str(message)
        if percent is not None:
            pending["percent"] = max(float(percent), pending["percent"] or -math.inf)
        if data is not None:
            pending["data"] = _plain(data)
        self._progress = pending
        if time.monotonic() - self._progress_sent >= PROGRESS_SEND_INTERVAL:
            self.flush()

    def eta(self, seconds: float) -> None:
        self._out("eta", float(seconds))

    def log(self, message: str, level: str = "info", data: dict[str, Any] | None = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        self._out("log", str(message), level, None if data is None else _plain(data))

    def flush(self) -> None:
        pending, self._progress = self._progress, None
        if pending is not None:
            self._progress_sent = time.monotonic()
            self._send(
                ("emit", "progress", (pending["message"], pending["percent"], pending["data"]))
            )

    def _out(self, method: str, *args: Any) -> None:
        self.flush()
        self._send(("emit", method, args))


class _ChildStep:
    def __init__(self, emitter: ChildEmitter, name: str, weight: float | None) -> None:
        self._emitter = emitter
        self._name = name
        self._weight = weight

    def __enter__(self) -> _ChildStep:
        self._emitter._out("start", self._name, self._weight)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._emitter._out("end", self._name, exc_type is not None)

    async def __aenter__(self) -> _ChildStep:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class ProcessTaskContext:
    """The smaller ``ctx`` of a ``process`` task: no ``identity``, ``Inject`` or graph."""

    def __init__(
        self, *, run_id: str, attempt: int, task: str, queue: str, cancel_flag: Any, emit: Any
    ) -> None:
        self.run_id = run_id
        self.attempt = attempt
        self.task = task
        self.queue = queue
        self.emit = emit
        self._cancel_flag = cancel_flag

    @property
    def cancelled(self) -> bool:
        return bool(self._cancel_flag.value)

    def thread_id(self) -> str:
        return str(uuid.uuid5(THREAD_NAMESPACE, self.run_id))

    async def invoke_graph(self, input_data: dict[str, Any], config: dict | None = None) -> Any:
        raise NotImplementedError("ctx.invoke_graph is not available in a process task")

    def __repr__(self) -> str:
        return f"ProcessTaskContext(run_id={self.run_id!r}, task={self.task!r})"


def _watch_parent(parent_pid: int) -> None:
    """Exit when the worker is gone (the child's OS parent is the forkserver)."""
    while True:
        time.sleep(1.0)
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            os._exit(1)
        except PermissionError:  # pragma: no cover - pid reused by another user
            os._exit(1)


def child_main(conn: Connection, cancel_flag: Any, preload: list[str], parent_pid: int) -> None:
    """Entry point of a slot's child process."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the worker decides about Ctrl+C
    threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()
    try:
        for name in preload:
            importlib.import_module(name)
    except BaseException as exc:
        with contextlib.suppress(Exception):
            conn.send(("startup_error", f"cannot import {name}: {type(exc).__name__}: {exc}"))
        return
    conn.send(("ready", os.getpid()))
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if message is None:
            return
        reply = _run_child_task(conn, message[1], cancel_flag)
        try:
            conn.send(reply)
        except (OSError, ValueError):
            return


def _run_child_task(  # noqa: PLR0911 - one reply per outcome
    conn: Connection, payload: dict[str, Any], cancel_flag: Any
) -> tuple:
    try:
        task_obj = resolve_task(payload["module"], payload["qualname"], payload["name"])
    except TaskDefinitionError as exc:
        return ("error", {"kind": "import", "type": "ImportError", "message": str(exc)})
    try:
        kwargs = task_obj.load_kwargs(payload["kwargs"])
    except TaskValidationError as exc:
        return ("error", {"kind": "validation", "type": type(exc).__name__, "message": str(exc)})
    emitter = ChildEmitter(conn.send)
    if task_obj.context_param is not None:
        kwargs[task_obj.context_param] = ProcessTaskContext(
            run_id=payload["run_id"],
            attempt=payload["attempt"],
            task=task_obj.name,
            queue=payload["queue"],
            cancel_flag=cancel_flag,
            emit=emitter,
        )
    try:
        value = task_obj.fn(**kwargs)
    except Fail as exc:
        emitter.flush()
        return ("error", {"kind": "fail", "message": exc.message, "data": _plain(exc.data)})
    except RetryAfter as exc:
        emitter.flush()
        return ("error", {"kind": "retry_after", "seconds": exc.seconds})
    except BaseException as exc:
        emitter.flush()
        return (
            "error",
            {
                "kind": "error",
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=20),
            },
        )
    emitter.flush()
    try:
        data = task_obj.dump_result(value, max_bytes=payload["max_result_bytes"])
    except TaskValidationError as exc:
        kind = "result_too_large" if isinstance(exc, PayloadTooLarge) else "result_invalid"
        return ("error", {"kind": kind, "type": type(exc).__name__, "message": str(exc)})
    return ("result", data)


# ====================================================================== parent side


def _child_error_result(info: dict[str, Any]) -> ExecutionResult:
    kind = info.get("kind")
    if kind == "fail":
        exc: BaseException = Fail(info.get("message", ""), info.get("data"))
        error = error_dict("fail", "Fail", info.get("message", ""))
        if info.get("data") is not None:
            error["data"] = info["data"]
        return ExecutionResult(kind="raised", exception=exc, error=error)
    if kind == "retry_after":
        retry = RetryAfter(info["seconds"])
        return ExecutionResult(
            kind="raised", exception=retry, error=error_dict("error", "RetryAfter", str(retry))
        )
    if kind == "import":
        return ExecutionResult(
            kind="crashed", error=error_dict("child_import", info["type"], info["message"])
        )
    if kind in ("validation", "result_invalid", "result_too_large"):
        exc_type = PayloadTooLarge if kind == "result_too_large" else TaskValidationError
        return ExecutionResult(
            kind="invalid",
            exception=exc_type(info["message"]),
            error=error_dict(kind, info["type"], info["message"]),
        )
    child = ChildTaskError(info["type"], info["message"], info.get("traceback", ""))
    return ExecutionResult(
        kind="raised", exception=child, error=error_dict("error", info["type"], info["message"])
    )


class _ChildStartError(Exception):
    pass


class _Slot:
    """One child process and its pipe; runs one attempt at a time."""

    def __init__(self, executor: ProcessExecutor, index: int) -> None:
        self.executor = executor
        self.index = index
        self.proc: Any = None
        self.conn: Connection | None = None
        self.cancel_flag: Any = None
        self.runs = 0
        self.failures = 0
        self.retry_at = 0.0
        self.last_error = ""

    @property
    def pid(self) -> int | None:
        return None if self.proc is None else self.proc.pid

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    async def ensure_child(self) -> None:
        if self.alive():
            return
        await self.discard()
        now = time.monotonic()
        if now < self.retry_at:
            raise _ChildStartError(
                f"process child of slot {self.index} cannot start: {self.last_error} "
                f"(next try in {self.retry_at - now:.1f} s)"
            )
        try:
            await self._spawn()
        except _ChildStartError as exc:
            self.failures += 1
            self.last_error = str(exc)
            self.retry_at = time.monotonic() + min(RESTART_BACKOFF_MAX, 2.0 ** (self.failures - 1))
            logger.error("jobs process slot %s: %s", self.index, exc)
            await self.discard()
            raise
        self.failures = 0

    async def _spawn(self) -> None:
        ex = self.executor
        ctx = ex.mp_context
        self.cancel_flag = ctx.RawValue(ctypes.c_bool, False)
        parent_conn, child_conn = ctx.Pipe()
        proc = ctx.Process(
            target=child_main,
            args=(child_conn, self.cancel_flag, ex.preload, os.getpid()),
            name=f"tenxgraph-task-{self.index}",
            daemon=True,
        )
        await asyncio.to_thread(proc.start)
        child_conn.close()
        self.proc, self.conn, self.runs = proc, parent_conn, 0
        deadline = time.monotonic() + ex.startup_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ChildStartError(f"not ready after {ex.startup_timeout} s")
            if await ex.wait_readable(parent_conn, min(remaining, ALIVE_POLL)):
                try:
                    message = parent_conn.recv()
                except (EOFError, OSError):
                    await asyncio.to_thread(proc.join, STOP_TIMEOUT)
                    raise _ChildStartError(f"child {describe_exit(proc.exitcode)}") from None
                if message[0] == "ready":
                    return
                raise _ChildStartError(str(message[1]))
            if not proc.is_alive():
                raise _ChildStartError(f"child {describe_exit(proc.exitcode)}")

    def kill_now(self) -> None:
        if self.proc is not None and self.proc.is_alive():
            with contextlib.suppress(Exception):
                self.proc.kill()

    async def discard(self) -> None:
        """Kill the child (if alive), reap it and drop its pipe."""
        proc, conn = self.proc, self.conn
        self.proc = self.conn = None
        if proc is not None:
            if proc.is_alive():
                with contextlib.suppress(Exception):
                    proc.kill()
            await asyncio.to_thread(proc.join, STOP_TIMEOUT)
            with contextlib.suppress(Exception):
                proc.close()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    async def recycle(self) -> None:
        """Ask the child to exit (``None``), wait, kill if it does not."""
        proc, conn = self.proc, self.conn
        if proc is None or conn is None:
            return
        with contextlib.suppress(Exception):
            conn.send(None)
        await asyncio.to_thread(proc.join, STOP_TIMEOUT)
        await self.discard()


class ProcessHandle(ExecutionHandle):
    def __init__(self, executor: ProcessExecutor, spec: ExecutionSpec) -> None:
        self.executor = executor
        self.spec = spec
        self.slot: _Slot | None = None
        self.kill_reason: str | None = None
        self._loop = asyncio.get_running_loop()
        self._outcome: asyncio.Future[ExecutionResult] = self._loop.create_future()
        self._cancel_timer: asyncio.TimerHandle | None = None
        self.supervisor: asyncio.Task | None = None

    async def result(self) -> ExecutionResult:
        return await asyncio.shield(self._outcome)

    @property
    def done(self) -> bool:
        return self._outcome.done()

    def request_cancel(self) -> None:
        self.spec.ctx._set_cancelled()
        if self.done:
            return
        if self.slot is not None and self.slot.cancel_flag is not None:
            self.slot.cancel_flag.value = True
        if self._cancel_timer is None:
            self._cancel_timer = self._loop.call_later(
                self.executor.cancel_grace, self.kill, "cancel"
            )

    def kill(self, reason: str) -> None:
        if self.done or self.kill_reason is not None:
            return
        self.kill_reason = reason
        self.spec.ctx._set_cancelled()
        if self.slot is not None:
            self.slot.kill_now()  # the supervisor sees EOF and resolves "stopped"
        elif self.supervisor is None:
            self._resolve(ExecutionResult(kind="stopped", reason=reason))

    def _resolve(self, result: ExecutionResult) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer.cancel()
            self._cancel_timer = None
        if not self._outcome.done():
            self._outcome.set_result(result)


class ProcessExecutor(Executor):
    """``processes`` slots, each one child. See the module docstring."""

    kind = "process"

    def __init__(  # - worker flags
        self,
        processes: int = 2,
        *,
        preload: Sequence[str] = (),
        max_tasks_per_child: int | None = None,
        max_memory_per_child: float | None = None,
        start_method: str | None = None,
        cancel_grace: float = 5.0,
        startup_timeout: float = STARTUP_TIMEOUT,
    ) -> None:
        if processes < 1:
            raise ValueError("processes must be >= 1")
        if max_tasks_per_child is not None and max_tasks_per_child < 1:
            raise ValueError("max_tasks_per_child must be >= 1")
        if max_memory_per_child is not None and max_memory_per_child <= 0:
            raise ValueError("max_memory_per_child must be > 0 MB")
        method = start_method or default_start_method()
        if method == "fork":
            raise ValueError("start_method 'fork' is not allowed (the worker has threads)")
        self.mp_context: Any = mp.get_context(method)
        self.start_method = method
        self.preload = [CHILD_MODULE, *[m for m in preload if m != CHILD_MODULE]]
        if method == "forkserver":
            self.mp_context.set_forkserver_preload(self.preload)
        self.max_tasks_per_child = max_tasks_per_child
        self.max_memory_per_child = max_memory_per_child
        self.cancel_grace = cancel_grace
        self.startup_timeout = startup_timeout
        self._slots = [_Slot(self, i) for i in range(processes)]
        self._idle: asyncio.Queue[_Slot] | None = None
        self._handles: set[ProcessHandle] = set()
        self._checked: set[str] = set()
        self._closed = False
        self._use_reader = sys.platform != "win32"

    # ------------------------------------------------------------------ introspection

    @property
    def capacity(self) -> int:
        return len(self._slots)

    @property
    def free_slots(self) -> int:
        return 0 if self._closed else max(0, len(self._slots) - len(self._handles))

    def child_pids(self) -> list[int]:
        return [s.pid for s in self._slots if s.pid is not None and s.alive()]

    def _idle_queue(self) -> asyncio.Queue[_Slot]:
        if self._idle is None:
            self._idle = asyncio.Queue()
            for slot in self._slots:
                self._idle.put_nowait(slot)
        return self._idle

    # ------------------------------------------------------------------ pipe wait

    async def wait_readable(self, conn: Connection, timeout: float) -> bool:
        """True when ``conn`` has data or hit EOF within ``timeout`` s."""
        if conn.poll(0):
            return True
        if self._use_reader:
            loop = asyncio.get_running_loop()
            ready = loop.create_future()
            fd = conn.fileno()

            def wake() -> None:
                if not ready.done():
                    ready.set_result(None)

            try:
                loop.add_reader(fd, wake)
            except (NotImplementedError, RuntimeError):
                self._use_reader = False
            else:
                try:
                    done, _ = await asyncio.wait({ready}, timeout=timeout)
                finally:
                    loop.remove_reader(fd)
                    ready.cancel()
                return bool(done)
        return await asyncio.to_thread(conn.poll, timeout)

    # ------------------------------------------------------------------ lifecycle

    async def astart(self) -> None:
        """Start every child now (the first one also starts the forkserver)."""
        for slot in self._slots:
            with contextlib.suppress(_ChildStartError):
                await slot.ensure_child()

    async def start(self, spec: ExecutionSpec) -> ProcessHandle:
        if self._closed:
            raise RuntimeError("process executor is closed")
        task = spec.task
        if task.executor != "process":
            raise ValueError(f"task {task.name!r} has executor={task.executor!r}")
        handle = ProcessHandle(self, spec)
        if task.name not in self._checked:
            try:
                resolve_task(task.fn.__module__, task.fn.__qualname__, task.name)
            except TaskDefinitionError as exc:
                handle._resolve(
                    ExecutionResult(
                        kind="crashed", error=error_dict("child_import", "ImportError", str(exc))
                    )
                )
                return handle
            self._checked.add(task.name)
        self._handles.add(handle)
        try:
            slot = await self._idle_queue().get()
        except BaseException:
            self._handles.discard(handle)
            raise
        if self._closed or handle.kill_reason is not None:
            self._idle_queue().put_nowait(slot)
            self._handles.discard(handle)
            if self._closed and handle.kill_reason is None:
                raise RuntimeError("process executor is closed")
            handle._resolve(ExecutionResult(kind="stopped", reason=handle.kill_reason))
            return handle
        handle.supervisor = asyncio.create_task(
            self._supervise(slot, handle), name=f"task-process:{spec.ctx.run_id}"
        )
        return handle

    async def _supervise(self, slot: _Slot, handle: ProcessHandle) -> None:
        try:
            result = await self._run_on(slot, handle)
        except asyncio.CancelledError:
            await slot.discard()
            handle._resolve(ExecutionResult(kind="stopped", reason=handle.kill_reason or "abort"))
            raise
        except Exception as exc:
            logger.exception("jobs process slot %s failed", slot.index)
            await slot.discard()
            result = ExecutionResult(
                kind="crashed", error=error_dict("crashed", type(exc).__name__, str(exc))
            )
        finally:
            handle.slot = None
            self._handles.discard(handle)
            self._idle_queue().put_nowait(slot)
        handle._resolve(result)

    async def _run_on(self, slot: _Slot, handle: ProcessHandle) -> ExecutionResult:
        spec = handle.spec
        try:
            await slot.ensure_child()
        except _ChildStartError as exc:
            return ExecutionResult(
                kind="crashed", error=error_dict("child_start", "ChildStartError", str(exc))
            )
        if handle.kill_reason is not None:
            return ExecutionResult(kind="stopped", reason=handle.kill_reason)
        conn = slot.conn
        assert conn is not None  # noqa: S101 - set by ensure_child
        slot.cancel_flag.value = spec.ctx.cancelled
        payload = {
            "module": spec.task.fn.__module__,
            "qualname": spec.task.fn.__qualname__,
            "name": spec.task.name,
            "kwargs": spec.stored_kwargs,
            "run_id": spec.ctx.run_id,
            "attempt": spec.ctx.attempt,
            "queue": spec.ctx.queue,
            "max_result_bytes": spec.max_result_bytes,
        }
        try:
            conn.send(("run", payload))
        except (OSError, ValueError) as exc:  # the idle child died just now
            await slot.discard()
            return ExecutionResult(
                kind="crashed", error=error_dict("crashed", type(exc).__name__, str(exc))
            )
        except Exception as exc:  # not picklable: never happens with JSON kwargs
            return ExecutionResult(
                kind="invalid", error=error_dict("validation", type(exc).__name__, str(exc))
            )
        handle.slot = slot
        if handle.kill_reason is not None:  # killed between the checks
            slot.kill_now()
        result = await self._watch(slot, handle, conn)
        handle.slot = None
        if result.kind in ("returned", "raised", "invalid"):
            await self._after_run(slot)
        return result

    async def _watch(  # noqa: PLR0911 - one return per way a run ends
        self, slot: _Slot, handle: ProcessHandle, conn: Connection
    ) -> ExecutionResult:
        timeout = handle.spec.effective_timeout()
        loop = asyncio.get_running_loop()
        deadline = math.inf if timeout is None else loop.time() + timeout
        interval = MEMORY_POLL if self.max_memory_per_child else ALIVE_POLL
        emitter = handle.spec.ctx.emit
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                handle.spec.ctx._set_cancelled()
                await slot.discard()
                return ExecutionResult(
                    kind="timeout",
                    error=error_dict(
                        "timeout", "TimeoutError", f"attempt exceeded timeout of {timeout} s"
                    ),
                )
            readable = await self.wait_readable(conn, min(interval, remaining))
            if handle.kill_reason is not None:
                await slot.discard()
                return ExecutionResult(kind="stopped", reason=handle.kill_reason)
            if readable:
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    return await self._crashed(slot)
                if message[0] == "emit":
                    _replay(emitter, message[1], message[2])
                    continue
                if message[0] == "result":
                    return ExecutionResult(kind="returned", value=message[1], value_is_json=True)
                return _child_error_result(message[1])
            if self.max_memory_per_child and slot.pid is not None:
                used = rss_mb(slot.pid)
                if used is not None and used > self.max_memory_per_child:
                    await slot.discard()
                    return ExecutionResult(
                        kind="crashed",
                        error=error_dict(
                            "memory_limit",
                            "MemoryError",
                            f"process child used {used:.0f} MB, over max_memory_per_child "
                            f"{self.max_memory_per_child:g} MB; it was killed",
                        ),
                    )
            if not slot.alive() and not conn.poll(0):
                return await self._crashed(slot)

    async def _crashed(self, slot: _Slot) -> ExecutionResult:
        proc = slot.proc
        if proc is not None:
            await asyncio.to_thread(proc.join, STOP_TIMEOUT)
        code = None if proc is None else proc.exitcode
        await slot.discard()
        return ExecutionResult(
            kind="crashed",
            error=error_dict(
                "crashed", "ChildProcessError", f"process child {describe_exit(code)}"
            ),
        )

    async def _after_run(self, slot: _Slot) -> None:
        slot.runs += 1
        if self.max_tasks_per_child is not None and slot.runs >= self.max_tasks_per_child:
            await slot.recycle()
            return
        if self.max_memory_per_child and slot.pid is not None:
            used = rss_mb(slot.pid)
            if used is not None and used > self.max_memory_per_child:
                logger.info(
                    "jobs process slot %s: child at %.0f MB after a run; replacing it",
                    slot.index,
                    used,
                )
                await slot.recycle()

    async def aclose(self, *, grace: float = 30.0) -> None:
        self._closed = True
        running = [h.supervisor for h in self._handles if h.supervisor is not None]
        if running:
            _, pending = await asyncio.wait(running, timeout=grace)
            if pending:
                for handle in list(self._handles):
                    handle.kill("shutdown")
                await asyncio.wait(pending, timeout=STOP_TIMEOUT + 1)
        for handle in list(self._handles):  # waiting for a slot
            handle.kill("shutdown")
        for slot in self._slots:
            await slot.recycle()
            await slot.discard()


def _replay(emitter: Any, method: str, args: Sequence[Any]) -> None:
    """Apply one child emit call to the attempt's ``TaskEmitter``."""
    try:
        if method == "start":
            emitter._start(args[0], args[1])
        elif method == "end":
            emitter._end(args[0], failed=bool(args[1]))
        elif method in ("steps", "progress", "eta", "log"):
            getattr(emitter, method)(*args)
    except Exception:
        logger.warning("dropped a bad emit call %r from a process child", method, exc_info=True)
