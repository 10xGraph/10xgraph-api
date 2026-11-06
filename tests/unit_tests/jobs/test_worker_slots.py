"""Worker queues, reserved slots, thread and process executors, unstorable results."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import pytest

from tenxgraph_api.jobs import task
from tenxgraph_api.jobs.errors import ValueNotStorable
from tenxgraph_api.jobs.executors import ProcessExecutor, ThreadExecutor
from tenxgraph_api.jobs.models import PRIORITIES
from tenxgraph_api.jobs.testing import jobs_harness
from tenxgraph_api.jobs.transport.base import Delivery, TransportMessage
from tenxgraph_api.jobs.transport.memory import MemoryTransport
from tenxgraph_api.jobs.worker import Worker
from tests.unit_tests.jobs import worker_process_tasks as wpt


pytestmark = pytest.mark.asyncio

U1 = {"user_id": "u1"}
GATE: dict[str, asyncio.Event] = {}


@task(name="ws_block", queue="slots")
async def ws_block(tag: str) -> str:
    await GATE["open"].wait()
    return tag


@task(name="ws_on_a", queue="qa")
async def ws_on_a() -> str:
    return "a"


@task(name="ws_on_b", queue="qb")
async def ws_on_b() -> str:
    return "b"


THREAD_SEEN: list[str] = []


@task(name="ws_thread", executor="thread")
def ws_thread(x: int) -> int:
    import threading

    THREAD_SEEN.append(threading.current_thread().name)
    return x + 1


class FilteringTransport(MemoryTransport):
    """A memory transport whose consume takes ``priorities=`` (like a filtered read)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[int, list[str] | None]] = []

    async def consume(
        self,
        queues: Sequence[str],
        *,
        max_messages: int,
        block: float = 0.0,
        priorities: Sequence[str] | None = None,
    ):
        self.calls.append((max_messages, None if priorities is None else list(priorities)))
        if priorities is None:
            return await super().consume(queues, max_messages=max_messages, block=block)
        out = []
        for level in PRIORITIES:
            if level not in priorities:
                continue
            for queue in queues:
                lane = self._lanes(queue)[level]
                while lane and len(out) < max_messages:
                    wire = lane.popleft()
                    receipt = next(self._receipts)
                    self._pending[receipt] = wire
                    out.append(
                        Delivery(message=TransportMessage.from_dict(wire), receipt=receipt)
                    )
        return out


async def test_worker_consumes_only_its_queues():
    async with jobs_harness() as jobs:
        jobs.worker._queues = ["qa"]
        a = await ws_on_a.options(identity=U1).enqueue()
        b = await ws_on_b.options(identity=U1).enqueue()
        await jobs.drain()
        assert (await a.status()).state.value == "succeeded"
        assert (await b.status()).state.value == "queued"
        assert jobs.worker.queues() == ["qa"]


@pytest.mark.parametrize("filtered", [False, True])
async def test_reserved_slots_keep_room_for_high(filtered):
    GATE["open"] = asyncio.Event()
    async with jobs_harness() as jobs:
        if filtered:
            transport = FilteringTransport()
            jobs.runtime.transport = transport
            jobs.transport = transport
        worker = Worker(
            jobs.runtime,
            queues=["slots"],
            concurrency=3,
            reserve={"high": 1},
            monotonic=jobs.clock,
            worker_id="w-reserve",
        )
        normals = [await ws_block.options(identity=U1).enqueue(tag=f"n{i}") for i in range(3)]
        assert await worker.poll_once() == 2  # the third slot is for high only
        assert len(worker.inflight) == 2
        states = [(await r.status()).state.value for r in normals]
        assert states.count("running") == 2
        assert states.count("queued") == 1
        # Without filtered reads the third normal run was read and given back.
        assert worker.released == (0 if filtered else 1)
        high = await ws_block.options(identity=U1, priority="high").enqueue(tag="h")
        assert await worker.poll_once() == 1
        assert (await high.status()).state.value == "running"
        assert worker.free_slots == 0
        if filtered:
            assert any(p == ["high"] for _, p in transport.calls)
        GATE["open"].set()
        await asyncio.wait(worker.jobs())
        jobs.clock.advance(5)  # the given-back run is due for its next publish
        await jobs.sweep()
        assert await worker.poll_once() == 1
        await asyncio.wait(worker.jobs())
        await worker.aclose(grace=1)
        assert all((await r.status()).state.value == "succeeded" for r in [*normals, high])


async def test_thread_task_runs_in_the_thread_executor():
    async with jobs_harness() as jobs:
        executor = ThreadExecutor(2)
        await executor.astart()
        jobs.worker.executors = {"thread": executor}
        try:
            run = await ws_thread.options(identity=U1).enqueue(x=41)
            await jobs.drain()
            assert (await run.status()).state.value == "succeeded"
            assert await run.result() == 42
            assert THREAD_SEEN[-1].startswith("tenxgraph-task")
        finally:
            await executor.aclose(grace=1)


async def test_a_worker_without_the_executor_leaves_the_run_queued():
    async with jobs_harness() as jobs:
        run = await ws_thread.options(identity=U1).enqueue(x=1)
        await jobs.worker.poll_once()
        assert (await run.status()).state.value == "queued"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process executor")
async def test_process_task_end_to_end_and_timeout_is_retried():
    async with jobs_harness() as jobs:
        executor = ProcessExecutor(2, preload=["tests.unit_tests.jobs.worker_process_tasks"])
        await executor.astart()
        jobs.worker.executors = {"process": executor}
        try:
            ok = await wpt.wp_add.options(identity=U1).enqueue(a=2, b=3)
            slow = await wpt.wp_slow_first.options(identity=U1).enqueue()
            await jobs.drain(follow_delays=True, timeout=60)
            assert await ok.result() == 5
            run = await slow.status()
            assert run.state.value == "succeeded"
            assert run.attempt == 2
            assert run.failure_count == 1
            assert run.result == "second attempt"
        finally:
            await executor.aclose(grace=1)


@task(name="ws_nan")
async def ws_nan() -> float:
    return 1.0


async def test_unstorable_result_fails_without_retry(monkeypatch):
    async with jobs_harness() as jobs:
        real = jobs.store.transition
        calls = []

        async def transition(run_id, *, attempt, worker_id, outcome):
            calls.append(outcome.kind)
            if outcome.kind == "succeeded":
                raise ValueNotStorable("result holds NaN")
            return await real(run_id, attempt=attempt, worker_id=worker_id, outcome=outcome)

        monkeypatch.setattr(jobs.store, "transition", transition)
        run = await ws_nan.options(identity=U1).enqueue()
        await jobs.drain()
        stored = await run.status()
        assert stored.state.value == "failed"
        assert stored.error["code"] == "result_not_serializable"
        assert "holds NaN" not in stored.error["message"]
        assert calls == ["succeeded", "failed"]
