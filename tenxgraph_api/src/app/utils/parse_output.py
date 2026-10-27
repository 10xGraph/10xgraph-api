from typing import Any

from pydantic import BaseModel

from tenxgraph_api.src.app.core.config.settings import Settings


# Provider payloads and framework bookkeeping stay on the server. The playground shows
# ``execution_meta.internal_data["error"]``, so that one key is kept; nothing reads the rest.
_CLIENT_INTERNAL_KEYS = frozenset({"error"})


def _strip_message(message: Any) -> None:
    if isinstance(message, dict):
        message.pop("raw", None)


def parse_state_output(settings: Settings, response: BaseModel) -> dict[str, Any]:
    data = response.model_dump(serialize_as_any=True)
    for message in data.get("context") or []:
        _strip_message(message)
    meta = data.get("execution_meta")
    if isinstance(meta, dict) and isinstance(meta.get("internal_data"), dict):
        internal = {k: v for k, v in meta["internal_data"].items() if k in _CLIENT_INTERNAL_KEYS}
        # Same rule as stream chunks: production shows a generic error, not the exception text.
        if "error" in internal and settings.MODE == "production":
            from tenxgraph_api.src.app.routers.graph.services.graph_service import (
                generic_error_text,
            )

            internal["error"] = generic_error_text(internal["error"])
        meta["internal_data"] = internal
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
