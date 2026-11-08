"""TaskContext, the reserved-argument check and cancel."""

from __future__ import annotations

import json
from typing import Annotated

import pytest
from injectq.integrations.taskiq import InjectTaskiq
from taskiq import Context, TaskiqDepends

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import ReservedArgumentError, check_arguments
from tenxgraph_api.jobs.context import TaskContext, cancel

from .conftest import Service


pytestmark = pytest.mark.asyncio
CONFIG = {"user_id": "u1", "user": {"name": "Ann"}}


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
        ((1, {}), {}, "at most 1 positional"),
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


def with_config(task, config):
    return task.kicker().with_labels(**{state.CONFIG_LABEL: json.dumps(config)})


async def test_kiq_with_config_label_reaches_task_and_records_owner(env):
    task = make_report(env)
    handle = await with_config(task, CONFIG).kiq(7, label="z")
    result = await handle.wait_result(timeout=5)
    assert not result.is_err, result.error
    value = result.return_value
    expected = {**CONFIG, "task_id": handle.task_id}
    assert value["config"] == expected
    assert value["ctx_config"] == expected
    assert value["graph"] is True
    assert value["cancelled"] is False
    record = await state.read_status(env.store, handle.task_id)
    assert (record.status, record.owner, record.task_name) == ("success", "u1", task.task_name)


async def test_kiq_without_config_and_sync_task(env):
    @env.broker.task
    def plain(a: int) -> int:
        return a * 2

    @env.broker.task
    def sync_cfg(a: int, config: dict, ctx: Annotated[TaskContext, TaskiqDepends()]) -> list:
        return [a, config, ctx.config, ctx.task_name]

    result = await (await with_config(plain, CONFIG).kiq(4)).wait_result(timeout=5)
    assert result.return_value == 8
    handle = await sync_cfg.kiq(1)
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
