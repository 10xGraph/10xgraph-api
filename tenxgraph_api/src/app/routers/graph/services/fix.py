"""The fix operation: drop broken tool calls from a thread's state."""

from typing import Any

from tenxgraph.core.authz import Identity
from tenxgraph.core.state import AgentState, Message

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    as_http_error,
)
from tenxgraph_api.src.app.core.threads import set_user
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (
    _field,
    _tool_calls,
    client_config,
    tool_result_ids,
    unanswered_tool_calls,
)
from tenxgraph_api.src.app.routers.graph.services.stream_utils import _log_run_error
from tenxgraph_api.src.app.utils.parse_output import (
    parse_state_output,
)


class FixMixin:
    """``GraphService`` method that repairs a thread's tool calls."""

    def _protected_tool_calls(self, state: Any, messages: list[Any]) -> set[str]:
        """Ids of the unanswered tool calls the thread is legitimately waiting on.

        Only the latest assistant message can be waiting. Its unanswered calls are pending
        work, not broken history, when the thread is paused (``interrupt()``, or HITL
        ``interrupt_before`` / ``interrupt_after``: ``execution_meta`` reports an interrupted
        status) or when a call belongs to a remote tool the client has yet to answer.
        """
        assistants = [m for m in messages if _field(m, "role") == "assistant"]
        if not assistants:
            return set()
        waiting = unanswered_tool_calls(messages)
        mine = {cid: name for cid, name in _tool_calls(assistants[-1]) if cid in waiting}
        if not mine:
            return set()
        if state.is_interrupted() or any(
            name in self._remote_tool_names() for name in mine.values()
        ):
            return set(mine)
        return set()

    def _drop_broken_tool_calls(self, state: Any, messages: list[Any]) -> list[Any]:
        """``messages`` without unanswered assistant tool calls and orphaned tool results."""
        keep_pending: set[str] = set()
        broken = set(unanswered_tool_calls(messages))
        if broken:
            keep_pending = self._protected_tool_calls(state, messages)
        kept = [
            m
            for m in messages
            if not (
                _field(m, "role") == "assistant"
                and any(cid in broken and cid not in keep_pending for cid, _ in _tool_calls(m))
            )
        ]
        live = {cid for m in kept if _field(m, "role") == "assistant" for cid, _ in _tool_calls(m)}
        return [
            m
            for m in kept
            if not ((ids := tool_result_ids([m])) and not any(i in live for i in ids))
        ]

    async def fix_graph(
        self,
        thread_id: str,
        user: Identity | None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Repair a thread whose history has a tool call that never got its result.

        A run that dies after the model asked for tools but before the results were saved
        leaves an assistant message with an unanswered tool call, which providers reject.
        This method removes every assistant message that has a tool call with no matching
        tool result (matched by call id), then removes tool results whose call is gone, and
        saves the state. Messages without tool calls are never removed. The unanswered calls
        of the latest assistant message are kept while the thread is paused or waiting on a
        client tool, because they are pending work.

        Args:
            thread_id (str): The thread ID to fix the graph state for
            user: The caller's identity, or None when auth is off
            config (dict, optional): Additional configuration for the operation

        Returns:
            dict: Result dictionary containing:
                - success (bool): Whether the operation was successful
                - message (str): Status message
                - removed_count (int): Number of messages removed
                - state (dict): Updated state after fixing

        Raises:
            HTTPException: If the operation fails
        """
        try:
            logger.info("Starting fix graph operation for thread: %s", log_safe(thread_id))
            logger.debug("User id: %s", log_safe(user.user_id if user else None))

            # Start with client config if provided, then overlay trusted attributes
            fix_config = client_config(config)
            fix_config["thread_id"] = thread_id
            set_user(fix_config, user)

            logger.debug("Fetching current state from checkpointer")
            state: AgentState | None = await self.checkpointer.aget_state(fix_config)
            if not state:
                logger.warning("No state found for thread: %s", log_safe(thread_id))
                return {
                    "success": False,
                    "message": f"No state found for thread: {thread_id}",
                    "removed_count": 0,
                    "state": None,
                }

            messages: list[Message] = list(state.context or [])
            logger.debug(f"Found {len(messages)} messages in state")
            if not messages:
                return {
                    "success": True,
                    "message": "No messages found in state",
                    "removed_count": 0,
                    "state": parse_state_output(get_settings(), state),
                }

            filtered = self._drop_broken_tool_calls(state, messages)
            removed_count = len(messages) - len(filtered)

            if removed_count:
                state.context = filtered
                await self.checkpointer.aput_state(fix_config, state)
                message = f"Successfully removed {removed_count} message(s)"
            else:
                message = "No unanswered tool calls or orphaned tool results found"

            return {
                "success": True,
                "message": message,
                "removed_count": removed_count,
                "state": parse_state_output(get_settings(), state),
            }
        except Exception as e:
            _log_run_error(e, "Fix graph operation failed")
            raise as_http_error(e, "Fix graph operation failed")
