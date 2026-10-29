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
- ``collection`` -- the vector store collection; the developer picks it in code (store default,
  memory scope config, ``RAGAgent`` store config), so a client cannot read another collection or
  create collections without limit

A server-owned key from the client is dropped, not refused, and the drop is logged at debug.

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

A request is also bounded in size before anything heavy runs (:func:`request_size_error`): the
body cap lets a few MB of messages or content blocks through, and core then copies and serialises
them on the event loop. The limits are fixed module constants (``MAX_MESSAGES`` and friends).
A resume that names the interrupt it answers is checked against the thread's open interrupt
(:class:`InterruptConflictError`).
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BeforeValidator, ValidationInfo

from tenxgraph_api.src.app.core import logger


RESERVED_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "authz",
        "user",
        "user_id",
        "remote_tools",
        # Set by the AG-UI endpoint from the AG-UI request; never from a plain graph request
        "ag_ui",
        # Read by core: the prebuilt file tools' root, the tool and node time limits and the
        # parallel tool call cap
        "file_tool_root",
        "workspace_root",
        "tool_timeout",
        "node_timeout",
        "max_parallel_tool_calls",
        # Read by the store: which collection to read and write; set only by developer code
        "collection",
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
    kept = {key: value for key, value in config.items() if _is_client_key(key)}
    if len(kept) != len(config):
        dropped = sorted(str(key) for key in config if key not in kept)
        logger.debug("Ignored server-owned config keys from the client: %s", dropped)
    return kept


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


# Request size limits, applied in schema validation to client messages (graph invoke and stream,
# WebSocket, AG-UI, PUT state ``context``, POST messages). Fixed on purpose: no setting.
MAX_MESSAGES = 500
MAX_BLOCKS_PER_MESSAGE = 100
# Characters (``len`` of the Python string, not UTF-8 bytes) of text summed over every text block
# of the request; a plain string ``content`` (AG-UI) counts too.
MAX_TEXT_CHARS = 1_000_000
# AG-UI clients resend the whole conversation on every run. Only the trailing turn reaches the
# graph and gets the limits above; the full history gets this looser message cap (plus the
# per-message block cap) so a long chat keeps working.
MAX_AG_UI_HISTORY_MESSAGES = 10_000


# Deepest nesting of dicts and lists accepted in client ``initial_state``, ``config`` and
# ``resume``. Deeper input would fail with RecursionError inside the run (copy, serialise).
MAX_NESTING_DEPTH = 64


def nesting_depth_error(value: Any, field: str, max_depth: int = MAX_NESTING_DEPTH) -> str | None:
    """Describe a ``value`` nested deeper than ``max_depth`` dicts and lists, or ``None``.

    Walks with an explicit stack, so a hostile value cannot trigger RecursionError here.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list | tuple):
            children = item
        else:
            continue
        if depth > max_depth:
            return f"{field}: nested more than {max_depth} levels deep"
        stack.extend((child, depth + 1) for child in children)
    return None


# Size caps for the open dicts and lists a client sends (``config``, ``initial_state``,
# ``metadata``, ``filters``, ``resume`` ...). Per container, plus a cap on all values together,
# so a wide or bushy body cannot cost more memory than a normal request does. Fixed: no setting.
MAX_CONTAINER_ITEMS = 10_000
MAX_DICT_KEYS = 1_000
MAX_TOTAL_NODES = 200_000


def structure_error(value: Any, field: str) -> str | None:
    """Describe why a client dict or list is too deep or too large, or ``None`` when it fits.

    One walk with an explicit stack: nesting beyond ``MAX_NESTING_DEPTH``, a dict with more than
    ``MAX_DICT_KEYS`` keys, a list with more than ``MAX_CONTAINER_ITEMS`` items, or more than
    ``MAX_TOTAL_NODES`` values in all.
    """
    if (error := nesting_depth_error(value, field)) is not None:
        return error
    stack: list[Any] = [value]
    total = 0
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if len(item) > MAX_DICT_KEYS:
                return f"{field}: an object has more than {MAX_DICT_KEYS} keys"
            children: Any = item.values()
        elif isinstance(item, list | tuple):
            if len(item) > MAX_CONTAINER_ITEMS:
                return f"{field}: a list has more than {MAX_CONTAINER_ITEMS} items"
            children = item
        else:
            continue
        total += len(item)
        if total > MAX_TOTAL_NODES:
            return f"{field}: more than {MAX_TOTAL_NODES} values in all"
        stack.extend(children)
    return None


def _bounded(value: Any, info: ValidationInfo) -> Any:
    if (error := structure_error(value, info.field_name or "value")) is not None:
        raise ClientInputError(error)
    return value


# A client-supplied open dict, checked for depth and size before it is parsed further.
BoundedDict = Annotated[dict[str, Any], BeforeValidator(_bounded)]


class InterruptConflictError(Exception):
    """A resume names an interrupt the thread is not paused at (HTTP 409, or an error chunk)."""


def request_size_error(
    messages: Any,
    field: str = "messages",
    *,
    max_messages: int = MAX_MESSAGES,
    max_text_chars: int | None = MAX_TEXT_CHARS,
) -> str | None:
    """Describe the first size limit ``messages`` breaks, or ``None``.

    Cheap on purpose: counts are checked before anything is iterated, so a huge request is
    rejected without walking it. Accepts ``Message`` objects, AG-UI messages or plain dicts, and
    ignores anything that is not a list (schema validation reports that). ``max_text_chars=None``
    skips the total text check.
    """
    if not isinstance(messages, list | tuple):
        return None
    if len(messages) > max_messages:
        return f"{field}: at most {max_messages} messages per request (got {len(messages)})"
    total = 0
    for index, message in enumerate(messages):
        content = _field(message, "content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list | tuple):
            if len(content) > MAX_BLOCKS_PER_MESSAGE:
                return (
                    f"{field}[{index}]: at most {MAX_BLOCKS_PER_MESSAGE} content blocks per "
                    f"message (got {len(content)})"
                )
            for block in content:
                if _field(block, "type") == "text":
                    text = _field(block, "text")
                    total += len(text) if isinstance(text, str) else 0
        if max_text_chars is not None and total > max_text_chars:
            return (
                f"{field}: at most {max_text_chars} characters of text in total across all "
                "text blocks of a request"
            )
    return None


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


# One rule for every id a client names in a path or body (thread, message, memory). The API's
# own ids (Snowflake integers, UUIDs, memory ids from the stores) all fit; a longer or odder id
# would reach log lines, Redis keys and database keys unbounded.
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")


def resource_id_error(value: Any, field: str) -> str | None:
    """Why ``value`` is not an acceptable resource id, or ``None`` when it is.

    Checked in one place, ``RequirePermission``, for the ``thread_id``, ``message_id`` and
    ``memory_id`` path parameters and the ``thread_id`` / ``config.thread_id`` body fields.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str | int):
        return f"{field} must be a string or integer"
    if not RESOURCE_ID_PATTERN.fullmatch(str(value)):
        return (
            f"{field} must be 1 to 128 characters of letters, digits and . _ : @ - "
            "(no spaces or slashes)"
        )
    return None


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
