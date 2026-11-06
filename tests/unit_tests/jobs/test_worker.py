"""The in-process worker: execution, retries, control flow, timeout, cancel, leases, caps,
hooks."""

from __future__ import annotations

import asyncio

import pytest

from tenxgraph_api.jobs import Fail, RetryAfter, TaskContext, task
from tenxgraph_api.jobs.core.backoff import Backoff
from tenxgraph_api.jobs.core.models import MAX_DEFERRALS, NewRun
from tenxgraph_api.jobs.testing import jobs_harness
from tenxgraph_api.jobs.worker.worker import Worker


pytestmark = pytest.mark.asyncio

U1 = {"user_id": "u1"}
GATES: dict[str, asyncio.Event] = {}
SEEN: dict[str, list] = {}
SUCCESS_HOOK: list[str] = []
FAILURE_HOOK: list[str] = []


def gate(name: str) -> asyncio.Event:
    GATES[name] = asyncio.Event()
    return GATES[name]


async def until(predicate, rounds: int = 200) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def on_success_hook(run):
    SUCCESS_HOOK.append(run.run_id)


def on_failure_hook(run):
    FAILURE_HOOK.append(run.run_id)
    raise RuntimeError("hook errors are ignored")


@task(on_success=on_success_hook)
async def wk_ok(x: int, ctx: TaskContext) -> dict:
    return {"x": x, "attempt": ctx.attempt}


@task(retries=2, backoff=Backoff(base=10, cap=100, jitter=False), on_failure=on_failure_hook)
async def wk_always_fails() -> None:
    raise ValueError("bad")


@task(retries=3, backoff=5)
async def wk_flaky(ok_on: int, ctx: TaskContext) -> int:
    if ctx.attempt < ok_on:
        raise RuntimeError(f"attempt {ctx.attempt}")
    return ctx.attempt


@task(retries=5)
async def wk_fail_now() -> None:
    raise Fail("bad input", data={"field": "x"})


@task(retries=0)
async def wk_rate_limited(times: int, ctx: TaskContext) -> int:
    seen = SEEN.setdefault(ctx.run_id, [])
    seen.append(ctx.attempt)
    if len(seen) <= times:
        raise RetryAfter(30)
    return len(seen)


@task(timeout=0.05, retries=1, backoff=1)
async def wk_slow(ctx: TaskContext) -> None:
    SEEN.setdefault(ctx.run_id, []).append(ctx.attempt)
    await asyncio.sleep(10)


@task
async def wk_wait(name: str, ctx: TaskContext) -> str:
    SEEN.setdefault(ctx.run_id, []).append(ctx.attempt)
    ctx.emit.progress("waiting", percent=10)
    try:
        await GATES[name].wait()
    except asyncio.CancelledError:
        SEEN[ctx.run_id].append(("cancelled", ctx.cancelled))
        raise
    return f"done {ctx.attempt}"


@task
async def wk_stubborn(name: str, ctx: TaskContext) -> str:
    """Ignores asyncio cancellation; checks ctx.cancelled cooperatively."""
    for _ in range(1000):
        if ctx.cancelled:
            return "saw cancel"
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            continue
    return "finished"


@task(concurrency=1)
async def wk_capped(name: str, ctx: TaskContext) -> None:
    SEEN.setdefault("capped", []).append(("start", ctx.run_id))
    await GATES[name].wait()
    SEEN["capped"].append(("end", ctx.run_id))


@task(concurrency=1, concurrency_key="tenant")
async def wk_per_key(tenant: str, name: str) -> None:
    await GATES[name].wait()


@task
async def wk_bad_result() -> int:
    return "not an int"  # type: ignore[return-value]


@task(max_lost=1)
async def wk_lossy(name: str, ctx: TaskContext) -> int:
    SEEN.setdefault(ctx.run_id, []).append(ctx.attempt)
    await GATES[name].wait()
    return ctx.attempt


@task
async def wk_order(n: int) -> None:
    SEEN.setdefault("order", []).append(n)


async def test_success_result_context_and_hooks(jobs, hook):
    run = await wk_ok.options(identity=U1).enqueue(x=3)
    await jobs.drain()
    status = await run.status()
    assert status.state == "succeeded"
    assert status.attempt == 1
    assert status.result["x"] == 3
    assert status.finished_at is not None and status.started_at is not None
    assert status.worker_id is None and status.lease_expires_at is None
    assert hook.names(run.id) == ["queued", "start", "success"]
    assert SUCCESS_HOOK[-1] == run.id


async def test_task_gets_the_graph_through_inject(jobs):
    from injectq import Inject, InjectQ

    class FakeGraph:
        name = "g"

    InjectQ.get_instance().bind_instance(FakeGraph, FakeGraph())

    @task(name="wk_inject_graph")
    async def wk_inject_graph(graph: FakeGraph = Inject[FakeGraph]) -> dict:
        return {"graph": graph.name}

    run = await wk_inject_graph.options(identity=U1).enqueue()
    await jobs.drain()
    assert (await run.result()) == {"graph": "g"}


async def test_retries_with_backoff_then_fail(jobs, hook):
    run = await wk_always_fails.options(identity=U1).enqueue()
    await jobs.drain()
    status = await run.status()
    assert status.state == "retrying"
    assert status.failure_count == 1
    assert status.available_at == pytest.approx(jobs.clock.now() + 10)
    assert status.error == {"code": "error", "type": "ValueError", "message": "bad"}
    jobs.clock.advance(10)
    await jobs.drain()
    status = await run.status()
    assert status.state == "retrying"
    assert status.available_at == pytest.approx(jobs.clock.now() + 20)
    jobs.clock.advance(20)
    await jobs.drain()
    status = await run.status()
    assert status.state == "failed"
    assert status.attempt == 3
    assert status.failure_count == 3
    assert hook.names(run.id) == [
        "queued",
        "start",
        "retry",
        "start",
        "retry",
        "start",
        "failure",
    ]
    assert FAILURE_HOOK[-1] == run.id  # the hook raised; the run is unchanged
    assert status.state == "failed"


async def test_flaky_succeeds_on_retry(jobs):
    run = await wk_flaky.options(identity=U1).enqueue(ok_on=3)
    await jobs.drain(follow_delays=True)
    status = await run.status()
    assert status.state == "succeeded"
    assert status.result == 3
    assert status.failure_count == 2


async def test_fail_skips_retries(jobs, hook):
    run = await wk_fail_now.options(identity=U1).enqueue()
    await jobs.drain()
    status = await run.status()
    assert status.state == "failed"
    assert status.attempt == 1
    assert status.error == {
        "code": "fail",
        "type": "Fail",
        "message": "bad input",
        "data": {"field": "x"},
    }
    assert "retry" not in hook.names(run.id)


async def test_retry_after_uses_no_retry(jobs):
    run = await wk_rate_limited.options(identity=U1).enqueue(times=2)
    await jobs.drain()
    status = await run.status()
    assert status.state == "queued"
    assert status.defer_count == 1
    assert status.failure_count == 0
    assert status.available_at == pytest.approx(jobs.clock.now() + 30)
    await jobs.drain(follow_delays=True)
    status = await run.status()
    assert status.state == "succeeded"
    assert status.result == 3
    assert status.attempt == 3
    assert status.defer_count == 2


async def test_retry_after_capped(jobs):
    run = await wk_rate_limited.options(identity=U1).enqueue(times=MAX_DEFERRALS + 5)
    await jobs.drain(follow_delays=True)
    status = await run.status()
    assert status.state == "failed"
    assert status.error["code"] == "too_many_deferrals"
    assert status.defer_count == MAX_DEFERRALS


async def test_timeout_fails_and_retries(jobs, hook):
    run = await wk_slow.options(identity=U1).enqueue()
    await jobs.drain(follow_delays=True, timeout=5)
    status = await run.status()
    assert status.state == "failed"
    assert status.error["code"] == "timeout"
    assert SEEN[run.id] == [1, 2]
    assert hook.names(run.id) == ["queued", "start", "retry", "start", "failure"]


async def test_cancel_queued_goes_straight_to_cancelled(jobs, hook):
    run = await wk_ok.options(identity=U1).enqueue(x=1)
    record = await run.cancel()
    assert record.state == "cancelled"
    assert hook.names(run.id) == ["queued", "cancel"]
    await jobs.drain()
    assert (await run.status()).state == "cancelled"
    assert (await run.status()).attempt == 0


async def test_cancel_running_is_cooperative(jobs, hook):
    gate("c1")
    run = await wk_wait.options(identity=U1).enqueue(name="c1")
    await jobs.worker.poll_once()
    await until(lambda: run.id in SEEN)
    record = await run.cancel()
    assert record.state == "cancelling"
    assert (await run.cancel()).state == "cancelling"
    await jobs.drain()
    status = await run.status()
    assert status.state == "cancelled"
    assert SEEN[run.id] == [1, ("cancelled", True)]
    assert hook.names(run.id)[-1] == "cancel"


async def test_cancel_with_task_ignoring_asyncio_cancel(jobs):
    run = await wk_stubborn.options(identity=U1).enqueue(name="s")
    await jobs.worker.poll_once()
    await run.cancel()
    await jobs.drain()
    status = await run.status()
    assert status.state == "cancelled"
    assert status.result is None


async def test_cancel_finished_run_is_unchanged(jobs):
    run = await wk_ok.options(identity=U1).enqueue(x=1)
    await jobs.drain()
    assert (await run.cancel()).state == "succeeded"


async def test_lost_lease_fences_the_stale_attempt(jobs, hook):
    g = gate("lost1")
    run = await wk_lossy.options(identity=U1).enqueue(name="lost1")
    await jobs.worker.poll_once()
    await until(lambda: run.id in SEEN)
    await jobs.worker.tick()  # flush the started attempt's state
    jobs.worker.heartbeats = False  # the worker stalls (think SIGSTOP)
    jobs.clock.advance(seconds=jobs.runtime.lease_ttl + 1)
    stats = await jobs.sweep()
    assert stats.lost == 1
    status = await run.status()
    assert status.state == "queued"
    assert status.lost_count == 1
    assert status.failure_count == 0
    assert hook.names(run.id)[-1] == "lost"

    # The stalled attempt wakes up and finishes: its writes are fenced out.
    g.set()
    await until(lambda: not jobs.worker.inflight)
    status = await run.status()
    assert status.state == "queued"
    assert status.result is None

    # The run goes again with a new attempt (the fencing token) and succeeds.
    jobs.worker.heartbeats = True
    await jobs.drain()
    status = await run.status()
    assert status.state == "succeeded"
    assert status.result == 2
    assert SEEN[run.id] == [1, 2]


async def test_heartbeat_detects_lost_lease_and_stops_task(jobs):
    gate("lost2")
    run = await wk_wait.options(identity=U1).enqueue(name="lost2")
    other = Worker(jobs.runtime, worker_id="other", queues=jobs.transport.queue_names)
    await jobs.worker.poll_once()
    await until(lambda: run.id in SEEN)
    jobs.worker.heartbeats = False
    jobs.clock.advance(seconds=jobs.runtime.lease_ttl + 1)
    await jobs.sweep()
    await other.poll_once()  # another worker takes attempt 2
    await until(lambda: len(SEEN[run.id]) >= 2)
    jobs.worker.heartbeats = True
    await jobs.worker.tick()  # renew reports the lease gone: the stale attempt is cancelled
    assert not jobs.worker.inflight
    await until(lambda: not jobs.worker.jobs())
    assert SEEN[run.id][0] == 1
    assert SEEN[run.id][1] == 2
    assert SEEN[run.id][2] == ("cancelled", True)  # the stale attempt was stopped
    GATES["lost2"].set()
    for _ in range(50):
        await other.tick()
        await asyncio.sleep(0)
        if not other.inflight:
            break
    status = await run.status()
    assert status.state == "succeeded"
    assert status.result == "done 2"
    await other.abort()


async def test_max_lost_fails_the_run(jobs, hook):
    gate("crash")
    run = await wk_lossy.options(identity=U1).enqueue(name="crash")
    for expected in ("queued", "failed"):
        await jobs.worker.poll_once()
        await until(lambda: bool(jobs.worker.inflight))
        await jobs.worker.abort()  # kill -9: nothing is written
        jobs.worker.resume()
        jobs.clock.advance(seconds=jobs.runtime.lease_ttl + 1)
        await jobs.sweep()
        assert (await run.status()).state == expected
    status = await run.status()
    assert status.error["code"] == "lost"
    assert status.lost_count == 2
    assert hook.names(run.id)[-2:] == ["lost", "failure"]


async def test_concurrency_cap_per_task(jobs):
    g = gate("cap")
    SEEN.pop("capped", None)
    runs_ = [await wk_capped.options(identity=U1).enqueue(name="cap") for _ in range(3)]
    for _ in range(5):
        await jobs.worker.tick()
        await jobs.sweep()
        await jobs.worker.poll_once()
        await asyncio.sleep(0)
    assert len(jobs.worker.inflight) == 1
    assert await jobs.store.concurrency_count("wk_capped") == 1
    g.set()
    await jobs.drain()
    assert [(await r.status()).state for r in runs_] == ["succeeded"] * 3
    events = SEEN["capped"]
    # Never two running at once.
    running = 0
    for kind, _ in events:
        running += 1 if kind == "start" else -1
        assert running <= 1
    assert await jobs.store.concurrency_count("wk_capped") == 0


async def test_concurrency_cap_per_key(jobs):
    g = gate("key")
    a1 = await wk_per_key.options(identity=U1).enqueue(tenant="a", name="key")
    a2 = await wk_per_key.options(identity=U1).enqueue(tenant="a", name="key")
    b1 = await wk_per_key.options(identity=U1).enqueue(tenant="b", name="key")
    await jobs.worker.poll_once()
    await asyncio.sleep(0)
    held = set(jobs.worker.inflight)
    assert held == {a1.id, b1.id}
    assert (await a2.status()).cap_waits == 1
    g.set()
    await jobs.drain()
    for r in (a1, a2, b1):
        assert (await r.status()).state == "succeeded"


async def test_priority_order(jobs):
    SEEN.pop("order", None)
    for n, p in ((1, "low"), (2, "normal"), (3, "high"), (4, "high")):
        await wk_order.options(identity=U1, priority=p).enqueue(n=n)
    small = Worker(jobs.runtime, concurrency=1, queues=jobs.transport.queue_names)
    for _ in range(4):
        await small.poll_once()
        await until(lambda: not small.inflight)
    assert SEEN["order"] == [3, 4, 2, 1]


async def test_result_validation_fails_without_retry(jobs):
    run = await wk_bad_result.options(identity=U1).enqueue()
    await jobs.drain()
    status = await run.status()
    assert status.state == "failed"
    assert status.error["code"] == "result_invalid"


async def test_result_size_limit():
    @task(name="wk_big_result")
    async def big() -> str:
        return "x" * 500

    try:
        async with jobs_harness(max_result_bytes=100) as jobs:
            run = await big.options(identity=U1).enqueue()
            await jobs.drain()
            status = await run.status()
            assert status.state == "failed"
            assert status.error["code"] == "result_too_large"
    finally:
        from tenxgraph_api.jobs.core.registry import unregister

        unregister("wk_big_result")


async def test_stored_kwargs_revalidated_in_worker(jobs):
    created = await jobs.store.create_run(
        NewRun(run_id="bad-kwargs", task="wk_ok", kwargs={"x": "nan?"}, identity=U1)
    )
    assert created.due
    await jobs.sweep()
    await jobs.drain()
    status = await jobs.store.get_run("bad-kwargs")
    assert status.state == "failed"
    assert status.error["code"] == "validation"


async def test_unknown_task_stays_queued_for_another_pod(jobs, hook):
    """Rolling deploy: a worker without the task leaves the run queued and republishes it
    later; a pod that has the task then runs it."""
    from tenxgraph_api.jobs.worker.worker import UNKNOWN_TASK_RETRY

    await jobs.store.create_run(NewRun(run_id="ghost", task="wk_not_registered", identity=U1))
    await jobs.drain()
    status = await jobs.store.get_run("ghost")
    assert status.state == "queued"
    assert status.attempt == 0
    assert status.next_publish_at == pytest.approx(jobs.clock.now() + UNKNOWN_TASK_RETRY)
    assert jobs.transport.unacked() == 0
    assert hook.names("ghost") == []

    @task(name="wk_not_registered")
    async def arrived() -> str:
        return "new pod"

    try:
        jobs.clock.advance(UNKNOWN_TASK_RETRY)
        await jobs.drain()
        assert (await jobs.store.get_run("ghost")).result == "new pod"
    finally:
        from tenxgraph_api.jobs.core.registry import unregister

        unregister("wk_not_registered")


async def test_hook_errors_never_change_the_run(caplog):
    class Broken:
        async def on_task_start(self, run):
            raise RuntimeError("start hook broke")

        async def on_task_success(self, run, result):
            raise RuntimeError("success hook broke")

        def on_task_queued(self, run):
            raise RuntimeError("sync hook broke")

    async with jobs_harness(hooks=[Broken()]) as jobs:
        run = await wk_ok.options(identity=U1).enqueue(x=1)
        await jobs.drain()
        assert (await run.status()).state == "succeeded"
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "Broken.on_task_start" in messages
    assert "Broken.on_task_success" in messages
    assert "Broken.on_task_queued" in messages


async def test_shutdown_requeues_running_attempt(jobs):
    gate("shutdown")
    run = await wk_wait.options(identity=U1).enqueue(name="shutdown")
    await jobs.worker.poll_once()
    await until(lambda: run.id in SEEN)
    await jobs.worker.aclose(grace=0.01)
    status = await run.status()
    assert status.state == "queued"
    assert status.failure_count == 0
    assert status.error["code"] == "interrupted"
    jobs.worker.resume()
    GATES["shutdown"].set()
    await jobs.drain()
    assert (await run.status()).state == "succeeded"


async def test_background_run_loop():
    async with jobs_harness() as jobs:
        worker = Worker(
            jobs.runtime, queues=["default"], worker_id="bg", lease_ttl=3, flush_interval=0
        )
        loop = asyncio.create_task(worker.run(block=0.01, tick_interval=0.01))
        handles = [await wk_ok.options(identity=U1).enqueue(x=i) for i in range(5)]
        results = await asyncio.gather(*(h.result(timeout=5) for h in handles))
        assert [r["x"] for r in results] == list(range(5))
        await worker.aclose(grace=1)
        await asyncio.wait_for(loop, timeout=5)


async def test_worker_full_waits_for_a_slot():
    gate("full")
    async with jobs_harness() as jobs:
        worker = Worker(jobs.runtime, queues=["default"], concurrency=1, worker_id="w")
        loop = asyncio.create_task(worker.run(block=0.01, tick_interval=0.01))
        first = await wk_wait.options(identity=U1).enqueue(name="full")
        second = await wk_ok.options(identity=U1).enqueue(x=1)
        await until(lambda: first.id in SEEN, rounds=2000)
        await asyncio.sleep(0.05)
        assert (await second.status()).state == "queued"
        GATES["full"].set()
        await second.result(timeout=5)
        await worker.aclose(grace=1)
        await asyncio.wait_for(loop, timeout=5)


async def test_worker_rejects_bad_concurrency(jobs):
    with pytest.raises(ValueError):
        Worker(jobs.runtime, concurrency=0)


async def test_stalled_worker_reclaims_its_own_run(jobs):
    """The sweeper requeues a run while its worker still runs the old attempt; the same
    worker then receives the run again: it claims attempt 2 and drops attempt 1."""
    g = gate("self")
    run = await wk_lossy.options(identity=U1).enqueue(name="self")
    await jobs.worker.poll_once()
    await until(lambda: run.id in SEEN)
    jobs.worker.heartbeats = False
    jobs.clock.advance(seconds=jobs.runtime.lease_ttl + 1)
    await jobs.sweep()  # requeued and republished
    jobs.worker.heartbeats = True
    assert await jobs.worker.poll_once() == 1
    assert jobs.worker.inflight[run.id].run.attempt == 2
    g.set()
    await jobs.drain()
    status = await run.status()
    assert status.state == "succeeded"
    assert status.result == 2
    assert status.lost_count == 1
