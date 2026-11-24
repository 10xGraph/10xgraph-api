"""The built-in task behind the ``memory_cleanup`` key: delete expired memories on a schedule.

Imported by ``tenxgraph_api.jobs.worker`` only when ``memory_cleanup.enabled`` is true, so the
worker can run it and ``10xgraph scheduler`` (a ``schedule`` label, every ``interval_seconds``)
sends it. Run one scheduler, as for any scheduled task.
"""

import logging
from typing import Annotated

from injectq.integrations.taskiq import InjectTaskiq
from tenxgraph.storage.store import BaseStore

from tenxgraph_api.jobs.worker import broker, graph_config


logger = logging.getLogger("tenxgraph_api.jobs")

TASK_NAME = "tenxgraph_api.memory_cleanup"

if graph_config is None:
    raise RuntimeError("memory_cleanup needs a 10xgraph.json")
_config = graph_config.memory_cleanup


@broker.task(
    task_name=TASK_NAME,
    schedule=[{"interval": _config.interval_seconds}],
)
async def delete_expired_memories(
    store: Annotated[BaseStore, InjectTaskiq(BaseStore)],
) -> int:
    """Remove every expired memory, ``batch_size`` rows per statement; return how many.

    The store loops its own batches until one comes back short, so one call clears the backlog.
    """
    deleted = await store.adelete_expired(batch_size=_config.batch_size)
    logger.info("memory_cleanup deleted %d expired memories", deleted)
    return deleted
