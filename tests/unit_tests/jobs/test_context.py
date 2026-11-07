"""TaskContext, the reserved-argument check, enqueue, cancel and the schedule helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from injectq.integrations.taskiq import InjectTaskiq
from taskiq import Context, TaskiqDepends

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.context import (
    ReservedArgumentError,
    ScheduleNotFoundError,
    TaskContext,
    cancel,
    check_arguments,
    enqueue,
    reschedule,
    schedule,
    unschedule,
)

from .conftest import Service


pytestmark = pytest.mark.asyncio
CONFIG = {"user_id": "u1", "user": {"name": "Ann"}, "authz": {"ok": True}}


def make_report(env):
    @env.broker.task
    async def report(
        report_id: int,
        config: dict,
        graph: Annotated[Service, InjectTaskiq(Service)],
        ctx: Annotated[TaskContext, TaskiqDepends()],
        *,
        label: str = "x",
    ) -> dict:
        await ctx.progress({"step": "loading"})
        return {
            "id": report_id,
            "config": config,
            "ctx_config": ctx.config,
            "graph": graph is env.service,
            "cancelled": await ctx.cancelled(),
        }

    return report


@pytest.mark.parametrize(
    ("args", "kwargs", "message"),
    [
        ((1,), {"graph": "EVIL"}, "set by the server"),
        ((1,), {"ctx": "EVIL"}, "set by the server"),
        ((1,), {"config": {}}, "set by the server"),
        ((1,), {"user_id": "u2"}, "set by the server"),
        ((1,), {"authz": {}}, "set by the server"),
        ((1,), {"bogus": 1}, "has no arguments"),
        ((1, {}), {}, "at most 1 positional"),
        ((1,), {"report_id": 2}, "given twice"),
    ],
)
async def test_check_arguments_rejects(env, args, kwargs, message):
    task = make_report(env)
    with pytest.raises(ReservedArgumentError, match=message):
        check_arguments(task, args, kwargs)


async def test_check_arguments_accepts_plain_and_spots_all_injection_styles(env):
    @env.broker.task
    async def styles(
        a: int,
        c: Context = TaskiqDepends(),
        s: Service = InjectTaskiq(Service),
        **extra: int,
    ) -> None: ...

    check_arguments(make_report(env), (1,), {"label": "y"})
    check_arguments(styles, (1,), {"anything": 2})  # **extra takes unknown names
    for name in ("c", "s"):
        with pytest.raises(ReservedArgumentError):
            check_arguments(styles, (), {name: 1})


async def test_enqueue_passes_config_records_owner_and_progress(env):
    task = make_report(env)
    handle = await enqueue(task, 7, config=CONFIG, label="z")
    result = await handle.wait_result(timeout=5)
    assert not result.is_err, result.error
    value = result.return_value
    expected = {**CONFIG, "task_id": handle.task_id}
    assert value["config"] == expected
    assert value["ctx_config"] == expected
    assert value["graph"] is True
    assert value["cancelled"] is False
    progress = await env.broker.result_backend.get_progress(handle.task_id)
    assert progress.meta == {"step": "loading"}
    record = await state.read_status(env.store, handle.task_id)
    assert (record.status, record.owner, record.task_name) == ("success", "u1", task.task_name)


async def test_enqueue_refuses_before_sending(env):
    task = make_report(env)
    with pytest.raises(ReservedArgumentError):
        await enqueue(task, 1, graph="EVIL")
    with pytest.raises(ReservedArgumentError):
        await enqueue(task, 1, 2)


async def test_enqueue_without_config_and_sync_task(env):
    @env.broker.task
    def plain(a: int) -> int:
        return a * 2

    @env.broker.task
    def sync_cfg(a: int, config: dict, ctx: Annotated[TaskContext, TaskiqDepends()]) -> list:
        return [a, config, ctx.config, ctx.task_name]

    result = await (await enqueue(plain, 4, config=CONFIG)).wait_result(timeout=5)
    assert result.return_value == 8
    handle = await enqueue(sync_cfg, 1)
    result = await handle.wait_result(timeout=5)
    only_id = {"task_id": handle.task_id}
    assert result.return_value == [1, only_id, only_id, sync_cfg.task_name]


async def test_cancel_marks_only_a_queued_task(env):
    await state.write_status(
        env.store, state.TaskStatus(task_id="q", task_name="t", status="queued")
    )
    await state.write_status(
        env.store, state.TaskStatus(task_id="r", task_name="t", status="running")
    )
    await cancel("q", env.broker)
    await cancel("r", env.broker)
    assert (await state.read_status(env.store, "q")).status == "cancelled"
    assert (await state.read_status(env.store, "r")).status == "running"
    assert await state.is_cancel_flag_set(env.store, "r")


async def test_schedule_reschedule_unschedule(env, source):
    task = make_report(env)
    schedule_id = await schedule(task, 3, cron="0 9 * * 1", config=CONFIG, label="w")
    stored = source.items[schedule_id]
    assert (stored.cron, stored.args, stored.kwargs) == ("0 9 * * 1", [3], {"label": "w"})
    assert stored.task_id is None  # never with_task_id on a schedule
    assert state.parse_config(stored.labels) == CONFIG
    assert (await state.read_schedule(env.store, schedule_id)).owner == "u1"

    await reschedule(schedule_id, every=60, broker=env.broker)
    changed = source.items[schedule_id]
    assert (changed.cron, changed.interval, changed.args) == (None, 60, [3])
    assert (await state.read_schedule(env.store, schedule_id)).task.interval == 60

    at = datetime.now(UTC) + timedelta(hours=1)
    await reschedule(schedule_id, at=at, broker=env.broker)
    assert source.items[schedule_id].time == at

    await unschedule(schedule_id, env.broker)
    assert schedule_id not in source.items
    assert await state.read_schedule(env.store, schedule_id) is None
    with pytest.raises(ScheduleNotFoundError):
        await reschedule(schedule_id, cron="* * * * *", broker=env.broker)


async def test_schedule_checks_timing_and_arguments(env, source):
    task = make_report(env)
    with pytest.raises(ValueError, match="exactly one"):
        await schedule(task, 1, cron="* * * * *", every=5)
    with pytest.raises(ReservedArgumentError):
        await schedule(task, 1, cron="* * * * *", graph="EVIL")
    assert source.items == {}
