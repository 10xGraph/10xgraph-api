"""ctx.emit: buffering, coalescing, percent rules, flush rate and fencing."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tenxgraph_api.jobs import TaskContext, task
from tenxgraph_api.jobs.core.clock import FakeClock
from tenxgraph_api.jobs.core.emit import TaskEmitter


U1 = {"user_id": "u1"}
GATE: dict[str, asyncio.Event] = {}


def emitter() -> tuple[TaskEmitter, FakeClock]:
    clock = FakeClock(1000.0)
    return TaskEmitter(clock=clock), clock


def test_clean_buffer_takes_nothing():
    em, _ = emitter()
    assert not em.dirty
    assert em.take() is None


def test_steps_and_durations():
    em, clock = emitter()
    em.steps(["download", "transcribe"])
    with em.step("download"):
        clock.advance(3)
    events, snap = em.take()
    assert [e.type for e in events] == ["steps", "step.start", "step.end"]
    assert events[2].data == {
        "step": "download",
        "outcome": "succeeded",
        "duration": 3.0,
        "percent": None,
    }
    steps = {s["name"]: s for s in snap["steps"]}
    assert steps["download"]["state"] == "succeeded"
    assert steps["download"]["duration"] == 3.0
    assert steps["transcribe"]["state"] == "pending"
    assert snap["step"] is None


def test_failed_step_outcome():
    em, _ = emitter()
    with pytest.raises(RuntimeError), em.step("boom"):
        raise RuntimeError("x")
    events, snap = em.take()
    assert events[-1].data["outcome"] == "failed"
    assert snap["steps"][0]["state"] == "failed"


@pytest.mark.asyncio
async def test_async_step():
    em, clock = emitter()
    async with em.step("a"):
        clock.advance(1)
    events, _ = em.take()
    assert [e.type for e in events] == ["step.start", "step.end"]


def test_progress_coalesced_into_one_event():
    em, _ = emitter()
    for i in range(10_000):
        em.progress(f"chunk {i}", percent=i / 100)
    events, snap = em.take()
    assert len(events) == 1
    assert events[0].type == "progress"
    assert events[0].data == {"message": "chunk 9999", "percent": 99.99}
    assert snap["message"] == "chunk 9999"
    assert em.take() is None


def test_percent_never_decreases_and_is_clamped():
    em, _ = emitter()
    em.progress("a", percent=40)
    em.progress("b", percent=20)
    assert em.take()[1]["percent"] == 40
    em.progress(percent=500)
    assert em.take()[1]["percent"] == 100
    em2, _ = emitter()
    em2.progress(percent=-5)
    assert em2.take()[1]["percent"] == 0


def test_weights_drive_percent():
    em, _ = emitter()
    with em.step("a", weight=0.25):
        pass
    assert em.take()[1]["percent"] == 25
    em.progress("explicit", percent=60)
    with em.step("b", weight=0.25):
        pass
    # finished weights (50) are below the explicit 60: never lower than shown
    assert em.take()[1]["percent"] == 60
    with em.step("c", weight=0.5):
        pass
    assert em.take()[1]["percent"] == 100
    with pytest.raises(ValueError):
        em.step("bad", weight=2)


def test_eta_log_and_data():
    em, _ = emitter()
    em.eta(240)
    em.log("cache miss", level="warning", data={"key": object})
    em.progress("p", data={"n": 1})
    events, snap = em.take()
    assert [e.type for e in events] == ["eta", "log", "progress"]
    assert events[1].data["level"] == "warning"
    assert isinstance(events[1].data["data"]["key"], str)  # non-JSON values become strings
    assert events[2].data["data"] == {"n": 1}
    assert snap["eta"] == 240
    with pytest.raises(ValueError):
        em.log("x", level="loud")


def test_closed_emitter_ignores_calls():
    em, _ = emitter()
    em.close()
    em.progress("late")
    em.log("late")
    em.steps(["x"])
    em.eta(1)
    with em.step("x"):
        pass
    assert em.take() is None


def test_thread_safe_from_many_threads():
    em, _ = emitter()

    def work():
        for _ in range(500):
            em.log("t")
            em.progress("p")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events, _ = em.take()
    assert len([e for e in events if e.type == "log"]) == 4000
    assert len([e for e in events if e.type == "progress"]) == 1


@task
async def em_loop(name: str, ctx: TaskContext) -> None:
    ctx.emit.steps(["work"])
    with ctx.emit.step("work", weight=1.0):
        for i in range(1000):
            ctx.emit.progress(f"item {i}", percent=i / 10)
        await GATE[name].wait()


@pytest.mark.asyncio
async def test_worker_flushes_at_most_once_per_second(jobs):
    GATE["flush"] = asyncio.Event()
    run = await em_loop.options(identity=U1).enqueue(name="flush")
    await jobs.worker.poll_once()
    for _ in range(5):
        await asyncio.sleep(0)
    writes = 0
    real = jobs.store.append_events

    async def counting(*args, **kwargs):
        nonlocal writes
        writes += 1
        return await real(*args, **kwargs)

    jobs.store.append_events = counting
    await jobs.worker.tick()
    assert writes == 1
    snap = (await run.status()).progress
    assert snap["message"] == "item 999"
    assert snap["step"] == "work"
    # More progress within the same second: buffered, not written.
    jobs.worker.inflight[run.id].ctx.emit.progress("more")
    await jobs.worker.tick()
    await jobs.worker.tick()
    assert writes == 1
    jobs.clock.advance(1.0)
    await jobs.worker.tick()
    assert writes == 2
    GATE["flush"].set()
    await jobs.drain()
    status = await run.status()
    assert status.state == "succeeded"
    assert status.progress["percent"] == 100
    assert writes == 3  # the final flush when the attempt ended
    types = [e.type async for e in run.events()]
    assert types == ["steps", "step.start", "progress", "progress", "step.end"]


@pytest.mark.asyncio
async def test_stale_flush_is_rejected(jobs):
    GATE["stale"] = asyncio.Event()
    run = await em_loop.options(identity=U1).enqueue(name="stale")
    await jobs.worker.poll_once()
    for _ in range(5):
        await asyncio.sleep(0)
    jobs.worker.heartbeats = False
    jobs.clock.advance(jobs.runtime.lease_ttl + 1)
    await jobs.sweep()
    jobs.worker.heartbeats = True
    attempt = jobs.worker.inflight[run.id]
    assert not await jobs.worker._flush(attempt)
    assert attempt.fenced_out
    assert (await run.status()).progress == {}
    assert [e async for e in run.events()] == []
    GATE["stale"].set()
    await jobs.drain()
    assert (await run.status()).state == "succeeded"
    assert {e.attempt async for e in run.events()} == {2}
