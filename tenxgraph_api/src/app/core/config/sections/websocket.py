"""``websocket`` section of 10xgraph.json."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass
from tenxgraph_api.src.app.core.config.sections.common import _parse_bool


logger = logging.getLogger("tenxgraph_api")


# Finite defaults so an unconfigured server cannot be held open by one client.
DEFAULT_WS_MAX_CONNECTIONS = 1000
DEFAULT_WS_MAX_CONNECTIONS_PER_USER = 10

# Realtime init-frame options a client may set (``model`` and ``tools_tags`` are restricted
# separately). ``system_prompt`` is off by default: it replaces the agent's instructions.
REALTIME_CLIENT_OVERRIDE_KEYS = frozenset({"voice", "modalities", "vad", "system_prompt"})
DEFAULT_REALTIME_CLIENT_OVERRIDES: tuple[str, ...] = ("voice", "modalities", "vad")


@dataclass
class WebSocketConfig:
    """WebSocket settings and connection limits parsed from 10xgraph.json.

    Example::

        "websocket": {
            "enabled": true,
            "max_connections": 1000,
            "max_connections_per_user": 10,
            "realtime_models": ["gemini-3.8-live"],
            "realtime_client_overrides": ["voice", "modalities", "vad"]
        }

    ``max_connections`` caps the concurrent WebSocket connections the server accepts
    (realtime ``/v1/graph/live`` + streaming ``/v1/graph/ws``).
    ``max_connections_per_user`` caps how many of those one verified user may hold, so a
    single account cannot take every slot. Both are counted in the API's cache store: across
    workers with a shared one (``cache_store`` type ``redis`` or ``custom``), per process with
    the in-memory one (then size them per worker).

    A missing key gets a finite default (1000 and 10). Set a key to ``0`` or ``null`` to make
    it unlimited. WebSocket handshakes, and every graph run started over ``/v1/graph/ws``, also
    count against the global ``rate_limit`` bucket shared with REST requests.

    ``realtime_models`` lists the models a ``/v1/graph/live`` client may ask for in its init
    frame. Any other requested model is ignored and the agent's own model is used. Empty (the
    default) means clients cannot choose the model.

    ``realtime_client_overrides`` lists the other init-frame session options a client may set:
    any of ``voice``, ``modalities``, ``vad``, ``system_prompt``. The default allows the
    presentation options (voice, modalities, vad) only. ``system_prompt`` replaces the agent's
    instructions, so a client could rewrite its rules; list it only when every caller is
    trusted to do that. ``model`` and ``tools_tags`` have their own rules and are not listed.

    ``enabled`` mounts ``/v1/graph/ws`` and ``/v1/graph/live``. ``false`` leaves both out, so the
    server needs no WebSocket library. Unset (``None``) keeps them mounted, as before this key
    existed. Serving them needs ``websockets`` or ``wsproto`` (``10xgraph-api[websocket]``):
    ``true`` without one refuses to boot, unset only logs a warning.
    """

    max_connections: int | None
    enabled: bool | None = None
    max_connections_per_user: int | None = DEFAULT_WS_MAX_CONNECTIONS_PER_USER
    realtime_models: tuple[str, ...] = ()
    realtime_client_overrides: tuple[str, ...] = DEFAULT_REALTIME_CLIENT_OVERRIDES

    @classmethod
    def from_dict(cls, data: dict) -> "WebSocketConfig":
        if not isinstance(data, dict):
            raise ValueError("websocket must be an object")
        enabled = data.get("enabled")
        return cls(
            max_connections=_connection_limit(data, "max_connections", DEFAULT_WS_MAX_CONNECTIONS),
            enabled=None if enabled is None else _parse_bool(enabled, field="websocket.enabled"),
            max_connections_per_user=_connection_limit(
                data, "max_connections_per_user", DEFAULT_WS_MAX_CONNECTIONS_PER_USER
            ),
            realtime_models=_string_list(data, "realtime_models"),
            realtime_client_overrides=_realtime_client_overrides(data),
        )


def _realtime_client_overrides(data: dict) -> tuple[str, ...]:
    if "realtime_client_overrides" not in data:
        return DEFAULT_REALTIME_CLIENT_OVERRIDES
    keys = _string_list(data, "realtime_client_overrides")
    unknown = sorted(set(keys) - REALTIME_CLIENT_OVERRIDE_KEYS)
    if unknown:
        raise ValueError(
            f"websocket.realtime_client_overrides has unknown keys {unknown}; allowed: "
            f"{sorted(REALTIME_CLIENT_OVERRIDE_KEYS)}"
        )
    return keys


def _string_list(data: dict, key: str) -> tuple[str, ...]:
    raw = data.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"websocket.{key} must be a list of strings")
    return tuple(item.strip() for item in raw if item.strip())


def _connection_limit(data: dict, key: str, default: int) -> int | None:
    """A missing key gets ``default``; an explicit ``0`` or ``null`` means unlimited."""
    if key not in data:
        return default
    raw = data[key]
    if raw in (None, 0):
        return None
    value = int(raw)
    if value < 0:
        raise ValueError(f"websocket.{key} must be a non-negative integer")
    return value
