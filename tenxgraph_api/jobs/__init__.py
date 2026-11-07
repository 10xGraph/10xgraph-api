"""Background jobs on TaskIQ: ``from tenxgraph_api.jobs import broker`` and write tasks.

Names load on first use, so importing ``tenxgraph_api.jobs.config`` stays light and does not
build the broker.
"""

import importlib
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from tenxgraph_api.jobs.context import (
        TaskContext,
        cancel,
        enqueue,
        reschedule,
        schedule,
        unschedule,
    )
    from tenxgraph_api.jobs.worker import broker

_SOURCES = {
    "broker": "worker",
    "TaskContext": "context",
    "enqueue": "context",
    "schedule": "context",
    "reschedule": "context",
    "unschedule": "context",
    "cancel": "context",
}

__all__ = list(_SOURCES)


def __getattr__(name: str) -> Any:
    if name not in _SOURCES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{_SOURCES[name]}")
    return getattr(module, name)
