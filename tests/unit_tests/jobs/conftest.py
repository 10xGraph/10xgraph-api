"""An in-memory broker with our middlewares, a fresh InjectQ container and cache store."""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from injectq import InjectQ
from injectq.integrations.taskiq import setup_taskiq
from taskiq import InMemoryBroker

from tenxgraph_api.jobs.middleware import TaskMiddleware, wrap_tasks
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore


class Service:
    """Something InjectQ injects into tasks."""

    name = "bound"


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
