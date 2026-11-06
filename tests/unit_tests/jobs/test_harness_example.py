"""The JOBS_PLAN "Testing" example, verbatim except for the task body.

JOBS_PLAN's ``summarize_interview`` runs a graph,
so the task here does the same steps without a graph. The test function is the plan's text.
"""

from __future__ import annotations

import pytest

from tenxgraph_api.jobs import TaskContext, task
from tenxgraph_api.jobs.testing import jobs_harness


async def load(interview_id: str) -> str:
    return f"transcript of {interview_id}"


@task(http=True, retries=3, timeout=600)
async def summarize_interview(interview_id: str, ctx: TaskContext) -> dict:
    ctx.emit.steps(["load", "summarize"])
    with ctx.emit.step("load"):
        transcript = await load(interview_id)
    with ctx.emit.step("summarize"):
        result = {"messages": [transcript.upper()]}
    return {"summary": result["messages"][-1]}


@pytest.mark.asyncio
async def test_summary():
    async with jobs_harness() as jobs:  # memory store, fake clock, in-process worker
        run = await summarize_interview.options(identity=jobs.user("u1")).enqueue(interview_id="42")
        await jobs.drain()  # run everything due
        assert (await run.status()).state == "succeeded"

        jobs.clock.advance(hours=24)  # cron ticks fire
        await jobs.drain()

        # Beyond the plan's example:
        assert await run.result() == {"summary": "TRANSCRIPT OF 42"}
        steps = (await run.status()).progress["steps"]
        assert [(s["name"], s["state"]) for s in steps] == [
            ("load", "succeeded"),
            ("summarize", "succeeded"),
        ]
