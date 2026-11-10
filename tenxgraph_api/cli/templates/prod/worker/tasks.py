"""Background tasks for this project.

Run a worker with `10xgraph worker`. Run `10xgraph scheduler` for the scheduled tasks, and run
exactly one scheduler or each schedule fires once per scheduler.

Start a task over HTTP: POST /v1/tasks/summarize_thread with {"kwargs": {"thread_id": "..."}}.
Start it from Python: `await summarize_thread.kiq(thread_id="...")`.
"""

import logging
from typing import Annotated

from injectq.integrations.taskiq import InjectTaskiq
from taskiq import TaskiqDepends
from taskiq.depends.progress_tracker import ProgressTracker, TaskState
from tenxgraph.core.graph import CompiledGraph
from tenxgraph.core.state import Message
from tenxgraph_api.jobs import TaskContext, broker


logger = logging.getLogger(__name__)


@broker.task(task_name="summarize_thread", http=True)
async def summarize_thread(
    thread_id: str,  # set by the caller
    config: dict,  # filled by the server: user_id, user, task_id
    ctx: Annotated[TaskContext, TaskiqDepends()],
    tracker: Annotated[ProgressTracker, TaskiqDepends()],
    graph: Annotated[CompiledGraph, InjectTaskiq(CompiledGraph)],
) -> dict:
    if await ctx.cancelled():  # a running task stops only if it checks
        return {"cancelled": True}
    await tracker.set_progress(TaskState.STARTED, {"step": "summarizing"})
    await graph.ainvoke(
        {"messages": [Message.text_message("Summarize this conversation in two sentences.")]},
        config={**config, "thread_id": thread_id},
    )
    return {"thread_id": thread_id, "task_id": ctx.task_id}


@broker.task(schedule=[{"cron": "0 2 * * *"}])  # every day at 02:00, sent by `10xgraph scheduler`
async def nightly_cleanup() -> None:
    # Put cleanup work here, for example deleting old threads.
    logger.info("nightly cleanup ran")
