"""Shared fixtures for the jobs tests."""

from __future__ import annotations

import pytest_asyncio

from tenxgraph_api.jobs.core.hooks import TaskLifecycleHook
from tenxgraph_api.jobs.testing import jobs_harness


class RecordingHook(TaskLifecycleHook):
    """Records every hook call as (name, run_id, extra)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def on_task_queued(self, run):
        self.calls.append(("queued", run.run_id))

    async def on_task_start(self, run):
        self.calls.append(("start", run.run_id, run.attempt))

    async def on_task_progress(self, run, event):
        self.calls.append(("progress", run.run_id, event["type"]))

    async def on_task_retry(self, run, error):
        self.calls.append(("retry", run.run_id, error["code"]))

    async def on_task_success(self, run, result):
        self.calls.append(("success", run.run_id, result))

    async def on_task_failure(self, run, error):
        self.calls.append(("failure", run.run_id, error["code"]))

    async def on_task_cancel(self, run):
        self.calls.append(("cancel", run.run_id))

    async def on_task_lost(self, run):
        self.calls.append(("lost", run.run_id, run.state.value))

    def names(self, run_id: str | None = None) -> list[str]:
        return [c[0] for c in self.calls if run_id is None or c[1] == run_id]


@pytest_asyncio.fixture
async def hook():
    return RecordingHook()


@pytest_asyncio.fixture
async def jobs(hook):
    async with jobs_harness(hooks=[hook]) as harness:
        yield harness
