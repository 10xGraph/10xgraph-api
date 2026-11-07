import asyncio
from typing import Annotated

from injectq.integrations.taskiq import InjectTaskiq
from taskiq import TaskiqDepends
from taskiq.depends.progress_tracker import ProgressTracker, TaskState
from tenxgraph.core.graph import CompiledGraph
from tenxgraph.core.state import Message
from tenxgraph_api.jobs import TaskContext, broker


@broker.task(task_name="make_report", http=True)  # http=True: POST /v1/tasks/make_report
async def make_report(
    topic: str,  # set by the caller
    config: dict,  # filled by the server: user_id, user, task_id
    ctx: Annotated[TaskContext, TaskiqDepends()],
    tracker: Annotated[ProgressTracker, TaskiqDepends()],
    graph: Annotated[CompiledGraph, InjectTaskiq(CompiledGraph)],
) -> dict:
    for step in range(1, 4):
        if await ctx.cancelled():  # a running task stops only if it checks
            return {"cancelled": True}
        await tracker.set_progress(TaskState.STARTED, {"step": step, "of": 3})
        await asyncio.sleep(1)
    await graph.ainvoke({"messages": [Message.text_message(topic)]}, config=config)
    return {"topic": topic, "task_id": ctx.task_id, "user_id": config.get("user_id")}


@broker.task(schedule=[{"cron": "* * * * *"}])  # every minute, sent by `10xgraph scheduler`
async def heartbeat() -> None:
    print("heartbeat")
