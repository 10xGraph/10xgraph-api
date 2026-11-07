"""``MonitoringMiddleware``: log every TaskIQ hook and send the same as core publisher events
when a ``BasePublisher`` is bound in the container. Never raises."""

from __future__ import annotations

import logging
from typing import Any

from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.context import TaskCancelled


logger = logging.getLogger("tenxgraph_api.jobs")
MESSAGE_LIMIT = 500


class MonitoringMiddleware(TaskiqMiddleware):
    """Task lifecycle logs and events. Core has no task event kind yet, so events use
    ``Event.GRAPH_EXECUTION`` with ``metadata["source"] == "task"`` and the hook name in
    ``metadata["phase"]``.

    | hook | event type | phase |
    |---|---|---|
    | pre_send | none | |
    | post_send | UPDATE | queued |
    | pre_execute | START | started |
    | on_error | ERROR (INTERRUPTED for ``TaskCancelled``) | failed (cancelled) |
    | post_execute | END (success only) | finished |
    | post_save | RESULT | saved |
    """

    async def startup(self) -> None:
        logger.info("Task monitoring started")

    async def shutdown(self) -> None:
        logger.info("Task monitoring stopped")

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        logger.debug("Task %s (%s) sending", message.task_name, message.task_id)
        return message

    def post_send(self, message: TaskiqMessage) -> None:
        logger.info("Task %s (%s) queued", message.task_name, message.task_id)
        self._publish(message, "update", "queued")

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        logger.info("Task %s (%s) started", message.task_name, message.task_id)
        self._publish(message, "start", "started")
        return message

    def on_error(
        self, message: TaskiqMessage, result: TaskiqResult[Any], exception: BaseException
    ) -> None:
        name, task_id, took = message.task_name, message.task_id, result.execution_time
        if isinstance(exception, TaskCancelled):
            logger.info("Task %s (%s) cancelled after %.2fs", name, task_id, took)
            self._publish(message, "interrupted", "cancelled", {"duration": took})
            return
        attempt = int(message.labels.get("_retries", 0)) + 1  # SimpleRetryMiddleware's counter
        logger.error(
            "Task %s (%s) failed on attempt %d after %.2fs: %s: %s",
            name,
            task_id,
            attempt,
            took,
            type(exception).__name__,
            exception,
            exc_info=exception,
        )
        data = {
            "error": type(exception).__name__,
            "message": str(exception)[:MESSAGE_LIMIT],
            "duration": took,
            "attempt": attempt,
        }
        self._publish(message, "error", "failed", data, is_error=True)

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        if result.is_err:  # on_error already reported it
            logger.debug("Task %s (%s) ended with an error", message.task_name, message.task_id)
            return
        logger.info(
            "Task %s (%s) finished in %.2fs",
            message.task_name,
            message.task_id,
            result.execution_time,
        )
        self._publish(message, "end", "finished", {"duration": result.execution_time})

    def post_save(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        logger.debug("Task %s (%s) result saved", message.task_name, message.task_id)
        self._publish(message, "result", "saved")  # no return value: it may be large or private

    def _publish(
        self,
        message: TaskiqMessage,
        event_type: str,
        phase: str,
        data: dict[str, Any] | None = None,
        is_error: bool = False,
    ) -> None:
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
                event_type=EventType(event_type),
                node_name=message.task_name,
                run_id=message.task_id,
                user_id=state.parse_config(message.labels).get("user_id"),
                is_error=is_error,
                data=data or {},
                metadata={"source": "task", "task_name": message.task_name, "phase": phase},
            )
            publish_event(event, publisher=publisher, task_manager=manager)
        except Exception:
            logger.debug("Task event not published", exc_info=True)
