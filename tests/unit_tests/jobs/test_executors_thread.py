"""Thread executor: bounded pool, emit from the thread, cancel flag, timeout abandon, Inject."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from injectq import Inject, InjectQ

from tenxgraph_api.jobs import Fail, RetryAfter, TaskContext, TaskDefinitionError, task
from tenxgraph_api.jobs.emit import TaskEmitter
from tenxgraph_api.jobs.executors import ExecutionSpec, ThreadExecutor
from tenxgraph_api.jobs.identity import current_task_context
from tenxgraph_api.jobs.registry import isolated_registry


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _registry():
    with isolated_registry():
        yield


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


async def run(ex: ThreadExecutor, s: ExecutionSpec):
    return await (await ex.start(s)).result()


async def test_task_definition_thread_needs_plain_def():
    @task(name="tt_ok", executor="thread")
    def ok() -> None: ...

    assert ok.executor == "thread"
    with pytest.raises(TaskDefinitionError, match="plain `def`"):

        @task(name="tt_async", executor="thread")
        async def bad() -> None: ...


async def test_success_runs_off_the_loop_thread():
    @task(name="tt_where", executor="thread")
    def where(x: int) -> dict:
        return {"x": x, "thread": threading.current_thread().name}

    ex = ThreadExecutor(2)
    result = await run(ex, spec(where, {"x": 3}))
    await ex.aclose()
    assert result.kind == "returned"
    assert result.value["x"] == 3
    assert result.value["thread"].startswith("tenxgraph-task")
    assert not result.value_is_json


async def test_exceptions_keep_their_types():
    @task(name="tt_boom", executor="thread")
    def boom() -> None:
        raise KeyError("k")

    @task(name="tt_fail", executor="thread")
    def fail() -> None:
        raise Fail("no", data={"a": 1})

    @task(name="tt_retry", executor="thread")
    def retry() -> None:
        raise RetryAfter(2)

    ex = ThreadExecutor(2)
    b = await run(ex, spec(boom))
    f = await run(ex, spec(fail))
    r = await run(ex, spec(retry))
    await ex.aclose()
    assert b.kind == "raised" and isinstance(b.exception, KeyError)
    assert b.error["type"] == "KeyError"
    assert isinstance(f.exception, Fail) and f.error == {
        "code": "fail",
        "type": "Fail",
        "message": "no",
        "data": {"a": 1},
    }
    assert isinstance(r.exception, RetryAfter)


async def test_invalid_kwargs_do_not_take_a_slot():
    @task(name="tt_int", executor="thread")
    def needs_int(n: int) -> int:
        return n

    ex = ThreadExecutor(1)
    result = await run(ex, spec(needs_int, {"n": "x"}))
    assert result.kind == "invalid"
    assert result.error["code"] == "validation"
    assert ex.free_slots == 1
    await ex.aclose()


async def test_emit_from_thread_and_cancel_flag_visible():
    @task(name="tt_emit", executor="thread")
    def work(ctx: TaskContext) -> str:
        ctx.emit.steps(["one"])
        with ctx.emit.step("one"):
            ctx.emit.progress("going", percent=40)
        while not ctx.cancelled:
            time.sleep(0.01)
        return "stopped cleanly"

    ex = ThreadExecutor(1)
    s = spec(work)
    handle = await ex.start(s)
    await asyncio.sleep(0.1)
    handle.request_cancel()
    result = await handle.result()
    await ex.aclose()
    assert result.value == "stopped cleanly"
    events, snapshot = s.ctx.emit.take()
    assert [e.type for e in events] == ["steps", "step.start", "step.end", "progress"]
    assert snapshot["percent"] == 40


async def test_timeout_abandons_thread_which_keeps_its_slot():
    release = threading.Event()
    finished = threading.Event()

    @task(name="tt_slow", executor="thread")
    def slow(ctx: TaskContext) -> str:
        release.wait(5)
        finished.set()
        return "late"

    ex = ThreadExecutor(1)
    s = spec(slow, timeout=0.2)
    started = time.monotonic()
    handle = await ex.start(s)
    result = await handle.result()
    assert result.kind == "timeout"
    assert result.error["code"] == "timeout"
    assert time.monotonic() - started < 0.5
    assert s.ctx.cancelled
    assert ex.free_slots == 0  # the abandoned thread still holds the slot
    release.set()
    for _ in range(100):
        if ex.free_slots == 1:
            break
        await asyncio.sleep(0.01)
    assert finished.is_set()
    assert ex.free_slots == 1
    assert (await handle.result()).kind == "timeout"  # the late result is discarded
    await ex.aclose()


async def test_kill_abandons_and_slot_bounds_concurrency():
    gate = threading.Event()
    running = []

    @task(name="tt_gate", executor="thread")
    def gated(i: int) -> int:
        running.append(i)
        gate.wait(5)
        return i

    ex = ThreadExecutor(2)
    first = await ex.start(spec(gated, {"i": 0}))
    second = await ex.start(spec(gated, {"i": 1}))
    third = asyncio.create_task(ex.start(spec(gated, {"i": 2})))
    await asyncio.sleep(0.1)
    assert not third.done()  # waits for a slot
    assert sorted(running) == [0, 1]
    first.kill("lost")
    assert (await first.result()).kind == "stopped"
    await asyncio.sleep(0.05)
    assert not third.done()  # a killed thread keeps its slot until it returns
    gate.set()
    handle3 = await third
    assert (await second.result()).value == 1
    assert (await handle3.result()).value == 2
    await ex.aclose()


class Greeter:
    def hello(self) -> str:
        return "hi"


async def test_inject_and_contextvars_reach_the_thread():
    container = InjectQ()
    container.bind_instance(Greeter, Greeter())

    @task(name="tt_inject", executor="thread")
    def greet(greeter: Greeter = Inject[Greeter]) -> dict:
        current = current_task_context.get()
        return {"hello": greeter.hello(), "run": None if current is None else current.run_id}

    ex = ThreadExecutor(1)
    s = spec(greet, run_id="run-ctx")
    token = current_task_context.set(s.ctx)
    try:
        with container.context():
            result = await run(ex, s)
    finally:
        current_task_context.reset(token)
    await ex.aclose()
    assert result.value == {"hello": "hi", "run": "run-ctx"}
    assert "greeter" not in greet.kwargs_schema().get("properties", {})


async def test_close_waits_then_abandons():
    gate = threading.Event()

    @task(name="tt_hang", executor="thread")
    def hang() -> None:
        gate.wait(5)

    ex = ThreadExecutor(1)
    handle = await ex.start(spec(hang))
    started = time.monotonic()
    await ex.aclose(grace=0.1)
    assert time.monotonic() - started < 0.5
    assert not handle.done
    gate.set()
    with pytest.raises(RuntimeError, match="closed"):
        await ex.start(spec(hang))
