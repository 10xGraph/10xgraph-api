"""What a response may show of messages and state: the same rule on every route.

Provider payloads (``Message.raw``) and framework bookkeeping (``execution_meta.internal_data``)
stay on the server. The playground shows ``internal_data["error"]``, so that one key is kept
(generic in production); nothing reads the rest. Thread routes, invoke, fix, stream, WebSocket
and AG-UI all go through these functions.
"""

from typing import Any

from pydantic import BaseModel

from tenxgraph_api.src.app.core.config.settings import Settings
from tenxgraph_api.src.app.core.exceptions.client_errors import generic_error_text


_CLIENT_INTERNAL_KEYS = frozenset({"error"})


def _strip_message(message: Any) -> None:
    if isinstance(message, dict):
        message.pop("raw", None)


def _client_internal(internal: dict[str, Any], production: bool) -> dict[str, Any]:
    kept = {k: v for k, v in internal.items() if k in _CLIENT_INTERNAL_KEYS}
    # Same rule as stream error chunks: production shows a generic error, not the exception.
    if "error" in kept and production:
        kept["error"] = generic_error_text(kept["error"])
    return kept


def parse_state_output(settings: Settings, response: BaseModel) -> dict[str, Any]:
    """A state as a dict without message ``raw`` payloads and with ``internal_data["error"]``
    only."""
    data = response.model_dump(serialize_as_any=True)
    for message in data.get("context") or []:
        _strip_message(message)
    meta = data.get("execution_meta")
    if isinstance(meta, dict) and isinstance(meta.get("internal_data"), dict):
        meta["internal_data"] = _client_internal(
            meta["internal_data"], settings.MODE == "production"
        )
    return data


def parse_message_output(settings: Settings, response: Any) -> Any:
    """A message, or a ``{"messages": [...]}`` list response, as a dict without ``raw``.

    Anything that is not a model (already a dict, for example) passes through unchanged.
    """
    if not isinstance(response, BaseModel):
        return response
    data = response.model_dump(serialize_as_any=True)
    _strip_message(data)
    for message in data.get("messages") or []:
        _strip_message(message)
    return data


def client_message(message: Any) -> Any:
    """``message`` without its provider ``raw`` payload (a copy; the original is untouched)."""
    if getattr(message, "raw", None) is None:
        return message
    return message.model_copy(update={"raw": None})


def client_state(state: Any, production: bool) -> Any:
    """A state model without message ``raw`` payloads or extra ``internal_data`` keys (a copy
    when anything changes; the graph checkpoints its own objects)."""
    if not isinstance(state, BaseModel):
        return state
    update: dict[str, Any] = {}
    context = getattr(state, "context", None)
    if isinstance(context, list) and any(getattr(m, "raw", None) is not None for m in context):
        update["context"] = [client_message(m) for m in context]
    meta = getattr(state, "execution_meta", None)
    internal = getattr(meta, "internal_data", None)
    if meta is not None and isinstance(internal, dict):
        kept = _client_internal(internal, production)
        if kept != internal:
            update["execution_meta"] = meta.model_copy(update={"internal_data": kept})
    return state.model_copy(update=update) if update else state


def client_chunk(chunk: Any, production: bool) -> Any:
    """A stream chunk with :func:`client_message` and :func:`client_state` applied."""
    update: dict[str, Any] = {}
    message = getattr(chunk, "message", None)
    if message is not None and getattr(message, "raw", None) is not None:
        update["message"] = client_message(message)
    state = getattr(chunk, "state", None)
    if state is not None:
        safe = client_state(state, production)
        if safe is not state:
            update["state"] = safe
    return chunk.model_copy(update=update) if update else chunk
