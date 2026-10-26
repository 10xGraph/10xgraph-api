"""Keep server-owned keys out of client-supplied request ``config``.

Request ``config`` is an open dict on purpose: frontends pass their own values through it and
graph nodes and tools read them. Its keys cannot be allow-listed, because the API does not know
what an application sends.

A small set of keys is owned by the server instead, and a client must never set them:

- ``authz``   -- the isolation policy and scopes; the trusted copy lives in ``user["authz"]``
- ``user``    -- the verified identity
- ``user_id`` -- derived from the verified identity
- ``remote_tools`` -- per-run client tool schemas the model may call; only protocol adapters
  whose clients legitimately bring tools (AG-UI) set it, from the protocol's own tool list
- ``_*``      -- framework internals (``_skip_interrupt_at``, ``_node_name``, ...)
- ``file_tool_root`` / ``workspace_root`` -- the directory the core prebuilt file tools may
  read and write; a client must not be able to point them at the server's filesystem
- ``tool_timeout`` / ``node_timeout`` -- core time limits, which ``0`` or ``None`` turns off
- ``max_parallel_tool_calls`` -- core's cap on tool calls per model turn; the operator sets it
  with ``runs.max_parallel_tool_calls``

Every route and service that forwards client config to the graph, checkpointer or store must
pass it through :func:`client_config` first, then set the server-owned keys itself.

Operators may narrow the open dict further with ``runs.config_keys`` and ``runs.state_keys`` in
10xgraph.json; :func:`reject_unlisted_keys` enforces them on graph input and state writes.

The thread a request runs on is also server-controlled: routes use the thread the permission
check approved, and both sides normalise it with :func:`normalize_thread_id`.

Tool calls are server-controlled too: only the model may request a tool. Client messages
are checked with :func:`client_tool_call_error`. A client may send a tool *result* only as the
answer to a remote tool call that is still waiting for one (:func:`unanswered_tool_calls`).

Client messages are user messages. :func:`client_message_error` refuses the ``assistant`` and
``system`` roles (a forged assistant turn persists and the model reads it as its own words) and
lets a ``tool`` message through only when it carries nothing but tool results. The built-in state
fields ``context_summary`` and ``execution_meta`` are never client-writable
(:func:`reject_builtin_state_keys`). The server also generates every message id itself
(:func:`with_fresh_message_ids`); the AG-UI adapter alone keeps client ids, since its converter
uses them to skip messages the thread already stores.
"""

from __future__ import annotations

from typing import Any


RESERVED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "authz",
        "user",
        "user_id",
        "remote_tools",
        # Read by core: the prebuilt file tools' root, the tool and node time limits and the
        # parallel tool call cap
        "file_tool_root",
        "workspace_root",
        "tool_timeout",
        "node_timeout",
        "max_parallel_tool_calls",
    }
)
INTERNAL_KEY_PREFIX = "_"


def _is_client_key(key: Any) -> bool:
    return (
        isinstance(key, str)
        and key not in RESERVED_CONFIG_KEYS
        and not key.startswith(INTERNAL_KEY_PREFIX)
    )


def client_config(config: Any) -> dict[str, Any]:
    """Return a copy of client-supplied config without server-owned keys.

    Application keys pass through unchanged, including ``thread_id``, which each route then
    replaces with, or checks against, the thread the permission check approved.
    """
    if not isinstance(config, dict):
        return {}
    return {key: value for key, value in config.items() if _is_client_key(key)}


class ClientInputError(ValueError):
    """A problem with the client's own input, described in words written for the client.

    Routes turn ``ValueError`` into 422. Only this subclass keeps its message in production:
    any other ``ValueError`` may come from deep inside the graph, a tool or an SDK and carry
    internal details, so production replaces its message (see :func:`client_error_detail`).
    """


def client_error_detail(exc: Exception) -> str:
    """The 422 detail for ``exc``: its message, unless production and not a ClientInputError."""
    if isinstance(exc, ClientInputError):
        return str(exc)
    from tenxgraph_api.src.app.core.config.settings import get_settings

    if get_settings().MODE == "production":
        return "Invalid input provided."
    return str(exc)


# State fields only developer code (nodes, context managers) writes. ``context`` is not listed:
# core already ignores it in caller state, and ``PUT state`` appends to it (see the message rules).
BUILTIN_STATE_KEYS: frozenset[str] = frozenset({"context_summary", "execution_meta"})


def reject_builtin_state_keys(state: Any, field: str) -> None:
    """Raise ``ClientInputError`` when client ``state`` sets a built-in state field.

    ``context_summary`` is sent to the model as its own earlier words and ``execution_meta``
    drives the run, so neither may come from a client.
    """
    if not isinstance(state, dict):
        return
    blocked = sorted(key for key in state if key in BUILTIN_STATE_KEYS)
    if blocked:
        raise ClientInputError(f"{field} keys cannot be set by clients: {', '.join(blocked)}")


# Config keys the API itself reads; always accepted when ``runs.config_keys`` is set.
ALWAYS_ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset({"thread_id", "run_id"})


def reject_unlisted_keys(
    data: Any,
    allowed: frozenset[str] | None,
    field: str,
    always: frozenset[str] = frozenset(),
) -> None:
    """Raise ``ValueError`` when ``data`` carries a key outside ``allowed`` (plus ``always``).

    ``allowed=None`` means no allow-list is configured and every key passes. Used for the
    ``runs.config_keys`` / ``runs.state_keys`` settings, so a graph that trusts a value such as
    ``role`` from config or state cannot be handed one by the client.
    """
    if allowed is None or not isinstance(data, dict):
        return
    unlisted = sorted(str(key) for key in data if key not in allowed and key not in always)
    if unlisted:
        raise ClientInputError(f"{field} keys not allowed by this server: {', '.join(unlisted)}")


# Content blocks that ask the server to run a tool. Only the model produces these; tool
# *results* (``tool_result``) are fine, since remote tools send them back from the client.
TOOL_CALL_BLOCK_TYPES: frozenset[str] = frozenset({"tool_call", "remote_tool_call"})


def _field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def client_tool_call_error(messages: Any) -> str | None:
    """Describe the first client-supplied message that carries a tool call, or ``None``.

    A ``ToolNode`` executes the ``tools_calls`` of the last message in context, so a client
    that can write a tool call can run any server tool with its own arguments, skipping the
    model, the system prompt and any guard node. Accepts ``Message`` objects or plain dicts.
    """
    if not isinstance(messages, list | tuple):
        return None
    for index, message in enumerate(messages):
        carries_call = bool(_field(message, "tools_calls")) or any(
            _field(block, "type") in TOOL_CALL_BLOCK_TYPES
            for block in _field(message, "content") or []
        )
        if carries_call:
            return f"messages[{index}] carries a tool call; only the model may request tools"
    return None


def client_message_error(messages: Any, *, user_only: bool = True) -> str | None:
    """Describe the first client message the API must refuse, or ``None``.

    Always refuses tool calls (:func:`client_tool_call_error`). With ``user_only`` (every client
    route except the admin import) it also refuses the ``assistant`` and ``system`` roles, and a
    ``tool`` message that carries anything but tool results; whether a result answers a pending
    call is checked separately (``GraphService._check_tool_results``).
    """
    if error := client_tool_call_error(messages):
        return error
    if not user_only or not isinstance(messages, list | tuple):
        return None
    for index, message in enumerate(messages):
        role = _field(message, "role")
        if role in ("assistant", "system"):
            return f"messages[{index}] has role '{role}'; clients may only send user messages"
        if role == "tool":
            blocks = _field(message, "content") or []
            if not blocks or any(_field(block, "type") != "tool_result" for block in blocks):
                return f"messages[{index}] has role 'tool' but is not a tool result"
    return None


def with_fresh_message_ids(messages: Any) -> Any:
    """Copy ``messages`` (``Message`` objects or dicts) with a newly generated ``message_id``.

    Ids are predictable and some checkpointers key messages on the id, so a client must not
    choose them. The id comes from ``Message``'s own default factory.
    """
    if not isinstance(messages, list | tuple):
        return messages
    from tenxgraph.core.state import Message

    make_id: Any = Message.model_fields["message_id"].default_factory
    fresh: list[Any] = []
    for message in messages:
        if isinstance(message, dict):
            fresh.append({**message, "message_id": make_id()})
        elif hasattr(message, "model_copy"):
            fresh.append(message.model_copy(update={"message_id": make_id()}))
        else:
            fresh.append(message)
    return fresh


def tool_result_ids(messages: Any) -> list[str]:
    """The ``call_id`` of every tool result block in ``messages``, in order."""
    if not isinstance(messages, list | tuple):
        return []
    return [
        str(_field(block, "call_id"))
        for message in messages
        for block in _field(message, "content") or []
        if _field(block, "type") == "tool_result"
    ]


def _tool_calls(message: Any) -> list[tuple[str, str]]:
    """``(call_id, tool_name)`` for every tool call a message makes."""
    calls: list[tuple[str, str]] = []
    for call in _field(message, "tools_calls") or []:
        if isinstance(call, dict) and call.get("id"):
            name = (call.get("function") or {}).get("name") or call.get("name") or ""
            calls.append((str(call["id"]), str(name)))
    for block in _field(message, "content") or []:
        if _field(block, "type") in TOOL_CALL_BLOCK_TYPES and _field(block, "id"):
            calls.append((str(_field(block, "id")), str(_field(block, "name") or "")))
    return calls


def unanswered_tool_calls(context: Any) -> dict[str, str]:
    """Map ``call_id -> tool_name`` for the tool calls in ``context`` that have no result yet."""
    calls: dict[str, str] = {}
    for message in context or []:
        if _field(message, "role") == "assistant":
            for call_id, name in _tool_calls(message):
                calls.setdefault(call_id, name)
    for call_id in tool_result_ids(list(context or [])):
        calls.pop(call_id, None)
    return calls


def normalize_thread_id(value: Any) -> str | None:
    """The one canonical form of a thread id, shared by the permission check and the services.

    Returns ``None`` for a missing or blank id, which means "start a new thread".
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def owned_thread_info(config: dict[str, Any], thread_id: Any, **fields: Any) -> Any:
    """A ``ThreadInfo`` for ``thread_id`` that names the caller in ``config`` as its owner.

    The in-memory and SQLite checkpointers answer ``aget_thread_owner`` from the stored
    ``ThreadInfo.user_id``, so a thread saved without it has no owner and ownership checks
    cannot protect it. Every thread the API saves goes through here.
    """
    from tenxgraph.utils.thread_info import ThreadInfo

    user_id = config.get("user_id") if isinstance(config, dict) else None
    return ThreadInfo(thread_id=thread_id, user_id=user_id, **fields)
