"""TaskMiddleware, the cancel guard, wrap_tasks and MonitoringMiddleware on InMemoryBroker.

String annotations on purpose (``from __future__``): the guard must keep TaskIQ's signature,
type-hint and dependency reading working through ``functools.wraps``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated

import pytest
import pytest_asyncio
from injectq.integrations.taskiq import InjectTaskiq
from taskiq import SimpleRetryMiddleware, TaskiqDepends
from tenxgraph.runtime.publisher.base_publisher import BasePublisher
from tenxgraph.utils.background_task_manager import BackgroundTaskManager

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.arguments import ReservedArgumentError
from tenxgraph_api.jobs.context import TaskCancelled, TaskContext, cancel
from tenxgraph_api.jobs.middleware import MonitoringMiddleware, TaskMiddleware
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore

from .conftest import Env, Service


pytestmark = pytest.mark.asyncio
RAN: list[str] = []


def add_tasks(env: Env):
    @env.broker.task
    async def job(a: int, config: dict, graph: Annotated[Service, InjectTaskiq(Service)]) -> list:
        RAN.append("job")
        return [a, config, graph is env.service]

    @env.broker.task
    def sync_job(a: int) -> int:
        RAN.append("sync_job")
        return a

    env.wrap()
    return job, sync_job


async def kiq_with_id(task, *args, **kwargs):
    task_id = uuid.uuid4().hex
    return await task.kicker().with_task_id(task_id).kiq(*args, **kwargs)


@pytest.fixture(autouse=True)
def _reset():
    RAN.clear()


async def test_cancel_before_start_gives_error_result_and_status(env):
    job, sync_job = add_tasks(env)
    for task in (job, sync_job):
        task_id = uuid.uuid4().hex
        await state.write_status(
            env.store, state.TaskStatus(task_id=task_id, task_name=task.task_name, status="queued")
        )
        await cancel(task_id, env.broker)
        handle = await task.kicker().with_task_id(task_id).kiq(1)
        result = await handle.wait_result(timeout=5)
        assert result.is_err
        assert isinstance(result.error, TaskCancelled)
        assert (await state.read_status(env.store, task_id)).status == "cancelled"
    assert RAN == []
    # The next run is not affected by the cancelled one.
    assert (await (await kiq_with_id(sync_job, 5)).wait_result(timeout=5)).return_value == 5


async def test_worker_recheck_blocks_injected_override(env):
    job, _ = add_tasks(env)
    handle = await kiq_with_id(job, 1, graph="EVIL")  # plain kiq, no caller-side check
    result = await handle.wait_result(timeout=5)
    assert isinstance(result.error, ReservedArgumentError)
    assert RAN == []
    assert (await state.read_status(env.store, handle.task_id)).status == "failure"


async def test_config_comes_only_from_the_label(env):
    job, sync_job = add_tasks(env)
    kicker = job.kicker().with_labels(**{state.CONFIG_LABEL: '{"user_id": "u1"}'})
    handle = await kicker.kiq(2, config={"user_id": "EVIL"})
    result = await handle.wait_result(timeout=5)
    assert result.return_value == [2, {"user_id": "u1", "task_id": handle.task_id}, True]
    record = await state.read_status(env.store, handle.task_id)
    assert (record.status, record.owner) == ("success", "u1")
    # A task without ``config`` never gets one; a broken label means ``{}``.
    kicker = sync_job.kicker().with_labels(**{state.CONFIG_LABEL: "not json"})
    assert (await (await kicker.kiq(3, config={})).wait_result(timeout=5)).return_value == 3


async def test_retry_resends_config_without_tripping_the_check():
    env = Env(InMemoryCacheStore(), SimpleRetryMiddleware(default_retry_count=3), TaskMiddleware())
    await env.broker.startup()
    calls: list[dict] = []

    @env.broker.task(retry_on_error=True)
    async def flaky(config: dict) -> str:
        calls.append(config)
        if len(calls) == 1:
            raise RuntimeError("first try fails")
        return "ok"

    env.wrap()
    handle = await flaky.kicker().with_labels(**{state.CONFIG_LABEL: "{}"}).kiq()
    result = await handle.wait_result(timeout=5)
    assert result.return_value == "ok"
    assert len(calls) == 2
    assert (await state.read_status(env.store, handle.task_id)).status == "success"
    await env.broker.shutdown()


class BrokenStore(InMemoryCacheStore):
    async def get(self, key: str) -> bytes | None:
        raise ConnectionError("store down")

    async def set(self, key, value, ttl, mode="always") -> bool:
        raise ConnectionError("store down")


async def test_middleware_never_raises_when_the_store_fails(caplog):
    env = Env(BrokenStore())
    await env.broker.startup()
    job, _ = add_tasks(env)
    with caplog.at_level(logging.ERROR, logger="tenxgraph_api.jobs"):
        kicker = job.kicker().with_labels(**{state.CONFIG_LABEL: '{"user_id": "u1"}'})
        result = await (await kicker.kiq(1)).wait_result(timeout=5)
    assert not result.is_err, result.error
    assert result.return_value[1]["user_id"] == "u1"  # config still passed
    assert "pre_execute failed" in caplog.text
    assert "status not saved" in caplog.text
    await env.broker.shutdown()


async def test_wrap_tasks_is_idempotent_and_keeps_injection(env):
    job, _ = add_tasks(env)
    wrapped = job.original_func
    env.wrap()
    assert job.original_func is wrapped
    assert not hasattr(wrapped.__wrapped__, "__tenxgraph_guard__")  # wrapped once

    @env.broker.task
    async def with_ctx(ctx: Annotated[TaskContext, TaskiqDepends()]) -> str:
        return ctx.task_id

    env.wrap()
    handle = await kiq_with_id(with_ctx)
    assert (await handle.wait_result(timeout=5)).return_value == handle.task_id


class FakePublisher(BasePublisher):
    def __init__(self, fail: bool = False) -> None:
        super().__init__({})
        self.events: list = []
        self.fail = fail

    async def publish(self, event):
        if self.fail:
            raise RuntimeError("sink down")
        self.events.append(event)

    async def close(self): ...

    def sync_close(self): ...


@pytest_asyncio.fixture
async def monitored():
    env = Env(InMemoryCacheStore(), MonitoringMiddleware(), TaskMiddleware())
    manager = BackgroundTaskManager()
    env.container.bind_instance(BackgroundTaskManager, manager)
    await env.broker.startup()
    yield env, manager
    await env.broker.shutdown()


async def test_monitoring_logs_and_publishes(monitored, caplog):
    env, manager = monitored
    publisher = FakePublisher()
    env.container.bind_instance(BasePublisher, publisher)
    _, sync_job = add_tasks(env)

    @env.broker.task
    async def boom() -> None:
        raise ValueError("bad " + "x" * 600)

    @env.broker.task
    async def stopped() -> None:
        raise TaskCancelled("stop")

    env.wrap()

    async def run(task, *args) -> list[tuple[str, str]]:
        publisher.events.clear()
        handle = await kiq_with_id(task, *args)
        await handle.wait_result(timeout=5)
        await manager.wait_for_all(timeout=5)
        events = [e for e in publisher.events if e.run_id == handle.task_id]
        return [(e.metadata["phase"], str(e.event_type)) for e in events]

    with caplog.at_level(logging.DEBUG, logger="tenxgraph_api.jobs"):
        ok = await run(sync_job, 1)
        ok_events = list(publisher.events)
        caplog.clear()
        failed = await run(boom)
        failed_events = list(publisher.events)
        failed_records = list(caplog.records)
        caplog.clear()
        cancelled = await run(stopped)
        cancelled_records = list(caplog.records)
        cancelled_events = list(publisher.events)

    assert ok == [
        ("queued", "update"),
        ("started", "start"),
        ("finished", "end"),
        ("saved", "result"),
    ]
    assert ok_events[2].data["duration"] >= 0
    assert "return_value" not in ok_events[3].data
    assert failed == [
        ("queued", "update"),
        ("started", "start"),
        ("failed", "error"),
        ("saved", "result"),
    ]
    error = failed_events[2]
    assert error.is_error and error.data["error"] == "ValueError"
    assert error.data["message"].startswith("bad x") and len(error.data["message"]) == 500
    assert error.data["attempt"] == 1 and "duration" in error.data
    logged = [
        r for r in failed_records if r.levelno == logging.ERROR and r.name == "tenxgraph_api.jobs"
    ]
    assert len(logged) == 1 and "failed on attempt 1" in logged[0].getMessage()
    assert logged[0].exc_info and logged[0].exc_info[0] is ValueError  # traceback is logged
    assert cancelled == [
        ("queued", "update"),
        ("started", "start"),
        ("cancelled", "interrupted"),
        ("saved", "result"),
    ]
    assert not cancelled_events[2].is_error
    assert not [
        r
        for r in cancelled_records
        if r.levelno >= logging.WARNING and r.name == "tenxgraph_api.jobs"
    ]
    assert any("cancelled" in r.getMessage() for r in cancelled_records)


async def test_monitoring_counts_retries():
    env = Env(
        InMemoryCacheStore(), SimpleRetryMiddleware(default_retry_count=3), MonitoringMiddleware()
    )
    manager = BackgroundTaskManager()
    publisher = FakePublisher()
    env.container.bind_instance(BackgroundTaskManager, manager)
    env.container.bind_instance(BasePublisher, publisher)
    await env.broker.startup()

    @env.broker.task(retry_on_error=True)
    async def flaky() -> str:
        raise RuntimeError("again")

    handle = await kiq_with_id(flaky)
    await handle.wait_result(timeout=5)
    await asyncio.sleep(0.3)  # retries are re-sent in the background
    await manager.wait_for_all(timeout=5)
    attempts = [e.data["attempt"] for e in publisher.events if e.metadata["phase"] == "failed"]
    assert attempts == [1, 2, 3]
    await env.broker.shutdown()


async def test_monitoring_never_raises_on_a_failing_publisher(monitored):
    env, _ = monitored
    env.container.bind_instance(BasePublisher, FakePublisher(fail=True))
    _, sync_job = add_tasks(env)
    assert (await (await kiq_with_id(sync_job, 9)).wait_result(timeout=5)).return_value == 9


class FakeMeter:
    """Records what core's metrics facade sends to OTEL."""

    def __init__(self):
        self.calls: list[tuple[str, str, float, dict]] = []

    def _instrument(self, kind: str, name: str):
        meter = self

        class Instrument:
            def add(self, value, attributes):
                meter.calls.append((kind, name, value, attributes))

            record = add

        return Instrument()

    def create_counter(self, name, **_):
        return self._instrument("counter", name)

    def create_histogram(self, name, **_):
        return self._instrument("histogram", name)


@pytest.fixture
def meter():
    from tenxgraph.utils import metrics

    fake = FakeMeter()
    metrics.setup_otel_metrics(fake)
    yield fake
    metrics._OTEL_METER = None


async def test_monitoring_records_run_counters_and_duration(monitored, meter):
    env, manager = monitored
    _, sync_job = add_tasks(env)

    @env.broker.task
    async def boom() -> None:
        raise ValueError("bad")

    @env.broker.task
    async def stopped() -> None:
        raise TaskCancelled("stop")

    env.wrap()
    for task in (sync_job, boom, stopped):
        args = (1,) if task is sync_job else ()
        await (await kiq_with_id(task, *args)).wait_result(timeout=5)
    await manager.wait_for_all(timeout=5)

    runs = {
        (a["task"].rsplit(":", 1)[-1], a["status"])
        for kind, name, _, a in meter.calls
        if name == "tenxgraph.task.runs"
    }
    assert {t for t, _ in runs} == {"sync_job", "boom", "stopped"}
    assert runs == {
        ("sync_job", "queued"),
        ("sync_job", "success"),
        ("boom", "queued"),
        ("boom", "failure"),
        ("stopped", "queued"),
        ("stopped", "cancelled"),
    }
    durations = [c for c in meter.calls if c[1] == "tenxgraph.task.duration_ms"]
    assert len(durations) == 3
    assert all(c[0] == "histogram" and c[2] >= 0 and set(c[3]) == {"task"} for c in durations)


async def test_monitoring_never_raises_when_metrics_fail(monitored, monkeypatch):
    env, _ = monitored
    _, sync_job = add_tasks(env)

    def broken(*_args, **_kwargs):
        raise RuntimeError("metrics down")

    monkeypatch.setattr("tenxgraph.utils.metrics.counter", broken)
    monkeypatch.setattr("tenxgraph.utils.metrics.timer", broken)
    assert (await (await kiq_with_id(sync_job, 4)).wait_result(timeout=5)).return_value == 4
