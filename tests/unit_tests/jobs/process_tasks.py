"""Module-level tasks for the executor tests (importable in process children)."""

from __future__ import annotations

import os
import signal
import time

from tenxgraph_api.jobs import Fail, RetryAfter, TaskContext, task


@task(name="pt_add", executor="process")
def pt_add(a: int, b: int) -> int:
    return a + b


@task(name="pt_pid", executor="process")
def pt_pid() -> int:
    return os.getpid()


@task(name="pt_boom", executor="process")
def pt_boom(message: str) -> None:
    raise ValueError(message)


@task(name="pt_fail", executor="process")
def pt_fail() -> None:
    raise Fail("bad input", data={"field": "x"})


@task(name="pt_retry_after", executor="process")
def pt_retry_after() -> None:
    raise RetryAfter(7)


@task(name="pt_sleep", executor="process")
def pt_sleep(seconds: float) -> str:
    time.sleep(seconds)
    return "slept"


@task(name="pt_wait_cancel", executor="process")
def pt_wait_cancel(ctx: TaskContext) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ctx.cancelled:
            return "saw cancel"
        time.sleep(0.01)
    return "no cancel"


@task(name="pt_ignore_cancel", executor="process")
def pt_ignore_cancel() -> None:
    time.sleep(30)


@task(name="pt_emit", executor="process")
def pt_emit(ctx: TaskContext) -> str:
    ctx.emit.steps(["a", "b"])
    with ctx.emit.step("a"):
        ctx.emit.progress("half", percent=50)
    ctx.emit.log("note", level="warning", data={"k": 1})
    with ctx.emit.step("b"):
        ctx.emit.eta(3)
    return ctx.run_id


@task(name="pt_alloc", executor="process")
def pt_alloc(mb: int, hold: float) -> int:
    block = bytearray(mb * 1024 * 1024)
    for i in range(0, len(block), 4096):  # touch every page so it is resident
        block[i] = 1
    time.sleep(hold)
    return len(block)


@task(name="pt_exit", executor="process")
def pt_exit(code: int) -> None:
    os._exit(code)


@task(name="pt_segv", executor="process")
def pt_segv() -> None:
    os.kill(os.getpid(), signal.SIGSEGV)


@task(name="pt_bad_result", executor="process")
def pt_bad_result() -> int:
    return "not an int"  # type: ignore[return-value]
