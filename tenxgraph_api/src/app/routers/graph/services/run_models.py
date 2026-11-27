"""What one run is checked against and set up with: ``RunChecks`` and the per-run record."""

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from tenxgraph.core.exceptions import InterruptPendingError
from tenxgraph.core.graph.utils.utils import ABORTED_RUN_REASONS
from tenxgraph.core.state.execution_state import ExecutionStatus

from tenxgraph_api.src.app.core.audit.tools import ToolAudit
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    as_http_error,
)
from tenxgraph_api.src.app.core.exceptions.resources_exceptions import (
    MessageAlreadyProcessedError,
)
from tenxgraph_api.src.app.core.leases import Lease
from tenxgraph_api.src.app.core.validation.request_config import (
    CLIENT_MESSAGE_ID,
)


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


def _client_id(message: Any) -> str | None:
    """The client id the schema recorded on a user message (``None`` for other roles)."""
    if getattr(message, "role", None) != "user":
        return None
    return (getattr(message, "metadata", None) or {}).get(CLIENT_MESSAGE_ID)


def has_client_ids(messages: list[Any]) -> bool:
    """Whether any user message carries a client id, so a retry is possible at all."""
    return any(_client_id(m) is not None for m in messages)


def unseen_messages(messages: list[Any], state: Any) -> list[Any]:
    """``messages`` without the user messages the thread already stores (a client retry).

    A user message is a repeat when its client id matches a stored ``client_message_id``. Stored
    server message ids never match: a REST/WS client is never sent its own message back under a
    server id, so a new message that reuses one (say the assistant reply's) is new. Tool
    results are always kept.

    When every message is a repeat, the thread's run status decides. The repeat must be the
    thread's latest user message, and that turn must not have finished: a run that is still
    marked running (a crash), failed (``error``) or was cut off (cancel, shutdown, crash
    recovery, stop) returns ``[]``, so core continues it from the stored node. A completed turn
    is a 409. A state with no ``execution_meta`` falls back to "the repeat is the last message".

    Raises:
        MessageAlreadyProcessedError: The repeat is not the latest user message, or its turn
            completed.
        HTTPException: 409, the thread is paused (``interrupt()``, ``interrupt_before`` /
            ``interrupt_after``, a client-side tool) and must be resumed, not sent the message
            again.
    """
    context = list(getattr(state, "context", None) or [])
    if not context:
        return messages
    known = {str(m.metadata[CLIENT_MESSAGE_ID]) for m in context if CLIENT_MESSAGE_ID in m.metadata}
    dropped = {_client_id(m) for m in messages} & known
    kept = [m for m in messages if _client_id(m) not in dropped]
    if kept or not messages:
        return kept
    last_user = next((m for m in reversed(context) if m.role == "user"), None)
    if last_user is None or str(last_user.metadata.get(CLIENT_MESSAGE_ID)) not in dropped:
        raise MessageAlreadyProcessedError()
    meta = getattr(state, "execution_meta", None)
    if meta is None:  # an old state: no run status to go by
        if context[-1] is last_user:
            return []
        raise MessageAlreadyProcessedError()
    if meta.status == ExecutionStatus.COMPLETED:
        raise MessageAlreadyProcessedError()
    if meta.is_interrupted() and meta.interrupt_reason not in ABORTED_RUN_REASONS:
        raise _resume_http_error(
            InterruptPendingError(
                f"Thread is paused in node '{meta.interrupted_node}' and already has this "
                "message. Resume the thread instead of sending the message again."
            )
        )
    return []


# ``RunChecks.state`` before the thread state has been read.
_UNREAD: Any = object()


@dataclass(slots=True)
class RunChecks:
    """What was already checked for one run, so the run does not check or read it again.

    A router makes one with :meth:`GraphService.check_run_input` before anything is held.
    ``state`` is the thread's state, read at most once per run, under the thread lock.
    """

    input_checked: bool = False
    state: Any = _UNREAD


@dataclass(slots=True)
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
