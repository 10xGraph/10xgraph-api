"""An in-memory broker with our middlewares, a fresh InjectQ container and cache store."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from injectq import InjectQ
from injectq.integrations.taskiq import setup_taskiq
from taskiq import InMemoryBroker, ScheduledTask, ScheduleSource

from tenxgraph_api.jobs.middleware import TaskMiddleware, wrap_tasks
from tenxgraph_api.jobs.schedules import set_schedule_source
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore


class Service:
    """Something InjectQ injects into tasks."""

    name = "bound"


class FakeScheduleSource(ScheduleSource):
    def __init__(self) -> None:
        self.items: dict[str, ScheduledTask] = {}

    async def get_schedules(self) -> list[ScheduledTask]:
        return list(self.items.values())

    async def add_schedule(self, schedule: ScheduledTask) -> None:
        self.items[schedule.schedule_id] = schedule

    async def delete_schedule(self, schedule_id: str) -> None:
        self.items.pop(schedule_id, None)


class Env:
    def __init__(self, store: BaseCacheStore, *middlewares: Any) -> None:
        self.store = store
        self.service = Service()
        self.container = InjectQ()
        self.container.bind_instance(BaseCacheStore, store)
        self.container.bind_instance(Service, self.service)
        self.broker = InMemoryBroker().with_middlewares(*(middlewares or (TaskMiddleware(),)))
        setup_taskiq(self.container, self.broker)

    def wrap(self) -> None:
        wrap_tasks(self.broker)


@pytest_asyncio.fixture
async def env():
    environment = Env(InMemoryCacheStore())
    await environment.broker.startup()
    yield environment
    await environment.broker.shutdown()


@pytest.fixture
def source():
    fake = FakeScheduleSource()
    set_schedule_source(fake)
    yield fake
    set_schedule_source(None)
