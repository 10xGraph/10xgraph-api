"""Audit events for the tool calls of a run and for the approvals that resume one.

Built from what every run path already sees, so local and MCP tools, ToolGuard denials and
human approvals are recorded without a hook in core:

- invoke: the result messages; stream, WebSocket and AG-UI: a message chunk for a result and
  an ``ERROR`` chunk with status ``tool_failed`` or ``tool_denied`` for a failure;
- ``/v1/graph/live``: the ``tool_call`` and ``tool_result`` realtime events;
- the interrupt a resume answers, for approvals.

Each call is recorded once. A run that pauses in a tool turn (an approval, ``interrupt()``)
records the calls of that turn that already finished from the tool ledger, since invoke does
not return them; the resumed run then skips those calls when core replays them from the ledger.

Never the tool's arguments or result: only its name, call id and outcome. An error here is
logged and the run continues, like a sink error in :func:`audit_event`.
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

# The result texts core gives a tool call that failed, as a live session's result carries
# them (core's realtime ``tool_result`` event has no error flag).
_LIVE_FAILURE_PREFIXES = ("Tool '{name}' failed", "Internal execution error:")

Outcome = tuple[str, str, str, str | None]  # call id, tool name, result, reason


def _denial_reason(text: Any, name: str) -> str | None:
    """The reason of a ToolGuard denial, without the ``Denied by policy: <tool>.`` prefix."""
    if not isinstance(text, str):
        return None
    return text.removeprefix(f"Denied by policy: {name}.").strip()[:MAX_REASON_CHARS] or None


def _from_message(message: Message) -> Outcome | None:
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


def _outcome(item: Any) -> Outcome | None:
    """The outcome of a tool result ``Message`` or ``StreamChunk``; None for anything else."""
    if isinstance(item, Message):
        return _from_message(item)
    if not isinstance(item, StreamChunk):
        return None
    if item.event == StreamEvent.MESSAGE and item.message is not None:
        return _from_message(item.message)
    data = item.data or {}
    result = (
        _CHUNK_RESULTS.get(str(data.get("status")), None)
        if item.event == StreamEvent.ERROR
        else None
    )
    if result is None:
        return None
    name = str(data.get("tool_name") or "")
    reason = _denial_reason(data.get("error"), name) if result == "denied" else None
    return str(data.get("tool_call_id") or ""), name, result, reason


def _live_outcome(call_id: str, name: str, result: Any) -> Outcome:
    """The outcome of a live session's tool result (``{"result": ...}`` or ``{"error": ...}``)."""
    if not isinstance(result, dict) or "error" in result:
        return call_id, name, "failed", None
    output = result.get("result")
    if isinstance(output, str):
        if output.startswith(f"Denied by policy: {name}."):
            return call_id, name, "denied", _denial_reason(output, name)
        if output.startswith(tuple(p.format(name=name) for p in _LIVE_FAILURE_PREFIXES)):
            return call_id, name, "failed", None
    return call_id, name, "success", None


def _ledger_message(entry: Any) -> Message | None:
    """The tool result message in a tool ledger entry (``{"__kind__", "value"}``)."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if entry.get("__kind__") == "raw" and isinstance(value, dict):
        value = value.get("messages")
    try:
        return Message.model_validate(value)
    except Exception:
        return None


def _paused_turn(state: Any) -> tuple[str, list[str]] | None:
    """The message id and call ids of the last tool turn in ``state``, if any."""
    for message in reversed(getattr(state, "context", None) or []):
        if message.role == "assistant" and message.tools_calls:
            ids = [str(c.get("id")) for c in message.tools_calls if c.get("id")]
            return str(message.message_id), ids
    return None


class ToolAudit:
    """The tool audit of one run: who runs it, and which calls are already recorded."""

    def __init__(
        self, identity: Identity | None, thread_id: str | None, run_id: str | None
    ) -> None:
        self.identity = identity
        self.thread_id = thread_id
        self.run_id = run_id
        # Calls to skip once: results the client sent for its own tools, and calls a resumed
        # run replays from the tool ledger (recorded by the run that paused).
        self.skip: set[str] = set()
        self.recorded: set[str] = set()
        # Call ids the model asked for in this run, and whether the run resumes a pause: when
        # neither leaves a call unrecorded, ``after_run`` reads nothing.
        self.called: set[str] = set()
        self.resumed = False
        self._live_names: dict[str, str] = {}

    async def _emit(self, outcome: Outcome) -> None:
        call_id, name, result, reason = outcome
        if not call_id:
            return
        if call_id in self.skip:
            self.skip.discard(call_id)
            self.recorded.add(call_id)
            return
        self.recorded.add(call_id)
        await audit_event(
            "tool",
            result,  # type: ignore[arg-type]
            action="tool:call",
            resource=f"tool/{name}",
            reason=reason,
            identity=self.identity,
            thread_id=self.thread_id,
            run_id=self.run_id,
            tool_call_id=call_id,
        )

    async def record(self, item: Any) -> None:
        """Record a tool result ``Message`` or ``StreamChunk`` the run produced.

        Pass a stream chunk before the production error sanitizer, which replaces the
        denial text.
        """
        if not audit_enabled():
            return
        try:
            message = item if isinstance(item, Message) else getattr(item, "message", None)
            if isinstance(message, Message) and message.tools_calls and not message.delta:
                self.called.update(str(c.get("id")) for c in message.tools_calls if c.get("id"))
            if (outcome := _outcome(item)) is not None:
                await self._emit(outcome)
        except Exception as exc:
            logger.error("Tool audit event failed: %s: %s", type(exc).__name__, exc)

    async def record_live(self, event: Any) -> None:
        """Record a ``tool_result`` event of a live session (``tool_call`` gives the name).

        A call the session replays from the ledger after a reconnect has the same id and is
        recorded once.
        """
        if not audit_enabled():
            return
        try:
            kind = getattr(event, "type", None)
            if kind == "tool_call":
                self._live_names[str(event.id)] = str(event.name)
            elif kind == "tool_result" and str(event.id) not in self.recorded:
                name = self._live_names.pop(str(event.id), "")
                await self._emit(_live_outcome(str(event.id), name, event.result))
        except Exception as exc:
            logger.error("Tool audit event failed: %s: %s", type(exc).__name__, exc)

    async def _ledger(self, checkpointer: Any, config: dict[str, Any], state: Any) -> list:
        """``(call id, ledger entry)`` for each finished call of the state's last tool turn."""
        turn = _paused_turn(state)
        if turn is None or checkpointer is None:
            return []
        message_id, call_ids = turn
        found = []
        for call_id in call_ids:
            entry = await checkpointer.aget_tool_result(config, f"{message_id}:{call_id}")
            if entry is not None:
                found.append((call_id, entry))
        return found

    async def before_resume(self, checkpointer: Any, config: dict[str, Any], state: Any) -> None:
        """On a resume: skip the calls of the paused turn that finished before the pause."""
        if not audit_enabled() or state is None or pending_interrupt(state) is None:
            return
        self.resumed = True
        try:
            self.skip.update(
                call_id for call_id, _ in await self._ledger(checkpointer, config, state)
            )
        except Exception as exc:
            logger.error("Tool audit ledger read failed: %s: %s", type(exc).__name__, exc)

    async def after_run(self, checkpointer: Any, config: dict[str, Any]) -> None:
        """When the run paused in a tool turn: record that turn's finished calls not seen yet."""
        if not audit_enabled() or checkpointer is None:
            return
        if not self.resumed and not self.called - self.recorded:
            return
        try:
            state = await checkpointer.aget_state(config)
            if state is None or pending_interrupt(state) is None:
                return
            for call_id, entry in await self._ledger(checkpointer, config, state):
                if call_id in self.recorded or call_id in self.skip:
                    continue
                message = _ledger_message(entry)
                outcome = _from_message(message) if message is not None else None
                await self._emit(outcome or (call_id, "", "success", None))
        except Exception as exc:
            logger.error("Tool audit ledger read failed: %s: %s", type(exc).__name__, exc)

    async def approval(self, state: Any, resume: Any) -> None:
        """One ``tool:approve`` event when a resume answers a ToolGuard approval pause.

        ``allowed`` for an approval; ``denied`` with reason ``rejected``, or ``cancelled`` for
        a ``None`` resume. A value core will refuse (not a boolean, object or ``None``) is
        skipped: that run fails and nothing is decided.
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
                identity=self.identity,
                thread_id=self.thread_id,
                run_id=self.run_id,
                tool_call_id=str(call_id) if call_id else None,
            )
        except Exception as exc:
            logger.error("Tool approval audit event failed: %s: %s", type(exc).__name__, exc)
