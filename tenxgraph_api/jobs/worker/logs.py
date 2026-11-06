"""One structured log line per run state change.

Every line reads ``task run <what> run_id=... task=... attempt=...`` plus ``trace_id=...`` when
one is known, and carries the same fields as ``extra`` (``jobs_run_id``, ``jobs_task``,
``jobs_attempt``, ``jobs_trace_id``) for JSON log formatters.

The trace id comes from the run's stored ``traceparent`` (``trigger["traceparent"]``, written by
the enqueuer from Sprint 6 on) or, failing that, from the current OpenTelemetry span.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from tenxgraph_api.jobs.core.models import TaskRun


_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def trace_id_of(run: TaskRun | None = None) -> str | None:
    """The W3C trace id for ``run`` (from its ``traceparent``), else the current span's."""
    if run is not None:
        parent = (run.trigger or {}).get("traceparent")
        if isinstance(parent, str):
            match = _TRACEPARENT.match(parent.strip().lower())
            if match and set(match.group(1)) != {"0"}:
                return match.group(1)
    try:
        from opentelemetry import trace  # optional dependency
    except ImportError:
        return None
    context = trace.get_current_span().get_span_context()
    if not getattr(context, "is_valid", False):
        return None
    return f"{context.trace_id:032x}"


def log_run(
    logger: logging.Logger,
    level: int,
    what: str,
    run: TaskRun,
    *,
    attempt: int | None = None,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    """Log ``task run <what>`` for ``run`` with its id, task, attempt and trace id."""
    if not logger.isEnabledFor(level):
        return
    number = run.attempt if attempt is None else attempt
    trace_id = trace_id_of(run)
    parts = [f"task run {what}", f"run_id={run.run_id}", f"task={run.task}", f"attempt={number}"]
    parts += [f"{key}={value}" for key, value in fields.items() if value is not None]
    if trace_id:
        parts.append(f"trace_id={trace_id}")
    extra = {
        "jobs_run_id": run.run_id,
        "jobs_task": run.task,
        "jobs_attempt": number,
        "jobs_trace_id": trace_id,
    }
    logger.log(level, "%s", " ".join(parts), extra=extra, exc_info=exc_info)
