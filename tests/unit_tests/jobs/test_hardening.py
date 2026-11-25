"""Status, retry, result and argument rules of the jobs layer (S3, L1 to L5, L8, L9, N1)."""

# ruff: noqa: S101

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated

import pytest
from taskiq import TaskiqDepends
from taskiq.exceptions import NoResultError

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import TaskArgumentError, check_arguments
from tenxgraph_api.jobs.config import WorkerConfig, warn_memory_state
from tenxgraph_api.jobs.context import TaskCancelled, cancel
from tenxgraph_api.jobs.middleware import TaskMiddleware
from tenxgraph_api.jobs.result_backend import StatusResultBackend
from tenxgraph_api.jobs.retry import DeliveryLimitError, TaskRetryMiddleware
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore

from .conftest import Env


pytestmark = pytest.mark.asyncio


def make_env(retries: int = 2) -> Env:
    return Env(InMemoryCacheStore(), TaskRetryMiddleware(retries=retries), TaskMiddleware())


async def kiq(task, *args, **kwargs):
    return await task.kicker().with_task_id(uuid.uuid4().hex).kiq(*args, **kwargs)


# S3


async def test_check_arguments_binds_to_the_signature(env):
    @env.broker.task
    async def job(a: int, b: int = 1, config: dict | None = None) -> int:
        return a + b

    check_arguments(job, [1], {"b": 2})
    for args, kwargs in (([], {}), ([1], {"c": 1}), ([1, 2], {"b": 3})):
        with pytest.raises(TaskArgumentError):
            check_arguments(job, args, kwargs)


async def test_worker_stops_a_run_whose_arguments_do_not_bind(env):
    @env.broker.task
    async def job(a: int) -> int:
        return a

    env.wrap()
    result = await (await kiq(job, bogus=1)).wait_result(timeout=5)
    assert result.is_err and isinstance(result.error, TaskArgumentError)


# L1, L2


async def test_retries_means_retries():
    env = make_env(retries=2)
    await env.broker.startup()
    runs: list[int] = []

    @env.broker.task(retry_on_error=True)
    async def always_fails() -> None:
        runs.append(1)
        raise RuntimeError("boom")

    env.wrap()
    handle = await kiq(always_fails)
    await asyncio.sleep(0.5)
    assert len(runs) == 3  # first run and 2 retries
    assert (await state.read_status(env.store, handle.task_id)).status == "failure"
    await env.broker.shutdown()


async def test_zero_retries_runs_once():
    env = make_env(retries=0)
    await env.broker.startup()
    runs: list[int] = []

    @env.broker.task(retry_on_error=True)
    async def always_fails() -> None:
        runs.append(1)
        raise RuntimeError("boom")

    env.wrap()
    await kiq(always_fails)
    await asyncio.sleep(0.3)
    assert len(runs) == 1
    await env.broker.shutdown()


async def test_cancelled_task_is_not_retried():
    env = make_env(retries=3)
    await env.broker.startup()
    runs: list[int] = []

    @env.broker.task(retry_on_error=True)
    async def job() -> None:
        runs.append(1)

    env.wrap()
    task_id = uuid.uuid4().hex
    await cancel(task_id, env.broker)
    handle = await job.kicker().with_task_id(task_id).kiq()
    result = await handle.wait_result(timeout=5)
    await asyncio.sleep(0.3)
    assert isinstance(result.error, TaskCancelled) and not runs
    assert (await state.read_status(env.store, task_id)).status == "cancelled"
    await env.broker.shutdown()


@pytest.mark.parametrize(
    "label_error",
    [(state.DELIVERY_LIMIT_LABEL, DeliveryLimitError), ("bad_args", TaskArgumentError)],
)
async def test_delivery_limit_and_bad_arguments_are_not_retried(label_error):
    label, error_type = label_error
    env = make_env(retries=3)
    await env.broker.startup()
    runs: list[int] = []

    @env.broker.task(retry_on_error=True)
    async def job() -> None:
        runs.append(1)

    env.wrap()
    kicker = job.kicker()
    handle = (
        await kicker.with_labels(**{label: "5"}).kiq()
        if label == state.DELIVERY_LIMIT_LABEL
        else await kicker.kiq(bogus=1)
    )
    result = await handle.wait_result(timeout=5)
    await asyncio.sleep(0.3)
    assert isinstance(result.error, error_type) and not runs
    await env.broker.shutdown()


async def test_refused_task_is_not_retried(monkeypatch):
    from tenxgraph_api.jobs import middleware as mw

    async def refuse(*args, **kwargs):
        raise PermissionError("no")

    monkeypatch.setattr(mw, "_run_identity", refuse)
    env = make_env(retries=3)
    await env.broker.startup()
    runs: list[int] = []

    @env.broker.task(retry_on_error=True)
    async def job() -> None:
        runs.append(1)

    env.wrap()
    handle = await kiq(job)
    result = await handle.wait_result(timeout=5)
    await asyncio.sleep(0.3)
    assert isinstance(result.error, PermissionError) and not runs
    assert (await state.read_status(env.store, handle.task_id)).status == "failure"
    await env.broker.shutdown()


# L4, L5


async def test_no_result_error_is_not_stuck_queued(env):
    @env.broker.task
    async def job() -> None:
        raise NoResultError

    env.wrap()
    handle = await kiq(job)
    await asyncio.sleep(0.2)
    assert (await state.read_status(env.store, handle.task_id)).status == "success"


async def test_status_never_steps_back(env):
    store = env.store
    await state.update_status(store, "t", "job", "queued", {})
    await state.update_status(store, "t", "job", "running", {})
    await state.update_status(store, "t", "job", "queued", {})  # a late queued
    assert (await state.read_status(store, "t")).status == "running"
    await state.update_status(store, "t", "job", "success", {})
    await state.update_status(store, "t", "job", "running", {})
    assert (await state.read_status(store, "t")).status == "success"


async def test_cancel_only_changes_a_queued_task(env):
    store = env.store
    await state.update_status(store, "t", "job", "running", {})
    await cancel("t", env.broker)
    assert (await state.read_status(store, "t")).status == "running"
    await state.update_status(store, "q", "job", "queued", {})
    await cancel("q", env.broker)
    assert (await state.read_status(store, "q")).status == "cancelled"
    await state.update_status(store, "f", "job", "failure", {})
    await cancel("f", env.broker)
    assert (await state.read_status(store, "f")).status == "failure"


async def test_retry_does_not_write_queued_after_the_next_attempt_started():
    env = make_env(retries=1)
    await env.broker.startup()
    seen: list[str] = []

    @env.broker.task(retry_on_error=True)
    async def flaky() -> str:
        record = await state.read_status(env.store, handle.task_id)
        seen.append(record.status)
        if len(seen) == 1:
            raise RuntimeError("first")
        return "ok"

    env.wrap()
    handle = await kiq(flaky)
    await handle.wait_result(timeout=5)
    await asyncio.sleep(0.2)
    assert seen == ["running", "running"]
    assert (await state.read_status(env.store, handle.task_id)).status == "success"
    await env.broker.shutdown()


# L3, N1


class FailingBackend(StatusResultBackend):
    def __init__(self) -> None:  # no Redis connection: set_result is replaced below
        pass


async def test_unsaved_result_marks_failure_with_an_error(env, monkeypatch):
    async def broken_set(self, task_id, result):
        raise TypeError("not serialisable")

    from taskiq_redis import RedisAsyncResultBackend

    monkeypatch.setattr(RedisAsyncResultBackend, "set_result", broken_set)
    # the store must be reachable the way a worker reaches it
    monkeypatch.setattr(state, "cache_store_for", lambda broker: env.store)
    await state.update_status(env.store, "t1", "job", "running", {})
    with pytest.raises(TypeError):
        await FailingBackend().set_result("t1", None)  # type: ignore[arg-type]
    record = await state.read_status(env.store, "t1")
    assert record.status == "failure"
    assert "could not be saved" in (record.error or "")
    assert "TypeError" in record.error


async def test_success_status_waits_for_the_saved_result(env):
    """The final status comes from ``post_save``: if the save fails it is never written."""

    @env.broker.task
    async def job() -> int:
        return 1

    env.wrap()
    original = env.broker.result_backend.set_result

    async def failing(task_id, result):
        raise RuntimeError("down")

    env.broker.result_backend.set_result = failing
    handle = await kiq(job)
    await asyncio.sleep(0.2)
    record = await state.read_status(env.store, handle.task_id)
    assert record is None or record.status == "running"
    env.broker.result_backend.set_result = original


# L8


async def test_warns_when_broker_is_remote_and_state_is_in_memory(caplog):
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
        warn_memory_state(WorkerConfig.model_validate({}), "memory")
        warn_memory_state(
            WorkerConfig.model_validate({"result_backend": {"type": "memory"}}), "redis"
        )
    texts = [r.message for r in caplog.records]
    assert len(texts) == 2
    assert "cache_store.type" in texts[0] and "cancel" in texts[0]
    assert "result_backend" in texts[1]


async def test_no_warning_for_memory_broker_or_shared_state(caplog):
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
        warn_memory_state(WorkerConfig.model_validate({"broker": {"type": "memory"}}), "memory")
        warn_memory_state(WorkerConfig.model_validate({}), "redis")
    assert not caplog.records


# L9


EVENTS: list[str] = []


def resource():
    EVENTS.append("setup")
    yield "r"
    EVENTS.append("teardown")


async def test_cancelled_task_does_not_resolve_generator_dependencies(env):
    events = EVENTS
    events.clear()

    @env.broker.task
    async def job(res: Annotated[str, TaskiqDepends(resource)]) -> str:
        events.append("body")
        return res

    env.wrap()
    task_id = uuid.uuid4().hex
    await cancel(task_id, env.broker)
    result = await (await job.kicker().with_task_id(task_id).kiq()).wait_result(timeout=5)
    assert isinstance(result.error, TaskCancelled)
    assert events == []

    ok = await (await kiq(job)).wait_result(timeout=5)  # an ordinary run still gets it
    assert ok.return_value == "r" and events == ["setup", "body", "teardown"]
