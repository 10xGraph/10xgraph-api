"""Tool audit for graph runs inside tasks: the API's ``ToolAudit``, fed by a lifecycle hook.

A task runs the graph itself (``graph.ainvoke``), so no API run path sees its messages. The
worker registers ``TaskToolAudit`` on the graph's ``CallbackManager`` when the audit log is on;
it hands each run's new messages, its pauses and its resumes to one ``ToolAudit`` per run, so
tool calls and approvals are recorded exactly as on ``/v1/graph/invoke``.
"""

from __future__ import annotations

import logging
from typing import Any

from tenxgraph.core.authz import get_identity
from tenxgraph.utils.callbacks import CallbackManager, GraphLifecycleContext, GraphLifecycleHook
from tenxgraph.utils.interrupt import RESUME_KEY

from tenxgraph_api.src.app.core.audit import audit_enabled, configure_audit
from tenxgraph_api.src.app.core.audit.tools import ToolAudit


logger = logging.getLogger("tenxgraph_api.jobs")

# Runs tracked at once; a run cancelled without an end hook is dropped oldest first.
MAX_RUNS = 10_000


class TaskToolAudit(GraphLifecycleHook):
    """One ``ToolAudit`` per graph run (by run id), from its first hook to its end, error or
    pause."""

    def __init__(self, checkpointer: Any) -> None:
        self.checkpointer = checkpointer
        self.runs: dict[str, ToolAudit] = {}

    def _audit(self, context: GraphLifecycleContext) -> ToolAudit:
        key = context.run_id or context.thread_id
        audit = self.runs.get(key)
        if audit is None:
            if len(self.runs) >= MAX_RUNS:
                self.runs.pop(next(iter(self.runs)))
            audit = ToolAudit(
                get_identity(context.config), context.thread_id or None, context.run_id or None
            )
            self.runs[key] = audit
        return audit

    def _end(self, context: GraphLifecycleContext) -> ToolAudit:
        audit = self._audit(context)
        self.runs.pop(context.run_id or context.thread_id, None)
        return audit

    async def on_resume(
        self,
        context: GraphLifecycleContext,
        resumed_node: str,
        state: Any,
        resume_data: dict[str, Any],
    ) -> None:
        audit = self._audit(context)
        if RESUME_KEY in resume_data:
            await audit.approval(state, resume_data[RESUME_KEY])
        await audit.before_resume(self.checkpointer, context.config, state)

    async def on_state_update(
        self,
        context: GraphLifecycleContext,
        node_name: str,
        old_state: Any,
        new_state: Any,
        step: int,
    ) -> None:
        audit = self._audit(context)
        seen = {message.message_id for message in old_state.context or []}
        for message in new_state.context or []:
            if message.message_id not in seen:
                await audit.record(message)

    async def on_interrupt(
        self,
        context: GraphLifecycleContext,
        interrupted_node: str,
        interrupt_type: str,
        state: Any,
    ) -> None:
        await self._end(context).record_paused(self.checkpointer, context.config, state)

    async def on_graph_end(self, context: GraphLifecycleContext, *args: Any) -> None:
        self._end(context)

    async def on_graph_error(self, context: GraphLifecycleContext, *args: Any) -> None:
        self._end(context)


def setup_worker_audit(container: Any, graph_config: Any) -> None:
    """The ``audit`` block as the API lifespan applies it; with audit on, ``TaskToolAudit``
    on the graph's callback manager. Call after the graph is loaded (auth class bound)."""
    from tenxgraph.storage.checkpointer import BaseCheckpointer

    from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth

    configure_audit(graph_config.audit, auth_enabled=container.try_get(BaseAuth) is not None)
    if not audit_enabled():
        return
    callbacks = container.try_get(CallbackManager)
    if callbacks is None:
        logger.warning("No CallbackManager bound: tool calls inside tasks are not audited")
        return
    checkpointer = container.try_get(BaseCheckpointer) if BaseCheckpointer in container else None
    callbacks.register_lifecycle_hook(TaskToolAudit(checkpointer))
