"""Scheduler side of the jobs: schedule rules, the checked Redis source, the TaskIQ loop
patches, the scheduler lease and the /v1/tasks schedule routes (S1, S2, S4, L12-L18)."""

# ruff: noqa: S101

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from taskiq import InMemoryBroker, ScheduledTask, ScheduleSource, TaskiqScheduler
from taskiq.cli.scheduler.run import SchedulerLoop
from taskiq.exceptions import ScheduledTaskCancelledError
from taskiq.serializers import JSONSerializer

from tenxgraph_api.jobs import schedule_source as source_module
from tenxgraph_api.jobs import scheduler as scheduler_module
from tenxgraph_api.jobs._taskiq_patches import patch_scheduler_loop
from tenxgraph_api.jobs.config import WorkerConfig, build_schedule_source
from tenxgraph_api.jobs.schedule_rules import HTTP_SCHEDULE_LABEL, check_cron
from tenxgraph_api.jobs.schedule_source import CheckedScheduleSource
from tenxgraph_api.jobs.scheduler import LEASE_KEY, LeasedScheduler

from .test_routes import ADD, Env, routes


patch_scheduler_loop()


class FakeRedis:
    """The few Redis commands the schedule source and the lease use, in memory."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def __aenter__(self) -> FakeRedis:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        return key in self.data

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def getdel(self, key: str) -> bytes | None:
        return self.data.pop(key, None)

    async def rpush(self, key: str, value: str) -> None:
        self.data.setdefault(key, []).append(value.encode())

    async def lrem(self, key: str, count: int, value: str) -> None:
        self.data[key] = [v for v in self.data.get(key, []) if v != value.encode()]

    async def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        return list(self.data.get(key, []))

    async def mget(self, keys: Any) -> list[bytes | None]:
        return [self.data.get(key) for key in keys]

    async def scan_iter(self, pattern: str) -> Any:
        prefix = pattern.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key.encode()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr(source_module, "Redis", lambda connection_pool: redis)
    monkeypatch.setattr("taskiq_redis.list_schedule_source.Redis", lambda connection_pool: redis)
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda url, **kwargs: redis)
    return redis


def _source() -> CheckedScheduleSource:
    return CheckedScheduleSource("redis://localhost:1/0", serializer=JSONSerializer())


def _task(schedule_id: str = "s1", **when: Any) -> ScheduledTask:
    return ScheduledTask(
        task_name=ADD, labels={}, args=[], kwargs={}, schedule_id=schedule_id, **when
    )


# S1: cron and every rules


@pytest.mark.parametrize(
    "cron",
    [
        "61 * * * *",
        "0 abc * * *",
        "5 */0 * * *",
        "* * * *",
        "* * * * * *",
        "5/10 * * * *",
        "30-10 * * * *",
        "* * * * 7",
        "* * * jan *",
        "1,,2 * * * *",
        "* * 0 * *",
        "mon-fri/2 * * * *",
    ],
)
def test_bad_cron_is_refused(cron):
    with pytest.raises(ValueError):
        check_cron(cron)


@pytest.mark.parametrize(
    ("cron", "normalised"),
    [
        ("*/5 * * * *", "*/5 * * * *"),
        ("0  9 * *  mon-fri", "0 9 * * mon-fri"),
        ("0 0 1,15 * *", "0 0 1,15 * *"),
        ("0-30/10 * * 1-12 0-6", "0-30/10 * * 1-12 0-6"),
    ],
)
def test_good_cron_is_normalised(cron, normalised):
    assert check_cron(cron) == normalised


@pytest.mark.parametrize(
    "schedule",
    [
        {"cron": "61 * * * *"},
        {"cron": "0 abc * * *"},
        {"cron": "5 */0 * * *"},
        {"every": 10**15},
        {"every": 1},
    ],
)
def test_route_refuses_bad_schedule(monkeypatch, schedule):
    env = Env(monkeypatch)
    resp = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [1, 2], "schedule": schedule})
    assert resp.status_code == 422
    assert env.source.schedules == {}


def test_at_is_stored_in_utc(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"at": "2030-01-01T02:00:00+02:00"}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    added = env.source.schedules[schedule_id].time
    assert added == datetime(2030, 1, 1, tzinfo=UTC)
    assert added.utcoffset() == timedelta(0)


def test_record_saved_before_strict_rules_still_reads(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"every": 30}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    key = routes._schedule_key(schedule_id)
    raw = asyncio.run(env.store.get(key)).replace(b'"every":30', b'"every":3')
    asyncio.run(env.store.set(key, raw, None))
    assert env.call("GET", f"/v1/tasks/{schedule_id}").json()["data"]["schedule"]["every"] == 3
    assert env.call("POST", f"/v1/tasks/{schedule_id}/cancel").status_code == 200


@pytest.mark.asyncio
async def test_loop_skips_a_schedule_that_raises(caplog):
    loop = SchedulerLoop(TaskiqScheduler(InMemoryBroker(), []))
    now = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    bad_cron = _task("cron-bad", cron="5 */0 * * *")  # pycron: integer modulo by zero
    huge = _task("every-huge", interval=10**15)  # timedelta overflow
    loop.interval_tasks_last_run["every-huge"] = now
    with caplog.at_level(logging.ERROR, logger="tenxgraph_api.jobs"):
        assert loop._is_schedule_ready_to_send(bad_cron, now) is False
        assert loop._is_schedule_ready_to_send(huge, now) is False
        assert loop._is_schedule_ready_to_send(bad_cron, now) is False  # logged once
    assert [r.getMessage().split(":")[0] for r in caplog.records] == [
        "Skipping schedule cron-bad (task tests.add)",
        "Skipping schedule every-huge (task tests.add)",
    ]
    assert loop._is_schedule_ready_to_send(_task("ok", cron="* * * * *"), now) is True


@pytest.mark.asyncio
async def test_source_refuses_bad_cron_from_code(fake_redis):
    with pytest.raises(ValueError):
        await _source().add_schedule(_task(cron="61 * * * *"))
    with pytest.raises(ValueError):
        await _source().add_schedule(_task(interval=10**15))
    assert fake_redis.data == {}


# S4: no run at scheduler start


class _Recorder(TaskiqScheduler):
    def __init__(self, tasks: list[ScheduledTask]) -> None:
        source = _ListSource(tasks)
        super().__init__(InMemoryBroker(), [source])
        self.sent: list[str] = []

    async def on_ready(self, source: Any, task: ScheduledTask) -> None:
        self.sent.append(task.schedule_id)


class _ListSource(ScheduleSource):
    def __init__(self, tasks: list[ScheduledTask]) -> None:
        self.tasks = tasks

    async def get_schedules(self) -> list[ScheduledTask]:
        return self.tasks


@pytest.mark.asyncio
async def test_skip_first_run_holds_interval_and_cron_at_start():
    scheduler = _Recorder([_task("every", interval=3600), _task("cron", cron="* * * * *")])
    loop = SchedulerLoop(scheduler)
    run = loop.run(skip_first_run=True, update_interval=timedelta(seconds=60))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run, 1.5)
    # A cron could still fire if the minute turned during the run: only "every" is certain.
    assert "every" not in scheduler.sent
    assert loop.interval_tasks_last_run["every"] is not None
    assert loop.cron_tasks_last_run["cron"] is not None


# S2: a stale copy is not sent and does not delete the new schedule


@pytest.mark.asyncio
async def test_patch_just_before_old_time_keeps_new_schedule(fake_redis):
    source = _source()
    old_time = datetime.now(UTC) + timedelta(seconds=5)
    await source.add_schedule(_task("s1", time=old_time))
    stale = await source.stored("s1")  # what the scheduler's refresh holds in memory

    await source.delete_schedule("s1")  # PATCH: delete and add with the same id
    await source.add_schedule(_task("s1", time=old_time + timedelta(minutes=5)))

    with pytest.raises(ScheduledTaskCancelledError):
        await source.pre_send(stale)
    await source.post_send(stale)
    current = await source.stored("s1")
    assert current is not None
    assert current.time == old_time + timedelta(minutes=5)

    await source.pre_send(current)  # the new copy is sent and then removed
    await source.post_send(current)
    assert await source.stored("s1") is None


@pytest.mark.asyncio
async def test_deleted_schedule_is_not_sent_from_stale_copy(fake_redis):
    source = _source()
    await source.add_schedule(_task("s1", cron="* * * * *"))
    stale = (await source.get_schedules())[0]
    await source.delete_schedule("s1")
    with pytest.raises(ScheduledTaskCancelledError):
        await source.pre_send(stale)


# L17: one scheduler at a time


@pytest.mark.asyncio
async def test_second_scheduler_waits_as_standby(fake_redis, caplog):
    sent: list[str] = []

    async def record(self, source, task):
        sent.append(task.schedule_id)

    first = LeasedScheduler(InMemoryBroker(), [], "redis://x")
    second = LeasedScheduler(InMemoryBroker(), [], "redis://x")
    for scheduler in (first, second):
        scheduler.broker.find_task = lambda name: None
    task = _task("s1", cron="* * * * *")
    orig = TaskiqScheduler.on_ready
    TaskiqScheduler.on_ready = record  # type: ignore[method-assign]
    try:
        await first.startup()
        with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
            await second.startup()
        assert first.leader
        assert not second.leader
        assert "Another scheduler is running" in caplog.text
        await first.on_ready(None, task)
        await second.on_ready(None, task)
        assert sent == ["s1"]

        await first.shutdown()  # releases the lease
        assert LEASE_KEY not in fake_redis.data
        await second.hold_lease()
        assert second.leader
        await second.on_ready(None, task)
        assert sent == ["s1", "s1"]
        await second.shutdown()
    finally:
        TaskiqScheduler.on_ready = orig  # type: ignore[method-assign]


def test_lease_redis_and_source_have_timeouts():
    source = build_schedule_source(
        WorkerConfig(broker={"type": "redis_stream", "url": "redis://localhost:1/0"})
    )
    pool = source._connection_pool
    assert pool.timeout == 5
    assert pool.connection_kwargs["socket_timeout"] == 5
    assert pool.connection_kwargs["socket_connect_timeout"] == 5


# L13: a task that lost http=True


@pytest.mark.asyncio
async def test_http_schedule_of_non_http_task_is_not_sent(caplog):
    broker = InMemoryBroker()

    @broker.task(task_name="tasks.private")
    async def private() -> None:
        return None

    scheduler = LeasedScheduler(broker, [], None)
    await scheduler.startup()
    task = ScheduledTask(
        task_name="tasks.private",
        labels={HTTP_SCHEDULE_LABEL: "1"},
        args=[],
        kwargs={},
        schedule_id="s1",
        cron="* * * * *",
    )
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
        await scheduler.on_ready(None, task)
    assert "no longer has http=True" in caplog.text
    await scheduler.shutdown()


def test_patch_of_task_that_lost_http_is_404(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"every": 30}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    added = env.source.schedules[schedule_id]
    assert added.labels[HTTP_SCHEDULE_LABEL] == "1"
    env.broker.find_task(ADD).labels["http"] = False
    resp = env.call("PATCH", f"/v1/tasks/{schedule_id}", json={"schedule": {"every": 60}})
    assert resp.status_code == 404
    assert env.source.schedules[schedule_id].interval == 30


# L12: limits per owner


def test_schedules_per_owner_are_capped(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.setattr(routes, "MAX_SCHEDULES_PER_OWNER", 2)
    body = {"args": [1, 2], "schedule": {"every": 30}}
    first = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    assert env.call("POST", f"/v1/tasks/{ADD}", json=body).status_code == 202
    resp = env.call("POST", f"/v1/tasks/{ADD}", json=body)
    assert resp.status_code == 429
    assert len(env.source.schedules) == 2
    assert env.call("POST", f"/v1/tasks/{ADD}", user="u2", json=body).status_code == 202
    # PATCH keeps the same slot; cancel frees it.
    patch = {"schedule": {"cron": "0 * * * *"}}
    assert env.call("PATCH", f"/v1/tasks/{first}", json=patch).status_code == 200
    assert env.call("POST", f"/v1/tasks/{first}/cancel").status_code == 200
    assert env.call("POST", f"/v1/tasks/{ADD}", json=body).status_code == 202


# L14: PATCH and cancel of one schedule never interleave


def test_patch_and_cancel_wait_for_the_schedule_lock(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"every": 30}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    held = asyncio.run(env.store.acquire(f"jobs:schedule-lock:{schedule_id}", 1, 30))
    patch = {"schedule": {"every": 60}}
    assert env.call("PATCH", f"/v1/tasks/{schedule_id}", json=patch).status_code == 409
    assert env.call("POST", f"/v1/tasks/{schedule_id}/cancel").status_code == 409
    assert env.source.schedules[schedule_id].interval == 30
    asyncio.run(env.store.release(f"jobs:schedule-lock:{schedule_id}", held.slot_id))
    assert env.call("POST", f"/v1/tasks/{schedule_id}/cancel").status_code == 200
    assert env.source.schedules == {}


# L15: a fired one-time schedule


def test_sent_one_time_schedule_reads_sent_and_cannot_be_patched(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"at": "2030-01-01T00:00:00Z"}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    assert env.call("GET", f"/v1/tasks/{schedule_id}").json()["data"]["status"] == "scheduled"

    env.source.schedules.pop(schedule_id)  # the scheduler sent it (post_send deleted it)
    assert env.call("GET", f"/v1/tasks/{schedule_id}").json()["data"]["status"] == "sent"
    patch = {"schedule": {"at": "2031-01-01T00:00:00Z"}}
    resp = env.call("PATCH", f"/v1/tasks/{schedule_id}", json=patch)
    assert resp.status_code == 409
    assert env.source.schedules == {}


def test_scheduler_module_builds_leased_scheduler():
    assert isinstance(scheduler_module.scheduler, LeasedScheduler)
