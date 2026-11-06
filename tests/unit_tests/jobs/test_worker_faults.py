"""Worker under faults: shutdown grace, store and transport errors, early stops, slow hooks,
local lease deadline, and the Redis key layout."""

from __future__ import annotations

import asyncio
import time

import pytest

from tenxgraph_api.jobs import TaskContext, task
from tenxgraph_api.jobs import worker as worker_module
from tenxgraph_api.jobs.emit import TaskEmitter
from tenxgraph_api.jobs.models import AttemptOutcome, RunState, TaskRun
from tenxgraph_api.jobs.runtime import JobsRuntime, use_runtime
from tenxgraph_api.jobs.store.base import REDIS_HASH_TAG, redis_key
from tenxgraph_api.jobs.store.memory import MemoryRunStore
from tenxgraph_api.jobs.sweeper import sweep_once
from tenxgraph_api.jobs.testing import jobs_harness
from tenxgraph_api.jobs.transport.memory import MemoryTransport
from tenxgraph_api.jobs.worker import Worker, _Attempt


pytestmark = pytest.mark.asyncio

U1 = {"user_id": "u1"}
BODY_RUNS: list[str] = []
GATES: dict[str, asyncio.Event] = {}


@task
async def ft_slow(seconds: float, ctx: TaskContext) -> str:
    BODY_RUNS.append(ctx.run_id)
    await asyncio.sleep(seconds)
    return "done"


@task
async def ft_quick(ctx: TaskContext) -> int:
    BODY_RUNS.append(ctx.run_id)
    return ctx.attempt


@task(retries=1, backoff=lambda n: 1 / 0)
async def ft_bad_backoff() -> None:
    raise RuntimeError("first failure")


@task
async def ft_progress(n: int, ctx: TaskContext) -> None:
    for i in range(n):
        ctx.emit.log(f"line {i}")
    await GATES["progress"].wait()


@task
async def ft_wait(name: str, ctx: TaskContext) -> str:
    BODY_RUNS.append(ctx.run_id)
    await GATES[name].wait()
    return "ok"


async def until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def real_runtime(**kw) -> JobsRuntime:
    return JobsRuntime(store=MemoryRunStore(), transport=MemoryTransport(), **kw)


# ---------------------------------------------------------------------- H1


async def test_aclose_keeps_renewing_during_grace():
    """Grace longer than the lease: the ticker keeps renewing, so no second worker runs it."""
    rt = real_runtime(lease_ttl=0.3)
    with use_runtime(rt):
        a = Worker(rt, queues=["default"], worker_id="A")
        loop = asyncio.create_task(a.run(block=0.05, tick_interval=0.05))
        handle = await ft_slow.options(identity=U1).enqueue(seconds=0.8)
        await until(lambda: handle.id in a.inflight)
        closing = asyncio.create_task(a.aclose(grace=5))
        await asyncio.sleep(0.6)  # two lease lengths into the grace period
        stats = await sweep_once(rt)
        b = Worker(rt, queues=["default"], worker_id="B")
        await b.poll_once()
        assert stats.lost == 0
        assert not b.inflight
        await closing
        await asyncio.wait_for(loop, timeout=5)
        run = await handle.status()
        assert run.state == RunState.SUCCEEDED
        assert run.attempt == 1
        assert run.lost_count == 0


# ---------------------------------------------------------------------- H2


async def test_run_loop_survives_consume_error():
    rt = real_runtime()
    with use_runtime(rt):
        real = rt.transport.consume
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("broker down")
            return await real(*args, **kwargs)

        rt.transport.consume = flaky
        w = Worker(rt, queues=["default"], worker_id="A")
        loop = asyncio.create_task(w.run(block=0.02, tick_interval=0.02))
        handle = await ft_quick.options(identity=U1).enqueue()
        assert await handle.result(timeout=5) == 1
        assert not loop.done()
        assert w.poll_errors == 1
        await w.aclose(grace=1)
        await asyncio.wait_for(loop, timeout=5)


async def test_run_loop_survives_claim_error():
    rt = real_runtime()
    with use_runtime(rt):
        real = rt.store.claim
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("store down")
            return await real(*args, **kwargs)

        rt.store.claim = flaky
        w = Worker(rt, queues=["default"], worker_id="A")
        loop = asyncio.create_task(w.run(block=0.02, tick_interval=0.02))
        handle = await ft_quick.options(identity=U1).enqueue()
        await until(lambda: w.poll_errors == 1)
        assert not loop.done()
        assert rt.transport.unacked() == 1  # left for redelivery
        rt.transport.requeue_unacked()  # what XAUTOCLAIM does for a real stream
        assert await handle.result(timeout=5) == 1
        await w.aclose(grace=1)
        await asyncio.wait_for(loop, timeout=5)


async def test_ack_error_after_claim_still_starts_attempt(jobs):
    async def broken_ack(deliveries):
        raise ConnectionError("ack lost")

    jobs.transport.ack = broken_ack
    handle = await ft_quick.options(identity=U1).enqueue()
    assert await jobs.worker.poll_once() == 1
    await jobs.drain()
    assert (await handle.status()).state == RunState.SUCCEEDED


# ---------------------------------------------------------------------- H3


async def test_transient_transition_error_is_retried(jobs):
    real = jobs.store.transition
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("blip")
        return await real(*args, **kwargs)

    jobs.store.transition = flaky
    handle = await ft_quick.options(identity=U1).enqueue()
    await jobs.worker.poll_once()
    jobs_ = jobs.worker.jobs()
    await jobs.drain()
    assert calls["n"] == 2
    status = await handle.status()
    assert status.state == RunState.SUCCEEDED
    assert all(j.done() and j.exception() is None for j in jobs_)


async def test_lost_reply_of_a_landed_write_counts_as_done(jobs, hook):
    real = jobs.store.transition

    async def lands_then_raises(*args, **kwargs):
        await real(*args, **kwargs)
        jobs.store.transition = real
        raise TimeoutError("reply lost")

    jobs.store.transition = lands_then_raises
    handle = await ft_quick.options(identity=U1).enqueue()
    await jobs.drain()
    status = await handle.status()
    assert status.state == RunState.SUCCEEDED
    assert hook.names(handle.id)[-1] == "success"


async def test_renewals_continue_while_outcome_write_retries(jobs):
    """A finishing attempt keeps its lease: renewals include it until the write lands."""
    real = jobs.store.transition
    failing = {"on": True}

    async def down(*args, **kwargs):
        if failing["on"]:
            raise ConnectionError("store down")
        return await real(*args, **kwargs)

    jobs.store.transition = down
    handle = await ft_quick.options(identity=U1).enqueue()
    await jobs.worker.poll_once()
    await until(lambda: jobs.worker.inflight[handle.id].finished)
    jobs.clock.advance(jobs.runtime.lease_ttl * 0.7)
    await jobs.worker.tick()
    jobs.clock.advance(jobs.runtime.lease_ttl * 0.7)  # past the first lease, renewed above
    await jobs.worker.tick()
    assert (await jobs.sweep()).lost == 0
    failing["on"] = False
    await jobs.drain()
    assert (await handle.status()).state == RunState.SUCCEEDED


async def test_raising_backoff_uses_default(jobs):
    handle = await ft_bad_backoff.options(identity=U1).enqueue()
    await jobs.drain()
    status = await handle.status()
    assert status.state == RunState.RETRYING
    assert 1.0 <= status.available_at - jobs.clock.now() <= 2.0  # default 2 s with jitter


async def test_final_flush_error_still_writes_result(jobs):
    async def broken(*args, **kwargs):
        raise ConnectionError("store down")

    jobs.store.append_events = broken

    @task(name="ft_logs_then_returns")
    async def logs(ctx: TaskContext) -> int:
        ctx.emit.log("hello")
        return 7

    try:
        handle = await logs.options(identity=U1).enqueue()
        await jobs.drain()
        assert (await handle.status()).result == 7
    finally:
        from tenxgraph_api.jobs.registry import unregister

        unregister("ft_logs_then_returns")


async def test_unexpected_worker_error_never_escapes(jobs, monkeypatch):
    def boom(self, attempt, inner):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(Worker, "_classify", boom)
    handle = await ft_quick.options(identity=U1).enqueue()
    await jobs.worker.poll_once()
    job = jobs.worker.jobs()[0]
    await jobs.drain()
    assert job.exception() is None
    status = await handle.status()
    assert status.state == RunState.FAILED
    assert status.error["code"] == "worker_error"


# ---------------------------------------------------------------------- M1


class GatedStart:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def on_task_start(self, run):
        self.entered.set()
        await self.release.wait()


@pytest.mark.parametrize("stop", ["cancel", "lost", "shutdown"])
async def test_stop_before_body_starts_skips_body(stop):
    gated = GatedStart()
    async with jobs_harness(hooks=[gated]) as jobs:
        handle = await ft_quick.options(identity=U1).enqueue()
        await jobs.worker.poll_once()
        await gated.entered.wait()
        if stop == "cancel":
            await handle.cancel()
            await jobs.worker.tick()
        elif stop == "lost":
            jobs.worker.heartbeats = False
            jobs.clock.advance(jobs.runtime.lease_ttl + 1)
            jobs.worker.check_deadlines()
        else:
            closing = asyncio.create_task(jobs.worker.aclose(grace=0))
            await asyncio.sleep(0.02)
        gated.release.set()
        await until(lambda: not jobs.worker.jobs())
        if stop == "shutdown":
            await closing
        assert handle.id not in BODY_RUNS
        status = await handle.status()
        expected = {
            "cancel": RunState.CANCELLED,
            "lost": RunState.RUNNING,  # nothing written; the sweeper recovers it
            "shutdown": RunState.QUEUED,
        }[stop]
        assert status.state == expected


# ---------------------------------------------------------------------- M2


class SlowProgress:
    def __init__(self) -> None:
        self.seen = 0

    async def on_task_progress(self, run, event):
        await asyncio.sleep(0.2)
        self.seen += 1


async def test_slow_progress_hook_does_not_delay_tick():
    hook = SlowProgress()
    GATES["progress"] = asyncio.Event()
    async with jobs_harness(hooks=[hook]) as jobs:
        handle = await ft_progress.options(identity=U1).enqueue(n=5)
        await jobs.worker.poll_once()
        await until(lambda: jobs.worker.inflight[handle.id].emitter.dirty)
        started = time.monotonic()
        await jobs.worker.tick()
        assert time.monotonic() - started < 0.1
        GATES["progress"].set()
        await jobs.drain()
        assert hook.seen == 5


async def test_progress_hook_queue_drops_when_full(monkeypatch, caplog):
    monkeypatch.setattr(worker_module, "HOOK_QUEUE_SIZE", 2)
    hook = SlowProgress()
    GATES["progress"] = asyncio.Event()
    async with jobs_harness(hooks=[hook]) as jobs:
        handle = await ft_progress.options(identity=U1).enqueue(n=10)
        await jobs.worker.poll_once()
        await until(lambda: jobs.worker.inflight[handle.id].emitter.dirty)
        await jobs.worker.tick()
        assert jobs.worker.hooks_dropped >= 7
        assert "dropped" in caplog.text
        GATES["progress"].set()
        await jobs.drain()


# ---------------------------------------------------------------------- M3


async def test_local_deadline_stops_attempt_when_renewals_fail(jobs):
    GATES["deadline"] = asyncio.Event()

    async def down(*args, **kwargs):
        raise ConnectionError("store unreachable")

    handle = await ft_wait.options(identity=U1).enqueue(name="deadline")
    await jobs.worker.poll_once()
    await until(lambda: handle.id in BODY_RUNS)
    real_renew = jobs.store.renew_leases
    jobs.store.renew_leases = down
    worker = jobs.worker
    jobs.clock.advance(worker.lease_ttl - worker.lease_margin - 1)
    await worker.tick()
    assert handle.id in worker.inflight  # still inside the local deadline
    jobs.clock.advance(2)
    writes = {"n": 0}
    real_transition = jobs.store.transition

    async def counting(*args, **kwargs):
        writes["n"] += 1
        return await real_transition(*args, **kwargs)

    jobs.store.transition = counting
    await worker.tick()
    assert handle.id not in worker.inflight
    await until(lambda: not worker.jobs())
    assert writes["n"] == 0  # stopped writing
    status = await handle.status()
    assert status.state == RunState.RUNNING  # the store lease has not expired yet
    assert status.lease_expires_at > jobs.clock.now()
    jobs.store.renew_leases = real_renew


# ---------------------------------------------------------------------- L2


async def test_shutdown_during_timeout_requeues(jobs):
    async def never():
        await asyncio.sleep(3600)

    inner = asyncio.create_task(never())
    inner.cancel()
    await asyncio.wait({inner})
    run = TaskRun(run_id="r", task="ft_quick", attempt=1)
    attempt = _Attempt(
        run=run,
        task=ft_quick,
        ctx=TaskContext(run_id="r", attempt=1, task="t", queue="q", identity=U1, emit=None),
        emitter=TaskEmitter(clock=time.time),
        timed_out=True,
        stop_reason="shutdown",
    )
    outcome = jobs.worker._classify(attempt, inner)
    assert outcome == AttemptOutcome(
        kind="requeue", error={"code": "interrupted", "message": "worker shut down"}
    )


# ---------------------------------------------------------------------- M4


async def test_redis_keys_share_one_hash_tag():
    keys = [redis_key("run", "abc"), redis_key("counter", "task\x1fkey"), redis_key("events", "x")]
    assert all(k.startswith(REDIS_HASH_TAG + ":") for k in keys)
    tags = {k[k.index("{") + 1 : k.index("}")] for k in keys}
    assert tags == {"tenxgraph-jobs"}
    with pytest.raises(ValueError):
        redis_key()
