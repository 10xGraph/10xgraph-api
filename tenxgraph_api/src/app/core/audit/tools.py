"""Audit events for the tool calls of a run and for the approvals that resume one.

Built from what every run path already sees: the tool results the run hands back (messages on
invoke; on a stream, WebSocket or AG-UI run, a message for a result and an ``ERROR`` chunk with
status ``tool_failed`` or ``tool_denied`` for a failure) and the interrupt a resume answers. So
local and MCP tools, ToolGuard denials and human approvals are recorded without a hook in core.
Never the tool's arguments or result: only its name, call id and outcome.

An error here is logged and the run continues, like a sink error in :func:`audit_event`.
"""

import logging
from typing import Any

from tenxgraph.core.authz import Identity
from tenxgraph.core.graph.tool_node.denial import TOOL_DENIED_META
from tenxgraph.core.graph.tool_node.guard import APPROVAL_REASON, is_approved
from tenxgraph.core.state import ErrorBlock, Message, StreamChunk, StreamEvent, ToolResultBlock
from tenxgraph.utils.interrupt import pending_interrupt

from tenxgraph_api.src.app.core.audit import audit_enabled, audit_event


logger = logging.getLogger("tenxgraph_api")

# The denial reason is the policy's own sentence; a longer one is cut.
MAX_REASON_CHARS = 200

# ``data["status"]`` of the stream's error chunk for a failed or denied tool call.
_CHUNK_RESULTS = {"tool_failed": "failed", "tool_denied": "denied"}


def _denial_reason(text: Any, name: str) -> str | None:
    """The reason of a ToolGuard denial, without the ``Denied by policy: <tool>.`` prefix."""
    if not isinstance(text, str):
        return None
    return text.removeprefix(f"Denied by policy: {name}.").strip()[:MAX_REASON_CHARS] or None


def _from_message(message: Message) -> tuple[str, str, str, str | None] | None:
    """``(call_id, tool name, result, reason)`` of a tool result message, else None."""
    if message.role != "tool" or message.delta:
        return None
    block = next((b for b in message.content or [] if isinstance(b, ToolResultBlock)), None)
    if block is None:
        return None
    meta = message.metadata or {}
    name = str(meta.get("function_name") or block.name or "")
    call_id = str(meta.get("tool_call_id") or block.call_id or "")
    if meta.get(TOOL_DENIED_META):
        error = next((b.message for b in message.content if isinstance(b, ErrorBlock)), None)
        return call_id, name, "denied", _denial_reason(error, name)
    if block.is_error or block.status == "failed":
        return call_id, name, "failed", None
    return call_id, name, "success", None


def _outcome(item: Any) -> tuple[str, str, str, str | None] | None:
    if isinstance(item, Message):
        return _from_message(item)
    if not isinstance(item, StreamChunk):
        return None
    if item.event == StreamEvent.MESSAGE and item.message is not None:
        return _from_message(item.message)
    data = item.data or {}
    key = data.get("status", None)
    result = (_CHUNK_RESULTS.get(key) if item.event == StreamEvent.ERROR else None) if key else None
    if result is None:
        return None
    name = str(data.get("tool_name") or "")
    reason = _denial_reason(data.get("error"), name) if result == "denied" else None
    return str(data.get("tool_call_id") or ""), name, result, reason


async def audit_tool_result(
    item: Any,
    *,
    identity: Identity | None,
    thread_id: str | None,
    run_id: str | None,
    seen: set[str],
) -> None:
    """One ``tool`` event for a tool result the run produced (a ``Message`` or ``StreamChunk``).

    ``seen`` holds the call ids already recorded in this run, plus the results the client sent
    for its own tools (those ran in the frontend), so each call is recorded once. Pass a stream
    chunk before the production error sanitizer, which replaces the denial text.
    """
    if not audit_enabled():
        return
    try:
        outcome = _outcome(item)
        if outcome is None or not outcome[0] or outcome[0] in seen:
            return
        call_id, name, result, reason = outcome
        seen.add(call_id)
        await audit_event(
            "tool",
            result,  # type: ignore[arg-type]
            action="tool:call",
            resource=f"tool/{name}",
            reason=reason,
            identity=identity,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=call_id,
        )
    except Exception as exc:
        logger.error("Tool audit event failed: %s: %s", type(exc).__name__, exc)


async def audit_tool_approval(
    state: Any,
    resume: Any,
    *,
    identity: Identity | None,
    thread_id: str | None,
    run_id: str | None,
) -> None:
    """One ``tool:approve`` event when a resume answers a ToolGuard approval pause.

    ``allowed`` for an approval; ``denied`` with reason ``rejected``, or ``cancelled`` for a
    ``None`` resume. A value core will refuse (not a boolean, object or ``None``) is skipped:
    that run fails and nothing is decided.
    """
    if not audit_enabled() or state is None:
        return
    try:
        pending = pending_interrupt(state)
        if pending is None or pending.reason != APPROVAL_REASON:
            return
        if resume is not None and not isinstance(resume, bool | dict):
            return
        value = pending.value if isinstance(pending.value, dict) else {}
        approved = is_approved(resume)
        reason = None if approved else ("cancelled" if resume is None else "rejected")
        call_id = pending.tool_call_id or value.get("tool_call_id")
        await audit_event(
            "tool",
            "allowed" if approved else "denied",
            action="tool:approve",
            resource=f"tool/{value.get('tool_name') or ''}",
            reason=reason,
            identity=identity,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=str(call_id) if call_id else None,
        )
    except Exception as exc:
        logger.error("Tool approval audit event failed: %s: %s", type(exc).__name__, exc)
