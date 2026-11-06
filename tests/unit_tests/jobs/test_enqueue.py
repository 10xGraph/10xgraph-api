"""Enqueue (by object and by name), options, identity, run handle and lookup."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from tenxgraph_api.jobs import (
    IdentityRequired,
    JobsNotConfigured,
    PayloadTooLarge,
    RunNotFound,
    TaskCancelled,
    TaskContext,
    TaskFailed,
    TaskNotFound,
    TaskValidationError,
    enqueue,
    get_task,
    runs,
    system_identity,
    task,
)
from tenxgraph_api.jobs.testing import jobs_harness


pytestmark = pytest.mark.asyncio

U1 = {"user_id": "u1"}
CHILDREN: dict[str, str] = {}


@task
async def eq_echo(value: str, key: str = "", delay: int = 0) -> dict:
    return {"value": value, "key": key, "delay": delay}


@task(priority="low", queue="eq-q")
async def eq_low(n: int) -> int:
    return n


@task
async def eq_parent(ctx: TaskContext) -> str:
    child = await eq_echo.enqueue(value="from-parent")
    CHILDREN[ctx.run_id] = child.id
    return child.id


@task
async def eq_boom() -> None:
    raise RuntimeError("nope")


@task
async def eq_blob(blob: str) -> None: ...


async def test_enqueue_writes_and_publishes(jobs, hook):
    run = await eq_echo.options(identity=jobs.user("u1")).enqueue(value="a")
    status = await run.status()
    assert status.state == "queued"
    assert status.task == "eq_echo"
    assert status.kwargs == {"value": "a"}
    assert status.identity == {"user_id": "u1"}
    assert status.owner_user_id == "u1"
    assert status.trigger == {"type": "code"}
    assert status.published_at is not None
    assert status.retention == 604800.0
    assert jobs.transport.size() == 1
    assert hook.names(run.id) == ["queued"]
    await jobs.drain()
    assert await run.result(timeout=1) == {"value": "a", "key": "", "delay": 0}


async def test_task_params_named_like_options(jobs):
    run = await eq_echo.options(identity=U1, key="idem").enqueue(value="v", key="arg", delay=5)
    await jobs.drain()
    assert await run.result() == {"value": "v", "key": "arg", "delay": 5}
    assert (await run.status()).idempotency_key == "idem"


async def test_identity_required_outside_task(jobs):
    with pytest.raises(IdentityRequired):
        await eq_echo.enqueue(value="a")
    with pytest.raises(IdentityRequired):
        await eq_echo.options(identity={"user_id": ""}).enqueue(value="a")
    with pytest.raises(IdentityRequired):
        await eq_echo.options(identity=42).enqueue(value="a")


async def test_identity_shapes(jobs):
    class Obj:
        user_id = 7

    run = await eq_echo.options(identity=Obj()).enqueue(value="a")
    assert (await run.status()).owner_user_id == "7"
    run = await eq_echo.options(identity=system_identity("livekit")).enqueue(value="a")
    assert (await run.status()).owner_user_id == "__system__:livekit"
    assert jobs.user("u9", org_id="o1") == {"user_id": "u9", "org_id": "o1"}


async def test_identity_and_parent_inherited_inside_task(jobs):
    parent = await eq_parent.options(identity={"user_id": "boss", "role": "admin"}).enqueue()
    await jobs.drain()
    child_id = await parent.result()
    child = await (await runs.get(child_id)).status()
    assert child.identity == {"user_id": "boss", "role": "admin"}
    assert child.parent_run_id == parent.id
    assert child.state == "succeeded"


async def test_enqueue_by_name(jobs):
    run = await enqueue("eq_echo", {"value": "n"}, identity=U1, key="evt-1")
    again = await enqueue("eq_echo", {"value": "n2"}, identity=U1, key="evt-1")
    assert again.id == run.id
    assert get_task("eq_echo") is eq_echo
    with pytest.raises(TaskNotFound):
        await enqueue("eq_missing", {}, identity=U1)
    with pytest.raises(TaskValidationError):
        await enqueue("eq_echo", {"value": 1, "bogus": 2}, identity=U1)


async def test_validation_and_size_limit_at_enqueue():
    async with jobs_harness(max_kwargs_bytes=100) as jobs:
        with pytest.raises(TaskValidationError):
            await eq_echo.options(identity=U1).enqueue(value=["not", "a", "str"])
        with pytest.raises(PayloadTooLarge):
            await eq_blob.options(identity=U1).enqueue(blob="x" * 200)
        assert (await runs.list()).items == []
        assert jobs.transport.size() == 0


async def test_key_keep_and_replace(jobs, hook):
    first = await eq_echo.options(identity=U1, key="k").enqueue(value="one")
    kept = await eq_echo.options(identity=U1, key="k").enqueue(value="two")
    assert kept.id == first.id
    assert (await first.status()).kwargs == {"value": "one"}
    replaced = await eq_echo.options(identity=U1, key="k", key_mode="replace").enqueue(
        value="three"
    )
    assert replaced.id == first.id
    assert (await first.status()).kwargs == {"value": "three"}
    assert hook.names(first.id) == ["queued"]
    other_user = await eq_echo.options(identity={"user_id": "u2"}, key="k").enqueue(value="x")
    assert other_user.id != first.id
    await jobs.drain()
    assert (await first.result())["value"] == "three"
    late = await eq_echo.options(identity=U1, key="k", key_mode="replace").enqueue(value="four")
    assert late.id == first.id
    assert (await first.status()).kwargs == {"value": "three"}


async def test_priority_override_and_queue(jobs):
    run = await eq_low.options(identity=U1).enqueue(n=1)
    high = await eq_low.options(identity=U1, priority="high").enqueue(n=2)
    assert (await run.status()).priority == "low"
    assert (await run.status()).queue == "eq-q"
    assert (await high.status()).priority == "high"


async def test_delay_and_at(jobs):
    delayed = await eq_echo.options(identity=U1, delay=30).enqueue(value="later")
    when = jobs.clock.datetime() + dt.timedelta(minutes=5)
    at_run = await eq_echo.options(identity=U1, at=when).enqueue(value="at")
    assert (await delayed.status()).published_at is None
    assert jobs.transport.size() == 0
    await jobs.drain()
    assert (await delayed.status()).state == "queued"
    jobs.clock.advance(seconds=31)
    await jobs.drain()
    assert (await delayed.status()).state == "succeeded"
    assert (await at_run.status()).state == "queued"
    jobs.clock.advance(minutes=5)
    await jobs.drain()
    assert (await at_run.status()).state == "succeeded"
    with pytest.raises(ValueError, match="naive"):
        await eq_echo.options(identity=U1, at=dt.datetime(2030, 1, 1)).enqueue(value="x")


async def test_drain_follow_delays(jobs):
    run = await eq_echo.options(identity=U1, delay=3600).enqueue(value="x")
    await jobs.drain(follow_delays=True)
    assert (await run.status()).state == "succeeded"


async def test_result_raises(jobs):
    bad = await eq_boom.options(identity=U1).enqueue()
    await jobs.drain()
    with pytest.raises(TaskFailed) as info:
        await bad.result()
    assert info.value.run.error["message"] == "nope"

    cancelled = await eq_echo.options(identity=U1).enqueue(value="x")
    await cancelled.cancel()
    with pytest.raises(TaskCancelled):
        await cancelled.result()

    waiting = await eq_echo.options(identity=U1, delay=60).enqueue(value="x")
    with pytest.raises(TimeoutError):
        await waiting.result(timeout=0.05)


async def test_runs_get_and_list(jobs):
    handles = [await eq_echo.options(identity=U1).enqueue(value=str(i)) for i in range(5)]
    jobs.clock.advance(1)
    other = await eq_low.options(identity={"user_id": "u2"}).enqueue(n=1)
    page = await runs.list(task="eq_echo", limit=2)
    seen = [r.run_id for r in page.items]
    while page.next_cursor:
        page = await runs.list(task="eq_echo", limit=2, cursor=page.next_cursor)
        seen += [r.run_id for r in page.items]
    assert sorted(seen) == sorted(h.id for h in handles)
    assert [r.run_id for r in (await runs.list(owner_user_id="u2")).items] == [other.id]
    assert (await runs.list(state="running")).items == []
    assert (await runs.get(other.id)).id == other.id
    with pytest.raises(RunNotFound):
        await runs.get("missing")


async def test_publish_failure_is_left_to_sweeper(jobs):
    real_publish = jobs.transport.publish
    calls = {"n": 0}

    async def flaky(message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("broker down")
        await real_publish(message)

    jobs.transport.publish = flaky
    run = await eq_echo.options(identity=U1).enqueue(value="x")
    assert (await run.status()).published_at is None
    await jobs.drain()
    assert (await run.status()).state == "succeeded"


async def test_not_configured():
    with pytest.raises(JobsNotConfigured):
        await eq_echo.options(identity=U1).enqueue(value="x")
    with pytest.raises(JobsNotConfigured):
        await runs.list()


async def test_status_after_purge():
    async with jobs_harness(result_ttl=10) as h:
        run = await eq_echo.options(identity=U1).enqueue(value="x")
        await h.drain()
        h.clock.advance(11)
        await h.sweep()
        with pytest.raises(RunNotFound):
            await run.status()
        with pytest.raises(RunNotFound):
            await run.cancel()


async def test_events_iterates_pages(jobs):
    @task(name="eq_many_logs")
    async def many(ctx: TaskContext) -> None:
        for i in range(250):
            ctx.emit.log(f"line {i}")

    try:
        run = await many.options(identity=U1).enqueue()
        await jobs.drain()
        seqs = [e.seq async for e in run.events(page_size=100)]
        assert seqs == list(range(1, 251))
        assert [e.seq async for e in run.events(after=240)] == list(range(241, 251))
    finally:
        from tenxgraph_api.jobs.registry import unregister

        unregister("eq_many_logs")


async def test_result_polls_until_done(jobs):
    run = await eq_echo.options(identity=U1).enqueue(value="poll")
    waiter = asyncio.create_task(run.result(timeout=5))
    await asyncio.sleep(0.01)
    await jobs.drain()
    assert (await waiter)["value"] == "poll"
