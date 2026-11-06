"""Process executor: supervised child per slot, kill, recycle, memory cap, crash, shutdown."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time

import pytest
import pytest_asyncio

from tenxgraph_api.jobs import Fail, RetryAfter, TaskContext, TaskDefinitionError, task
from tenxgraph_api.jobs.core.emit import TaskEmitter
from tenxgraph_api.jobs.core.registry import isolated_registry
from tenxgraph_api.jobs.executors import (
    ChildTaskError,
    ExecutionSpec,
    ProcessExecutor,
    default_start_method,
)
from tenxgraph_api.jobs.executors.process import _Slot, rss_mb
from tests.unit_tests.jobs import process_tasks as pt


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX process checks"),
]

PRELOAD = ["tests.unit_tests.jobs.process_tasks"]


def make_ctx(run_id: str = "run-1") -> TaskContext:
    return TaskContext(
        run_id=run_id,
        attempt=1,
        task="t",
        queue="default",
        identity={"user_id": "u1"},
        emit=TaskEmitter(clock=time.time),
    )


def spec(task_obj, kwargs=None, *, run_id="run-1", timeout=None) -> ExecutionSpec:
    return ExecutionSpec(
        task=task_obj, stored_kwargs=kwargs or {}, ctx=make_ctx(run_id), timeout=timeout
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is not alive
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@pytest_asyncio.fixture
async def executor():
    ex = ProcessExecutor(2, preload=PRELOAD)
    await ex.astart()
    yield ex
    await ex.aclose(grace=1)


async def run(ex: ProcessExecutor, s: ExecutionSpec):
    handle = await ex.start(s)
    return await handle.result()


async def test_start_method_is_never_fork():
    assert default_start_method() in ("forkserver", "spawn")
    with pytest.raises(ValueError, match="fork"):
        ProcessExecutor(1, start_method="fork")


async def test_success_runs_in_a_child(executor):
    result = await run(executor, spec(pt.pt_add, {"a": 2, "b": 3}))
    assert result.kind == "returned"
    assert result.value == 5
    assert result.value_is_json
    child = await run(executor, spec(pt.pt_pid))
    assert child.value != os.getpid()
    assert child.value in executor.child_pids()


async def test_exception_fail_and_retry_after(executor):
    boom = await run(executor, spec(pt.pt_boom, {"message": "nope"}))
    assert boom.kind == "raised"
    assert isinstance(boom.exception, ChildTaskError)
    assert boom.error == {"code": "error", "type": "ValueError", "message": "nope"}
    assert "ValueError" in boom.exception.traceback_text

    fail = await run(executor, spec(pt.pt_fail))
    assert fail.kind == "raised"
    assert isinstance(fail.exception, Fail)
    assert fail.exception.data == {"field": "x"}
    assert fail.error["code"] == "fail"

    retry = await run(executor, spec(pt.pt_retry_after))
    assert isinstance(retry.exception, RetryAfter)
    assert retry.exception.seconds == 7


async def test_invalid_kwargs_and_result(executor):
    bad_kwargs = await run(executor, spec(pt.pt_add, {"a": "x", "b": 1}))
    assert bad_kwargs.kind == "invalid"
    assert bad_kwargs.error["code"] == "validation"
    bad_result = await run(executor, spec(pt.pt_bad_result))
    assert bad_result.kind == "invalid"
    assert bad_result.error["code"] == "result_invalid"


async def test_timeout_kills_child_fast_and_slot_recovers(executor):
    started = time.monotonic()
    result = await run(executor, spec(pt.pt_sleep, {"seconds": 30}, timeout=0.3))
    elapsed = time.monotonic() - started
    assert result.kind == "timeout"
    assert result.error["code"] == "timeout"
    assert elapsed < 0.6
    again = await run(executor, spec(pt.pt_add, {"a": 1, "b": 1}))
    assert again.value == 2


async def test_cancel_is_cooperative_first(executor):
    s = spec(pt.pt_wait_cancel)
    handle = await executor.start(s)
    await asyncio.sleep(0.1)
    handle.request_cancel()
    result = await handle.result()
    assert s.ctx.cancelled
    assert result.kind == "returned"
    assert result.value == "saw cancel"


async def test_cancel_kills_after_grace():
    ex = ProcessExecutor(1, preload=PRELOAD, cancel_grace=0.2)
    try:
        handle = await ex.start(spec(pt.pt_ignore_cancel))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        handle.request_cancel()
        result = await handle.result()
        assert result.kind == "stopped"
        assert result.reason == "cancel"
        assert time.monotonic() - started < 0.6
    finally:
        await ex.aclose(grace=1)


async def test_kill_stops_now(executor):
    handle = await executor.start(spec(pt.pt_sleep, {"seconds": 30}))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    handle.kill("lost")
    result = await handle.result()
    assert result.kind == "stopped"
    assert result.reason == "lost"
    assert time.monotonic() - started < 0.3


async def test_emit_events_arrive_in_order(executor):
    s = spec(pt.pt_emit, run_id="run-emit")
    result = await run(executor, s)
    assert result.value == "run-emit"
    taken = s.ctx.emit.take()
    assert taken is not None
    events, snapshot = taken
    kinds = [e.type for e in events]
    assert kinds == [
        "steps",
        "step.start",
        "step.end",
        "log",
        "step.start",
        "eta",
        "step.end",
        "progress",
    ]
    assert events[3].data == {"message": "note", "level": "warning", "data": {"k": 1}}
    assert snapshot["percent"] == 50
    assert [st["state"] for st in snapshot["steps"]] == ["succeeded", "succeeded"]


async def test_recycle_after_max_tasks():
    ex = ProcessExecutor(1, preload=PRELOAD, max_tasks_per_child=2)
    try:
        pids = [(await run(ex, spec(pt.pt_pid))).value for _ in range(4)]
        assert pids[0] == pids[1]
        assert pids[2] == pids[3]
        assert pids[0] != pids[2]
        assert not pid_alive(pids[0])
    finally:
        await ex.aclose(grace=1)


@pytest.mark.skipif(rss_mb(os.getpid()) is None, reason="no way to read memory")
async def test_memory_cap_kills_mid_run():
    ex = ProcessExecutor(1, preload=PRELOAD)
    await ex.astart()
    base = rss_mb(ex.child_pids()[0])
    assert base is not None
    ex.max_memory_per_child = base + 100
    try:
        started = time.monotonic()
        result = await run(ex, spec(pt.pt_alloc, {"mb": 300, "hold": 5}))
        assert result.kind == "crashed"
        assert result.error["code"] == "memory_limit"
        assert time.monotonic() - started < 2
        assert (await run(ex, spec(pt.pt_add, {"a": 1, "b": 2}))).value == 3
    finally:
        await ex.aclose(grace=1)


@pytest.mark.skipif(rss_mb(os.getpid()) is None, reason="no way to read memory")
async def test_memory_over_cap_after_run_recycles():
    ex = ProcessExecutor(1, preload=PRELOAD)
    await ex.astart()
    first = ex.child_pids()[0]
    base = rss_mb(first)
    assert base is not None
    ex.max_memory_per_child = base + 150
    try:
        # A 100 MB block freed on return: what stays resident decides; either way no crash.
        result = await run(ex, spec(pt.pt_alloc, {"mb": 100, "hold": 0}))
        assert result.kind == "returned"
    finally:
        await ex.aclose(grace=1)


async def test_child_exit_and_segfault_fail_the_attempt(executor):
    exited = await run(executor, spec(pt.pt_exit, {"code": 3}))
    assert exited.kind == "crashed"
    assert exited.error["code"] == "crashed"
    assert "exited with code 3" in exited.error["message"]
    segv = await run(executor, spec(pt.pt_segv))
    assert segv.kind == "crashed"
    assert "SIGSEGV" in segv.error["message"]
    assert (await run(executor, spec(pt.pt_add, {"a": 4, "b": 4}))).value == 8


async def test_sigkill_from_outside(executor):
    handle = await executor.start(spec(pt.pt_sleep, {"seconds": 30}))
    await asyncio.sleep(0.1)
    victim = handle.slot.pid
    os.kill(victim, signal.SIGKILL)
    result = await handle.result()
    assert result.kind == "crashed"
    assert "SIGKILL" in result.error["message"]
    assert (await run(executor, spec(pt.pt_add, {"a": 1, "b": 0}))).value == 1


async def test_import_error_is_reported(executor, monkeypatch):
    monkeypatch.setattr(pt.pt_add.fn, "__module__", "tests.unit_tests.jobs.no_such_module")
    result = await run(executor, spec(pt.pt_add, {"a": 1, "b": 1}))
    assert result.kind == "crashed"
    assert result.error["code"] == "child_import"
    assert "no_such_module" in result.error["message"]


async def test_child_startup_failure_backs_off():
    ex = ProcessExecutor(1, preload=["tests.unit_tests.jobs.no_such_module"])
    try:
        first = await run(ex, spec(pt.pt_add, {"a": 1, "b": 1}))
        assert first.kind == "crashed"
        assert first.error["code"] == "child_start"
        started = time.monotonic()
        second = await run(ex, spec(pt.pt_add, {"a": 1, "b": 1}))
        assert second.error["code"] == "child_start"
        assert "next try" in second.error["message"]
        assert time.monotonic() - started < 0.1  # no new spawn during the backoff
    finally:
        await ex.aclose(grace=1)


async def test_lambda_and_local_function_rejected_at_registration():
    with isolated_registry():
        with pytest.raises(TaskDefinitionError, match="lambda"):
            task(lambda: 1, name="pt_lambda", executor="process")
        with pytest.raises(TaskDefinitionError, match="inside another function"):

            @task(name="pt_local", executor="process")
            def local() -> None: ...

        with pytest.raises(TaskDefinitionError, match="plain `def`"):

            @task(name="pt_async", executor="process")
            async def coro() -> None: ...


async def test_many_tasks_across_two_slots(executor):
    started = time.monotonic()
    handles = [
        await executor.start(spec(pt.pt_sleep, {"seconds": 0.1}, run_id=f"r{i}")) for i in range(2)
    ]
    rest = [
        asyncio.create_task(run(executor, spec(pt.pt_add, {"a": i, "b": 1}, run_id=f"x{i}")))
        for i in range(20)
    ]
    assert executor.free_slots == 0
    results = [await h.result() for h in handles] + list(await asyncio.gather(*rest))
    assert [r.value for r in results[2:]] == [i + 1 for i in range(20)]
    assert all(r.value == "slept" for r in results[:2])
    assert time.monotonic() - started < 2
    assert executor.free_slots == 2


async def test_close_kills_running_children_and_leaves_no_orphans():
    ex = ProcessExecutor(2, preload=PRELOAD)
    await ex.astart()
    pids = ex.child_pids()
    assert len(pids) == 2
    handle = await ex.start(spec(pt.pt_sleep, {"seconds": 30}))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    await ex.aclose(grace=0.2)
    assert time.monotonic() - started < 1.5
    result = await handle.result()
    assert result.kind == "stopped"
    assert result.reason == "shutdown"
    assert ex.child_pids() == []
    assert not any(pid_alive(p) for p in pids)
    with pytest.raises(RuntimeError, match="closed"):
        await ex.start(spec(pt.pt_add, {"a": 1, "b": 1}))


async def test_close_drains_running_work():
    ex = ProcessExecutor(1, preload=PRELOAD)
    handle = await ex.start(spec(pt.pt_sleep, {"seconds": 0.2}))
    await asyncio.sleep(0.05)
    await ex.aclose(grace=5)
    assert (await handle.result()).value == "slept"


EMIT_THREADS = 4
EMIT_EACH = 15
BIG = "x" * 20_000


@task(name="pt_emit_threads", executor="process")
def pt_emit_threads(ctx: TaskContext) -> str:
    def worker(n: int) -> None:
        for i in range(EMIT_EACH):
            ctx.emit.log(f"{n}-{i}", data={"blob": BIG})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(EMIT_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return "done"


async def test_emits_from_many_threads_do_not_interleave():
    ex = ProcessExecutor(1, preload=PRELOAD)
    try:
        s = spec(pt_emit_threads, run_id="run-threads", timeout=30)
        result = await run(ex, s)
        assert result.kind == "returned", result.error
        assert result.value == "done"
        events, _ = s.ctx.emit.take()
        logs = sorted(e.data["message"] for e in events if e.type == "log")
        expected = sorted(f"{n}-{i}" for n in range(EMIT_THREADS) for i in range(EMIT_EACH))
        assert logs == expected
    finally:
        await ex.aclose(grace=1)


async def test_kill_while_child_starts_resolves_at_once(monkeypatch):
    async def never_ready(self):
        await asyncio.sleep(60)

    monkeypatch.setattr(_Slot, "_spawn", never_ready)
    ex = ProcessExecutor(1, preload=PRELOAD)
    handle = await ex.start(spec(pt.pt_add, {"a": 1, "b": 1}))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    handle.kill("lost")
    result = await asyncio.wait_for(handle.result(), 2)
    assert result.kind == "stopped"
    assert result.reason == "lost"
    assert time.monotonic() - started < 1
    await asyncio.wait_for(ex.aclose(grace=0.1), 5)


async def test_close_while_children_start_leaves_no_child():
    ex = ProcessExecutor(2, preload=PRELOAD)
    seen: set[int] = set()

    async def watch_pids():
        while True:
            for slot in ex._slots:
                try:
                    if slot.proc is not None and slot.proc.pid is not None:
                        seen.add(slot.proc.pid)
                except ValueError:  # closed process object
                    pass
            await asyncio.sleep(0.002)

    watcher = asyncio.create_task(watch_pids())
    handles = [await ex.start(spec(pt.pt_add, {"a": i, "b": 1}, run_id=f"c{i}")) for i in range(2)]
    deadline = time.monotonic() + 10
    while not seen and time.monotonic() < deadline:  # a child exists, not yet ready
        await asyncio.sleep(0.002)
    assert seen
    await asyncio.wait_for(ex.aclose(grace=0), 30)
    await asyncio.sleep(0.3)
    watcher.cancel()
    for handle in handles:
        assert (await asyncio.wait_for(handle.result(), 5)).kind in ("stopped", "returned")
    assert not [p for p in seen if pid_alive(p)]
