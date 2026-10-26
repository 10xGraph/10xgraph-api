import ipaddress
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator


logger = logging.getLogger("tenxgraph_api")


def _empty_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


class RemoteToolConfig(BaseModel):
    """Trusted model-facing schema for a tool executed by the client."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    node_name: str = Field(alias="node", min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=_empty_parameters)

    @field_validator("node_name", "name", "description")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        parameters = dict(value)
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        parameters.setdefault("required", [])
        if parameters["type"] != "object":
            raise ValueError("remote tool parameters.type must be 'object'")
        if not isinstance(parameters["properties"], dict):
            raise ValueError("remote tool parameters.properties must be an object")
        if not isinstance(parameters["required"], list) or not all(
            isinstance(item, str) for item in parameters["required"]
        ):
            raise ValueError("remote tool parameters.required must be a list of strings")
        return parameters

    def to_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_REMOTE_TOOLS_ADAPTER = TypeAdapter(list[RemoteToolConfig])


def validate_remote_tools(raw: object) -> list[RemoteToolConfig]:
    """Validate remote-tool declarations for both CLI audit and API startup."""
    if not isinstance(raw, list):
        raise ValueError("remote_tools must be a list")

    try:
        tools = _REMOTE_TOOLS_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid remote_tools configuration: {exc}") from exc

    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise ValueError(f"Duplicate remote tool name '{tool.name}'")
        seen.add(tool.name)
    return tools


def _parse_bool(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


def _expand_env(value: str | None) -> str | None:
    if value is None:
        return None
    expanded = os.path.expandvars(value)
    if expanded == value and (value.startswith("$") or "${" in value):
        raise ValueError(f"Unresolved environment variable in value: {value}")
    return expanded


# Where X-Forwarded-For is trusted from when ``trusted_proxy_headers`` is on but no
# ``trusted_proxies`` are listed: loopback and private networks, which is where a reverse proxy,
# ingress or sidecar in front of the app connects from. A client on the public internet that
# reaches the app directly is keyed by its own address. List ``0.0.0.0/0`` and ``::/0`` to
# trust every peer (only when nothing can reach the app except through your proxy).
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)


def _parse_networks(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        raise ValueError("rate_limit.trusted_proxies must be a list of IPs or CIDR ranges")
    networks = []
    for item in raw:
        try:
            networks.append(str(ipaddress.ip_network(str(item).strip(), strict=False)))
        except ValueError as exc:
            raise ValueError(f"rate_limit.trusted_proxies: invalid network {item!r}") from exc
    return tuple(networks)


@dataclass
class RateLimitConfig:
    """Rate limit configuration parsed from 10xgraph.json.

    Example (memory backend, default)::

        "rate_limit": {
            "enabled": true,
            "backend": "memory",
            "requests": 100,
            "window": 60,
            "by": "ip",
            "trusted_proxy_headers": false,
            "exclude_paths": ["/ping", "/docs", "/redoc", "/openapi.json"]
        }

    Example (Redis backend)::

        "rate_limit": {
            "enabled": true,
            "backend": "redis",
            "requests": 100,
            "window": 60,
            "by": "ip",
            "trusted_proxy_headers": true,
            "exclude_paths": ["/ping"],
            "redis": {
                "url": "redis://localhost:6379/0",
                "prefix": "10xgraph:rate-limit"
            },
            "fail_open": true
        }

    Example (custom backend)::

        "rate_limit": {
            "enabled": true,
            "backend": "custom",
            "requests": 100,
            "window": 60,
            "by": "ip"
        }

    For custom backends, bind a ``BaseRateLimitBackend`` instance in InjectQ.
    """

    enabled: bool
    requests: int
    window: int
    by: str  # "ip" | "user" | "global"
    backend: str  # "memory" | "redis" | "custom"
    redis_url: str | None
    redis_prefix: str
    exclude_paths: tuple[str, ...]
    trusted_proxy_headers: bool  # honour X-Forwarded-For only when True
    # Number of proxies you control in front of the app. Only this many entries,
    # counted from the RIGHT of X-Forwarded-For, were appended by your own
    # infrastructure; anything further left came from the caller and is forgeable.
    trusted_proxy_hops: int = 1
    fail_open: bool = True  # on backend error: True=allow, False=deny
    # Networks your proxies connect from. X-Forwarded-For is honoured only for requests
    # whose peer address is in one of them, so a client that reaches the app directly
    # (bypassing the proxy) cannot pick its own address. Empty means
    # DEFAULT_TRUSTED_PROXIES (loopback and private networks), never "everyone".
    trusted_proxies: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "RateLimitConfig":
        if not isinstance(data, dict):
            raise ValueError("rate_limit must be an object")

        enabled = _parse_bool(data.get("enabled", True), field="rate_limit.enabled")
        requests = int(data.get("requests", 100))
        window = int(data.get("window", 60))
        by = data.get("by", "ip")
        backend = data.get("backend", "memory")
        trusted_proxy_headers = _parse_bool(
            data.get("trusted_proxy_headers", False),
            field="rate_limit.trusted_proxy_headers",
        )
        exclude_paths_raw = data.get("exclude_paths", [])
        if not isinstance(exclude_paths_raw, list | tuple):
            raise ValueError("rate_limit.exclude_paths must be a list of paths")
        exclude_paths = tuple(str(path) for path in exclude_paths_raw)
        fail_open = _parse_bool(data.get("fail_open", True), field="rate_limit.fail_open")

        # Redis sub-object: {"url": "...", "prefix": "..."}
        redis_obj = data.get("redis") or {}
        if isinstance(redis_obj, str):
            # Allow shorthand: "redis": "redis://..."
            redis_url: str | None = _expand_env(redis_obj)
            redis_prefix = "10xgraph:rate-limit"
        elif isinstance(redis_obj, dict):
            redis_url = _expand_env(redis_obj.get("url") or None)
            redis_prefix = str(redis_obj.get("prefix", "10xgraph:rate-limit"))
        else:
            raise ValueError("rate_limit.redis must be an object or Redis URL string")

        # How many proxies of YOUR OWN sit in front of the app and append to
        # X-Forwarded-For. Only that many entries, counted from the right, were
        # written by infrastructure you control; everything to the left of them came
        # from the caller and is forgeable. See keying._client_ip.
        trusted_proxy_hops = int(data.get("trusted_proxy_hops", 1))
        trusted_proxies = _parse_networks(data.get("trusted_proxies", []))

        # Validation
        if by not in ("ip", "global", "user"):
            raise ValueError(f"rate_limit.by must be 'ip', 'user', or 'global', got '{by}'")
        if backend not in ("memory", "redis", "custom"):
            raise ValueError(
                f"rate_limit.backend must be 'memory', 'redis', or 'custom', got '{backend}'"
            )
        if requests <= 0:
            raise ValueError("rate_limit.requests must be a positive integer")
        if window <= 0:
            raise ValueError("rate_limit.window must be a positive integer")
        if trusted_proxy_hops < 1:
            raise ValueError("rate_limit.trusted_proxy_hops must be >= 1")
        if enabled and trusted_proxy_headers and not trusted_proxies:
            logger.warning(
                "rate_limit.trusted_proxy_headers is on without rate_limit.trusted_proxies; "
                "X-Forwarded-For is trusted only from loopback and private networks. List "
                "your proxy's addresses in trusted_proxies (or 0.0.0.0/0 and ::/0 if nothing "
                "can reach the app except through it)."
            )

        # The memory backend counts per PROCESS. Under gunicorn with N workers the
        # effective limit therefore becomes requests x N, and it resets whenever a
        # worker restarts -- so it does not really limit anything in production.
        if enabled and backend == "memory":
            logger.warning(
                "Rate limiting uses the in-memory backend. It counts per process, so "
                "with N workers the real limit is requests x N and it resets on every "
                "worker restart. Use the 'redis' backend for any multi-worker deployment."
            )

        return cls(
            enabled=enabled,
            requests=requests,
            window=window,
            by=by,
            backend=backend,
            redis_url=redis_url,
            redis_prefix=redis_prefix,
            exclude_paths=exclude_paths,
            trusted_proxy_headers=trusted_proxy_headers,
            trusted_proxy_hops=trusted_proxy_hops,
            fail_open=fail_open,
            trusted_proxies=trusted_proxies,
        )


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
            "realtime_models": ["gemini-2.5-flash-live"],
            "realtime_client_overrides": ["voice", "modalities", "vad"]
        }

    ``max_connections`` caps the concurrent WebSocket connections this server *process*
    accepts (realtime ``/v1/graph/live`` + streaming ``/v1/graph/ws``).
    ``max_connections_per_user`` caps how many of those one verified user may hold, so a
    single account cannot take every slot. Both are per process, like the in-memory
    rate-limit backend; size them per worker.

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


# Finite default so one account cannot hold an unbounded number of HTTP graph runs open.
DEFAULT_MAX_CONCURRENT_RUNS_PER_USER = 10
THREAD_ID_MODES = ("client", "server")
GRAPH_INTROSPECTION_MODES = ("open", "scoped")


@dataclass
class RunsConfig:
    """Per-run policy for graph runs (``runs`` key in 10xgraph.json).

    Example::

        "runs": {
            "thread_ids": "server",
            "config_keys": ["locale", "tenant_hint"],
            "state_keys": ["preferences"],
            "max_concurrent_per_user": 10,
            "timeout": 600,
            "max_parallel_tool_calls": 10
        }

    ``thread_ids`` decides who may name a new thread. ``"client"`` (the default) lets a
    request start a thread under any id it picks. ``"server"`` accepts only ids of threads
    that already exist; a new thread is started by omitting the id (or sending ``"new"`` over
    WebSocket), and the server picks it. Use ``"server"`` when ids could be guessed: under
    owner-only authorization, the first writer of an id owns it, so a user who claims an id
    first locks its intended owner out. Clients that generate their own ids (the realtime
    client, AG-UI/CopilotKit) need ``"client"``.

    ``config_keys`` and ``state_keys`` are allow-lists for client input. When set, a request
    whose ``config`` or ``initial_state`` carries any other key is rejected with 422.
    ``thread_id`` is always allowed in ``config``. ``null`` (the default) accepts any key
    except the server-owned ones. Set them when a graph tool or node trusts a value such as
    ``role`` or ``plan`` from state or config: without an allow-list a client can send it.

    ``max_concurrent_per_user`` caps the HTTP graph runs (invoke, stream, AG-UI) one verified
    user may have in flight at once in this process; over the cap the request gets 429.
    Default 10; ``0`` or ``null`` means unlimited. Requests without a verified user (auth off)
    are not capped. ``timeout`` is the longest a single graph run may take, in seconds; a run
    that passes it is cancelled (invoke answers 504; a stream, ``/v1/graph/ws`` run or AG-UI
    run ends with an error). Realtime sessions are not limited. Default ``null``: no limit.

    ``max_parallel_tool_calls`` caps the tool calls one model turn may run at once. The API
    puts it into every run's config (clients cannot set it). Default ``null``: core decides,
    from the ``TENXGRAPH_MAX_PARALLEL_TOOL_CALLS`` environment variable, else 10.
    """

    thread_ids: str = "client"
    config_keys: frozenset[str] | None = None
    state_keys: frozenset[str] | None = None
    max_concurrent_per_user: int | None = DEFAULT_MAX_CONCURRENT_RUNS_PER_USER
    timeout: float | None = None
    max_parallel_tool_calls: int | None = None

    @property
    def server_thread_ids(self) -> bool:
        return self.thread_ids == "server"

    @classmethod
    def from_dict(cls, data: dict) -> "RunsConfig":
        if not isinstance(data, dict):
            raise ValueError("runs must be an object")
        thread_ids = data.get("thread_ids", "client")
        if thread_ids not in THREAD_ID_MODES:
            raise ValueError(f"runs.thread_ids must be one of {list(THREAD_ID_MODES)}")
        return cls(
            thread_ids=thread_ids,
            config_keys=_key_allow_list(data, "config_keys"),
            state_keys=_key_allow_list(data, "state_keys"),
            max_concurrent_per_user=_runs_limit(data),
            timeout=_runs_timeout(data),
            max_parallel_tool_calls=_runs_max_parallel_tool_calls(data),
        )


def _key_allow_list(data: dict, key: str) -> frozenset[str] | None:
    raw = data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"runs.{key} must be a list of strings or null")
    return frozenset(item.strip() for item in raw if item.strip())


def _runs_limit(data: dict) -> int | None:
    key = "max_concurrent_per_user"
    if key not in data:
        return DEFAULT_MAX_CONCURRENT_RUNS_PER_USER
    raw = data[key]
    if raw in (None, 0):
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"runs.{key} must be a non-negative integer or null")
    return raw


def _runs_timeout(data: dict) -> float | None:
    raw = data.get("timeout")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        raise ValueError("runs.timeout must be a positive number of seconds or null")
    return float(raw)


def _runs_max_parallel_tool_calls(data: dict) -> int | None:
    raw = data.get("max_parallel_tool_calls")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError("runs.max_parallel_tool_calls must be a positive integer or null")
    return raw


@dataclass
class AgUiConfig:
    """AG-UI protocol endpoint settings parsed from 10xgraph.json.

    Example::

        "ag_ui": {
            "enabled": true,
            "allow_client_tools": true
        }

    When enabled, ``POST /v1/ag-ui`` streams the graph as AG-UI events, so AG-UI clients
    such as CopilotKit can use the agent. It needs the ``ag-ui`` extra
    (``pip install "10xgraph-api[ag-ui]"``). Off unless explicitly enabled.

    ``allow_client_tools`` (default ``true``) offers the tools an AG-UI client sends with each
    run to the model for that run. They run in the caller's own browser, cannot take the name of
    a server tool, and must pass size limits. Set it to ``false`` to accept only the client tools
    declared under ``remote_tools``.
    """

    enabled: bool = False
    allow_client_tools: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "AgUiConfig":
        if not isinstance(data, dict):
            raise ValueError("ag_ui must be an object")
        return cls(
            enabled=_parse_bool(data.get("enabled", False), field="ag_ui.enabled"),
            allow_client_tools=_parse_bool(
                data.get("allow_client_tools", True), field="ag_ui.allow_client_tools"
            ),
        )


@dataclass
class MediaConfig:
    """Uploaded-file lifecycle settings parsed from 10xgraph.json.

    Example::

        "media": {"delete_with_thread": true}

    The API records which threads use an uploaded file (``graph://media/`` references in
    accepted messages). With ``delete_with_thread`` (default ``true``), deleting a message or a
    thread drops that link, and a file with no thread left is deleted too. Set it to ``false``
    to keep the links but never delete automatically. Files uploaded before link tracking
    existed are never deleted automatically either way.
    """

    delete_with_thread: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "MediaConfig":
        if not isinstance(data, dict):
            raise ValueError("media must be an object")
        return cls(
            delete_with_thread=_parse_bool(
                data.get("delete_with_thread", True), field="media.delete_with_thread"
            ),
        )


CONFIG_FILE = "10xgraph.json"
# Pre-rename name, read only when no 10xgraph.json exists. Removed in 2.0.
LEGACY_CONFIG_FILE = "agentflow.json"


def default_config_path() -> str:
    """``10xgraph.json`` in the cwd, or ``agentflow.json`` when only the legacy file exists."""
    if not Path(CONFIG_FILE).exists() and Path(LEGACY_CONFIG_FILE).exists():
        return LEGACY_CONFIG_FILE
    return CONFIG_FILE


class GraphConfig:
    def __init__(self, path: str | None = None):
        with Path(path or default_config_path()).open() as f:
            self.data: dict = json.load(f)

        # load .env file
        env_file = self.data.get("env")
        if env_file and Path(env_file).exists():
            load_dotenv(env_file)

    @property
    def graph_path(self) -> str:
        agent = self.data.get("agent")
        if agent:
            return agent

        raise ValueError("Agent graph not found")

    @property
    def checkpointer_path(self) -> str | None:
        return self.data.get("checkpointer", None)

    @property
    def injectq_path(self) -> str | None:
        return self.data.get("injectq", None)

    @property
    def store_path(self) -> str | None:
        return self.data.get("store", None)

    @property
    def redis_url(self) -> str | None:
        return self.data.get("redis", None)

    @property
    def thread_name_generator_path(self) -> str | None:
        return self.data.get("thread_name_generator", None)

    @property
    def remote_tools(self) -> list[RemoteToolConfig]:
        """Validated client-executed tool schemas configured at startup."""
        return validate_remote_tools(self.data.get("remote_tools", []))

    @property
    def observability(self) -> dict | None:
        """The declarative ``observability`` block (Logfire / LangSmith).

        Shape mirrors ``tenxgraph.runtime.publisher.setup_observability``::

            {
              "level": "standard",
              "logfire":   {"enabled": true, "service_name": "my-agent", ...},
              "langsmith": {"enabled": true, "project": "my-agent", "endpoint": null}
            }

        Secrets (``LOGFIRE_TOKEN``, ``LANGSMITH_API_KEY``) stay in the
        environment and are never read from here.
        """
        return self.data.get("observability", None)

    @property
    def authorization_path(self) -> str | None:
        """
        Get the authorization backend selector from configuration.

        Accepts, in order of precedence:
        - ``"module:attribute"`` -- a custom ``AuthorizationBackend`` to load.
        - a built-in name: ``"ownership"`` (owner-only thread access) or
          ``"allow_all"``/``"default"``/``"none"`` (any authenticated user).
        - ``None`` (not configured) -- defaults by run mode: ``ownership`` in
          production, ``allow_all`` in development.

        Returns:
            str | None: The configured selector, or None to use the mode default.
        """
        return self.data.get("authorization", None)

    def auth_config(self) -> dict | None:
        """The parsed ``auth`` setting. Called on every request, so the result is cached.

        The cache is keyed by the raw ``auth`` value, so a changed config is parsed again. The
        JWT environment check therefore runs once, not per request.
        """
        res = self.data.get("auth", None)
        key = json.dumps(res, sort_keys=True, default=str)
        cached = getattr(self, "_auth_config_cache", None)
        if cached is not None and cached[0] == key:
            return dict(cached[1]) if cached[1] is not None else None
        parsed = self._parse_auth_config(res)
        self._auth_config_cache = (key, parsed)
        return dict(parsed) if parsed is not None else None

    @staticmethod
    def _parse_auth_config(res: Any) -> dict | None:
        if not res:
            return None

        if isinstance(res, str) and "jwt" in res:
            # Now check jwt secret and algorithm available in env
            secret = os.environ.get("JWT_SECRET_KEY", None)
            algorithm = os.environ.get("JWT_ALGORITHM", None)
            if not secret or not algorithm:
                raise ValueError(
                    "JWT_SECRET_KEY and JWT_ALGORITHM must be set in environment variables",
                )
            return {
                "method": "jwt",
            }

        if isinstance(res, dict):
            method = res.get("method", None)
            path: str | None = res.get("path", None)
            if not path or not method:
                raise ValueError("Both method and path must be provided in auth config")

            if method == "custom" and path:
                return {
                    "method": "custom",
                    "path": path,
                }

        raise ValueError(f"Unsupported auth method: {res}")

    @property
    def rate_limit(self) -> RateLimitConfig | None:
        """
        Get rate limit configuration from 10xgraph.json.

        Returns:
            RateLimitConfig if 'rate_limit' key is present and enabled, else None.

        Example 10xgraph.json entry::

            "rate_limit": {
                "enabled": true,
                "requests": 100,
                "window": 60,
                "by": "ip"
            }
        """
        data = self.data.get("rate_limit", None)
        if data is None:
            return None
        config = RateLimitConfig.from_dict(data)
        if not config.enabled:
            return None
        return config

    @property
    def websocket(self) -> "WebSocketConfig":
        """WebSocket connection limits from 10xgraph.json (``websocket`` key).

        Returns the default limits when the key is absent.
        """
        data = self.data.get("websocket", None)
        return WebSocketConfig.from_dict({} if data is None else data)

    @property
    def graph_introspection(self) -> str:
        """Who may read the graph's structure, tool schemas and state schema.

        ``"open"``: any authenticated user. ``"scoped"``: only identities that carry the
        ``graph:read`` scope (from JWT claims or an RBAC role); an identity with no scopes is
        refused. Defaults to ``"scoped"`` in production and ``"open"`` in development. With
        auth off there is no identity, so these routes stay open either way.
        """
        value = self.data.get("graph_introspection", None)
        if value is None:
            from tenxgraph_api.src.app.core.config.settings import get_settings

            return "scoped" if get_settings().MODE == "production" else "open"
        if value not in GRAPH_INTROSPECTION_MODES:
            raise ValueError(
                f"graph_introspection must be one of {list(GRAPH_INTROSPECTION_MODES)}"
            )
        return value

    @property
    def introspection_scoped(self) -> bool:
        return self.graph_introspection == "scoped"

    @property
    def token_revocation(self) -> str | None:
        """``token_revocation`` key: ``"redis"``, ``"memory"``, ``"module:attr"`` or None."""
        value = self.data.get("token_revocation", None)
        if value is not None and not isinstance(value, str):
            raise ValueError("token_revocation must be 'redis', 'memory', 'module:attr' or null")
        return value or None

    @property
    def runs(self) -> RunsConfig:
        """Per-run policy from 10xgraph.json (``runs`` key); defaults when absent."""
        data = self.data.get("runs", None)
        if data is None:
            return RunsConfig()
        return RunsConfig.from_dict(data)

    @property
    def ag_ui(self) -> AgUiConfig:
        """AG-UI endpoint settings from 10xgraph.json (``ag_ui`` key).

        Returns a disabled config when the key is absent.
        """
        data = self.data.get("ag_ui", None)
        return AgUiConfig.from_dict({} if data is None else data)

    @property
    def media(self) -> MediaConfig:
        """Uploaded-file lifecycle settings (``media`` key); defaults when absent."""
        data = self.data.get("media", None)
        return MediaConfig.from_dict({} if data is None else data)
