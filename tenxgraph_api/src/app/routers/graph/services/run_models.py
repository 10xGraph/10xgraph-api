"""What one run is checked against and set up with: ``RunChecks`` and the per-run record."""

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from tenxgraph_api.src.app.core.audit.tools import ToolAudit
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    as_http_error,
)
from tenxgraph_api.src.app.core.leases import Lease


def _run_tool_names(config: dict[str, Any]) -> set[str]:
    """Names of the per-run client tools in ``config["remote_tools"]`` (server-owned key)."""
    names: set[str] = set()
    for item in config.get("remote_tools") or []:
        if isinstance(item, dict):
            spec = item.get("function") if isinstance(item.get("function"), dict) else item
            if isinstance(spec.get("name"), str):
                names.add(spec["name"])
    return names


def _resume_http_error(exc: Exception) -> Exception:
    """409 for a resume aimed at the wrong (or no) interrupt, 422 for an answer that fails the
    interrupt's schema. Both messages are written for the client, so production keeps them."""
    return as_http_error(exc)


# ``RunChecks.state`` before the thread state has been read.
_UNREAD: Any = object()


@dataclass
class RunChecks:
    """What was already checked for one run, so the run does not check or read it again.

    A router makes one with :meth:`GraphService.check_run_input` before anything is held.
    ``state`` is the thread's state, read at most once per run, under the thread lock.
    """

    input_checked: bool = False
    state: Any = _UNREAD


@dataclass
class _Run:
    """One run's setup, filled in by ``_start_run`` and cleaned up by ``_end_run``."""

    input_data: dict[str, Any] = dataclass_field(default_factory=dict)
    config: dict[str, Any] = dataclass_field(default_factory=dict)
    # Initialised so the error chunk has it even when setup fails part way.
    meta: dict[str, Any] = dataclass_field(default_factory=dict)
    lock: Lease = dataclass_field(default_factory=lambda: Lease(None))
    thread_id: str | None = None
    run_id: str | None = None
    started: float = 0.0
    # The run's tool audit, set by ``_start_run``.
    tools: ToolAudit = dataclass_field(default_factory=lambda: ToolAudit(None, None, None))
