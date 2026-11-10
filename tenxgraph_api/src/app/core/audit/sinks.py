"""Built-in audit sinks and the sink list built from ``audit.sinks`` at boot."""

import importlib
import inspect
import logging
import sys
from typing import Any

from injectq import InjectQ
from tenxgraph.runtime.publisher.base_publisher import BasePublisher
from tenxgraph.runtime.publisher.events import ContentType, Event, EventModel, EventType
from tenxgraph.runtime.publisher.publish import _select, publish_event
from tenxgraph.utils.background_task_manager import BackgroundTaskManager

from tenxgraph_api.src.app.core.audit import AuditEvent, BaseAuditSink


AUDIT_LOGGER_NAME = "tenxgraph_api.audit"

logger = logging.getLogger("tenxgraph_api")


def _audit_logger() -> logging.Logger:
    """``tenxgraph_api.audit``: raw JSON lines on stdout, whatever ``LOG_FORMAT`` and ``LOG_LEVEL``.

    It has its own handler and does not propagate, so the app's (sanitizing, possibly text)
    console handler never writes a second, reformatted copy. Events are metadata only.
    """
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    if not audit_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit_logger.addHandler(handler)
    return audit_logger


class LogAuditSink(BaseAuditSink):
    """One JSON line per event on stdout (logger ``tenxgraph_api.audit``)."""

    def __init__(self) -> None:
        self._logger = _audit_logger()

    async def emit(self, event: AuditEvent) -> None:
        self._logger.info(event.model_dump_json())


class PublisherAuditSink(BaseAuditSink):
    """Also sends each event through the app's publisher (Kafka, Redis, RabbitMQ, custom).

    Best effort: the event is queued as a background task and ``emit`` returns at once, so a
    slow or down broker never delays a request. The sink has its own task manager (and so its
    own queue cap), so a slow broker under audit volume sheds audit events only, never graph
    events or memory-tool writes. Events still in flight at SIGTERM, or shed when the queue is
    full, are lost; the ``log`` sink is the record. Observability publishers (OTEL, Logfire,
    Phoenix) are skipped, so audit events never reach an AI tracing backend.
    """

    def __init__(self, publisher: BasePublisher) -> None:
        self._publisher = publisher
        self._tasks = BackgroundTaskManager()

    async def emit(self, event: AuditEvent) -> None:
        publish_event(
            EventModel(
                event=Event.GRAPH_EXECUTION,
                event_type=EventType.UPDATE,
                data=event.model_dump(),
                content_type=[ContentType.DATA],
                node_name="audit",
                user_id=event.user_id,
                metadata={"audit": True},
            ),
            publisher=self._publisher,
            task_manager=self._tasks,
            only="events",
        )


def _build_publisher_sink() -> BaseAuditSink | None:
    publisher = InjectQ.get_instance().try_get(BasePublisher)
    if publisher is None or _select(publisher, "events") is None:
        logger.warning(
            'audit.sinks lists "publisher" but no events publisher is configured; skipped'
        )
        return None
    return PublisherAuditSink(publisher)


def _load_custom_sink(path: str) -> BaseAuditSink:
    """Import ``module:attr`` (an instance, a ``BaseAuditSink`` subclass or a zero-argument
    factory) and return the sink. Anything else raises, which stops the boot."""
    module_name, _, attr = path.partition(":")
    try:
        target: Any = importlib.import_module(module_name)
        for part in attr.split("."):
            target = getattr(target, part)
        if isinstance(target, BaseAuditSink):
            return target
        if inspect.isclass(target) and not issubclass(target, BaseAuditSink):
            raise TypeError("class is not a BaseAuditSink subclass")
        sink = target() if callable(target) else target
    except Exception as exc:
        raise ValueError(f"audit.sinks {path!r} cannot be loaded: {exc}") from exc
    if not isinstance(sink, BaseAuditSink):
        raise ValueError(f"audit.sinks {path!r} did not produce a BaseAuditSink instance")
    return sink


def build_sinks(names: list[str]) -> list[BaseAuditSink]:
    """The sinks named in ``audit.sinks``: ``"log"``, ``"publisher"`` or ``"module:Class"``."""
    sinks: list[BaseAuditSink] = []
    for name in names:
        if name == "log":
            sinks.append(LogAuditSink())
        elif name == "publisher":
            if (sink := _build_publisher_sink()) is not None:
                sinks.append(sink)
        else:
            sinks.append(_load_custom_sink(name))
    return sinks
