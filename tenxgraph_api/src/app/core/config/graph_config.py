import json
import logging
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from tenxgraph.core.graph.remote_tool import RemoteToolConfig

from tenxgraph_api.config_names import CONFIG_FILE, LEGACY_CONFIG_FILE
from tenxgraph_api.import_path import is_import_path
from tenxgraph_api.src.app.core.config.sections.cache_store import (
    _CACHE_STORE_SETTINGS,
    CACHE_STORE_KEYS,
    CACHE_STORE_TYPES,
    DEFAULT_USER_REVOCATION_TTL,
    CacheStoreConfig,
    TokenRevocationConfig,
    _positive_seconds,
    check_cache_store_settings,
    redis_url_or_env,
)
from tenxgraph_api.src.app.core.config.sections.common import (
    _expand_env,
    _parse_bool,
)
from tenxgraph_api.src.app.core.config.sections.misc import (
    _REMOTE_TOOLS_ADAPTER,
    ID_GENERATOR_NAMES,
    THREAD_NAME_CHOICES,
    AgUiConfig,
    MediaConfig,
    validate_remote_tools,
)
from tenxgraph_api.src.app.core.config.sections.rate_limit import (
    DEFAULT_TRUSTED_PROXIES,
    RATE_LIMIT_KEYS,
    RateLimitConfig,
    _parse_networks,
)
from tenxgraph_api.src.app.core.config.sections.runs import (
    DEFAULT_MAX_CONCURRENT_RUNS_PER_USER,
    DEFAULT_PRODUCTION_RUN_TIMEOUT_SECONDS,
    THREAD_ID_MODES,
    RunsConfig,
    _key_allow_list,
    _runs_limit,
    _runs_max_parallel_tool_calls,
    _runs_timeout,
    default_run_timeout,
)
from tenxgraph_api.src.app.core.config.sections.websocket import (
    DEFAULT_REALTIME_CLIENT_OVERRIDES,
    DEFAULT_WS_MAX_CONNECTIONS,
    DEFAULT_WS_MAX_CONNECTIONS_PER_USER,
    REALTIME_CLIENT_OVERRIDE_KEYS,
    WebSocketConfig,
    _connection_limit,
    _realtime_client_overrides,
    _string_list,
)


if TYPE_CHECKING:
    from tenxgraph_api.jobs.config import WorkerConfig
    from tenxgraph_api.src.app.core.audit.config import AuditConfig
    from tenxgraph_api.src.app.core.auth.auth_config import AuthConfig
    from tenxgraph_api.src.app.core.config.metrics import MetricsConfig


logger = logging.getLogger("tenxgraph_api")


def default_config_path() -> str:
    """``10xgraph.json`` in the cwd, or ``agentflow.json`` when only the legacy file exists."""
    if not Path(CONFIG_FILE).exists() and Path(LEGACY_CONFIG_FILE).exists():
        return LEGACY_CONFIG_FILE
    return CONFIG_FILE


# The removed ``checkpointer`` key refuses the boot, like the removed ``authorization`` key.
CHECKPOINTER_KEY_REMOVED = (
    "`checkpointer` was removed from 10xgraph.json (the server never applied it): pass the "
    "checkpointer to graph.compile(checkpointer=...) in your agent module and delete the key."
)


class GraphConfig:
    def __init__(self, path: str | None = None):
        with Path(path or default_config_path()).open() as f:
            self.data: dict = json.load(f)

        # load .env file
        env_file = self.data.get("env")
        if env_file and Path(env_file).exists():
            load_dotenv(env_file)

        if isinstance(self.data, dict):
            check_cache_store_settings(self.data)
            if "checkpointer" in self.data:
                raise ValueError(CHECKPOINTER_KEY_REMOVED)

    @property
    def worker(self) -> "WorkerConfig | None":
        """``worker`` key (the jobs layer), parsed; None when absent."""
        from tenxgraph_api.jobs.config import WorkerConfig

        raw = self.data.get("worker")
        return None if raw is None else WorkerConfig.model_validate(raw)

    @property
    def graph_path(self) -> str:
        agent = self.data.get("agent")
        if agent:
            return agent

        raise ValueError("Agent graph not found")

    @property
    def injectq_path(self) -> str | None:
        return self.data.get("injectq", None)

    @property
    def store_path(self) -> str | None:
        return self.data.get("store", None)

    @property
    def store_allow_tenant_mismatch(self) -> bool:
        """``store_allow_tenant_mismatch`` (default false): the ``store`` key's opt-out of the
        checkpointer/store tenant column check, like ``compile(allow_tenant_mismatch=True)``.
        """
        value = self.data.get("store_allow_tenant_mismatch", False)
        if value is None:
            return False
        return _parse_bool(value, field="store_allow_tenant_mismatch")

    @cached_property
    def cache_store(self) -> CacheStoreConfig:
        """``cache_store`` key, parsed (:class:`CacheStoreConfig`); the memory store when absent.

        Parsed once (the config does not change after load), so a config warning (such as the
        one about ``trusted_proxy_headers``) is not repeated by every caller.
        """
        return CacheStoreConfig.parse(self.data.get("cache_store", None))

    @property
    def thread_name_generator(self) -> str:
        """``thread_name_generator`` key: ``none`` (unset means none: threads stay unnamed but
        are saved), ``random``, ``first_message`` or ``"module:attr"`` naming a
        ``ThreadNameGenerator`` subclass or instance."""
        value = self.data.get("thread_name_generator", None)
        if value is None or value == "":
            return "none"
        if not isinstance(value, str) or (
            value not in THREAD_NAME_CHOICES and not is_import_path(value)
        ):
            raise ValueError(
                f"thread_name_generator must be one of {', '.join(THREAD_NAME_CHOICES)} or "
                f'"module:attr"; got {value!r}'
            )
        return value

    @property
    def id_generator(self) -> str | None:
        """``id_generator`` key: a built-in name (``ID_GENERATOR_NAMES``) or ``"module:attr"``
        naming a ``BaseIDGenerator`` instance, subclass or zero-argument factory; None when
        absent (the graph keeps the generator it was compiled with)."""
        value = self.data.get("id_generator", None)
        if value is None or value == "":
            return None
        if not isinstance(value, str) or (
            value not in ID_GENERATOR_NAMES and not is_import_path(value)
        ):
            raise ValueError(
                f"id_generator must be one of {', '.join(ID_GENERATOR_NAMES)} or "
                f'"module:attr"; got {value!r}'
            )
        return value

    @property
    def remote_tools(self) -> list[RemoteToolConfig]:
        """Validated client-executed tool schemas configured at startup."""
        return validate_remote_tools(self.data.get("remote_tools", []))

    @property
    def observability(self) -> dict | None:
        """The declarative ``observability`` block (Logfire / LangSmith / Phoenix).

        Shape mirrors ``tenxgraph.runtime.publisher.setup_observability``::

            {
              "level": "full",
              "logfire":   {"enabled": true, "service_name": "my-agent", ...},
              "langsmith": {"enabled": true, "project": "my-agent", "endpoint": null},
              "phoenix":   {"enabled": true, "project": "my-agent", "endpoint": null}
            }

        Secrets (``LOGFIRE_TOKEN``, ``LANGSMITH_API_KEY``, ``PHOENIX_API_KEY``) stay in the
        environment and are never read from here.
        """
        return self.data.get("observability", None)

    @cached_property
    def _auth_parsed(self) -> "AuthConfig":
        from tenxgraph_api.src.app.core.auth.auth_config import AuthConfig

        return AuthConfig.parse(self.data.get("auth", None))

    def auth_config(self) -> "AuthConfig | None":
        """The parsed ``auth`` setting; None when auth is off. Called on every request, so it
        is parsed once (the config does not change after load).

        Only parses; ``check_auth_config`` reports rule violations at boot.
        """
        parsed = self._auth_parsed
        return None if parsed.method == "none" else parsed

    @property
    def rate_limit(self) -> RateLimitConfig | None:
        """``cache_store.rate_limit`` when present and enabled, else None."""
        config = self.cache_store.rate_limit
        if config is None or not config.enabled:
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
    def token_revocation(self) -> TokenRevocationConfig | None:
        """``cache_store.token_revocation``: its settings when on (``true`` or an object),
        else None. See :class:`TokenRevocationConfig`."""
        return self.cache_store.token_revocation

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
    def metrics(self) -> "MetricsConfig":
        """The ``metrics`` block (OTLP push or Prometheus scrape); off when absent."""
        from tenxgraph_api.src.app.core.config.metrics import MetricsConfig

        return MetricsConfig.model_validate(self.data.get("metrics") or {})

    @property
    def audit(self) -> "AuditConfig":
        """The ``audit`` block (access, auth and change events); off when absent. Unknown keys
        are a validation error, so a typo refuses to boot."""
        from tenxgraph_api.src.app.core.audit.config import AuditConfig

        return AuditConfig.model_validate(self.data.get("audit") or {})

    @property
    def media(self) -> MediaConfig:
        """Uploaded-file lifecycle settings (``media`` key); defaults when absent."""
        data = self.data.get("media", None)
        return MediaConfig.from_dict({} if data is None else data)


__all__ = [
    "CACHE_STORE_KEYS",
    "CACHE_STORE_TYPES",
    "DEFAULT_MAX_CONCURRENT_RUNS_PER_USER",
    "DEFAULT_PRODUCTION_RUN_TIMEOUT_SECONDS",
    "DEFAULT_REALTIME_CLIENT_OVERRIDES",
    "DEFAULT_TRUSTED_PROXIES",
    "DEFAULT_USER_REVOCATION_TTL",
    "DEFAULT_WS_MAX_CONNECTIONS",
    "DEFAULT_WS_MAX_CONNECTIONS_PER_USER",
    "ID_GENERATOR_NAMES",
    "RATE_LIMIT_KEYS",
    "REALTIME_CLIENT_OVERRIDE_KEYS",
    "THREAD_ID_MODES",
    "THREAD_NAME_CHOICES",
    "_CACHE_STORE_SETTINGS",
    "_REMOTE_TOOLS_ADAPTER",
    "AgUiConfig",
    "CacheStoreConfig",
    "GraphConfig",
    "MediaConfig",
    "RateLimitConfig",
    "RunsConfig",
    "TokenRevocationConfig",
    "WebSocketConfig",
    "_connection_limit",
    "_expand_env",
    "_key_allow_list",
    "_parse_bool",
    "_parse_networks",
    "_positive_seconds",
    "_realtime_client_overrides",
    "_runs_limit",
    "_runs_max_parallel_tool_calls",
    "_runs_timeout",
    "_string_list",
    "check_cache_store_settings",
    "default_config_path",
    "default_run_timeout",
    "redis_url_or_env",
    "validate_remote_tools",
]
