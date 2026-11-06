"""Module-level process tasks for the worker tests (importable in process children)."""

from __future__ import annotations

import time

from tenxgraph_api.jobs import TaskContext, task


@task(name="wp_add", executor="process")
def wp_add(a: int, b: int) -> int:
    return a + b


@task(name="wp_slow_first", executor="process", timeout=1.0, retries=1)
def wp_slow_first(ctx: TaskContext) -> str:
    if ctx.attempt == 1:
        time.sleep(30)
    return "second attempt"
