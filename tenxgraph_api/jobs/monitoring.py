"""``MonitoringMiddleware``: log task started / finished / failed, and send the same as core
publisher events when a ``BasePublisher`` is bound in the container. Never raises."""

from __future__ import annotations

import logging
from typing import Any

from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from tenxgraph_api.jobs import state


logger = logging.getLogger("tenxgraph_api.jobs")


class MonitoringMiddleware(TaskiqMiddleware):
    """Task lifecycle logs and events. Core has no task event kind yet, so events use
    ``Event.GRAPH_EXECUTION`` with ``metadata["source"] == "task"``."""

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        logger.info("Task %s (%s) started", message.task_name, message.task_id)
        self._publish(message, "start", {})
        return message

    async def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        if result.is_err:
            logger.warning(
                "Task %s (%s) failed after %.2fs: %s",
                message.task_name,
                message.task_id,
                result.execution_time,
                type(result.error).__name__,
            )
            data = {"error": type(result.error).__name__}
            self._publish(message, "error", {**data, "duration": result.execution_time})
        else:
            logger.info(
                "Task %s (%s) finished in %.2fs",
                message.task_name,
                message.task_id,
                result.execution_time,
            )
            self._publish(message, "end", {"duration": result.execution_time})

    def _publish(self, message: TaskiqMessage, phase: str, data: dict[str, Any]) -> None:
        try:
            from tenxgraph.runtime.publisher.base_publisher import BasePublisher
            from tenxgraph.runtime.publisher.events import Event, EventModel, EventType
            from tenxgraph.runtime.publisher.publish import publish_event
            from tenxgraph.utils.background_task_manager import BackgroundTaskManager

            container = state.container_for(self.broker)
            publisher = container.try_get(BasePublisher)
            manager = container.try_get(BackgroundTaskManager)
            if publisher is None or manager is None:
                return
            event = EventModel(
                event=Event.GRAPH_EXECUTION,
                event_type=EventType(phase),
                node_name=message.task_name,
                run_id=message.task_id,
                user_id=state.parse_config(message.labels).get("user_id"),
                is_error=phase == "error",
                data=data,
                metadata={"source": "task", "task_name": message.task_name},
            )
            publish_event(event, publisher=publisher, task_manager=manager)
        except Exception:
            logger.debug("Task event not published", exc_info=True)
