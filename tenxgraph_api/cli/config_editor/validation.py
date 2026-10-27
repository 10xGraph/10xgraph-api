"""Validate an ``10xgraph.json`` document the same way the CLI and API server read it.

Errors block a save because the server would refuse to start (or silently misbehave).
Warnings describe things that are valid but probably not what the user meant, such as
an import path whose module file cannot be found next to the config.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.config_editor.schema import (
    BUILTIN_AUTHORIZATION,
    KNOWN_TOP_LEVEL_KEYS,
    section_for_key,
)


Issue = dict[str, str]

_IMPORT_PATH = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
_OBSERVABILITY_LEVELS = ("spans", "standard", "full")
_USER_ID_MODES = ("raw", "hash", "mask")
_MAX_PERCENT = 100


class _Collector:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def add(self, level: str, path: str, message: str) -> None:
        top = path.split(".", 1)[0].split("[", 1)[0]
        self.issues.append(
            {
                "level": level,
                "section": section_for_key(top) if path else "core",
                "path": path,
                "message": message,
            }
        )

    def error(self, path: str, message: str) -> None:
        self.add("error", path, message)

    def warning(self, path: str, message: str) -> None:
        self.add("warning", path, message)


def validate_config(config: Any, base_dir: Path | None = None) -> list[Issue]:
    """Return every error and warning for ``config``.

    Args:
        config: Parsed ``10xgraph.json`` content.
        base_dir: Directory that holds the config file. Enables checks that
            referenced modules and files exist.
    """
    out = _Collector()
    if not isinstance(config, dict):
        out.error("", "The configuration must be a JSON object.")
        return out.issues

    _check_agent(config, out, base_dir)
    _check_env(config, out, base_dir)
    _check_auth(config, out, base_dir)
    _check_authorization(config, out, base_dir)
    _check_token_revocation(config, out, base_dir)
    _check_graph_introspection(config, out)
    _check_rate_limit(config, out)
    _check_websocket(config, out)
    _check_runs(config, out)
    _check_ag_ui(config, out)
    _check_media(config, out)
    _check_observability(config, out)
    _check_plugins(config, out, base_dir)
    _check_remote_tools(config, out)
    _check_test(config, out)
    _check_evaluation(config, out)

    for key in config:
        if key not in KNOWN_TOP_LEVEL_KEYS:
            out.warning(key, f"'{key}' is not read by 10xGraph. It is kept as-is.")
    return out.issues


def has_errors(issues: list[Issue]) -> bool:
    return any(issue["level"] == "error" for issue in issues)


# ---------------------------------------------------------------------------
# Individual sections
# ---------------------------------------------------------------------------


def _check_agent(config: dict, out: _Collector, base_dir: Path | None) -> None:
    agent = config.get("agent")
    if agent is None or agent == "":
        out.error("agent", "agent is required, for example 'graph.react:app'.")
        return
    _check_import_path("agent", agent, out, base_dir)


def _check_env(config: dict, out: _Collector, base_dir: Path | None) -> None:
    env = config.get("env")
    if env is None:
        return
    if not isinstance(env, str):
        out.error("env", "env must be a file path string or null.")
        return
    if env and base_dir is not None and not (base_dir / env).exists():
        out.warning("env", f"Env file '{env}' does not exist yet.")


def _check_auth(config: dict, out: _Collector, base_dir: Path | None) -> None:
    auth = config.get("auth")
    if not auth:
        return
    if isinstance(auth, str):
        if "jwt" not in auth:
            out.error("auth", f"Unsupported auth '{auth}'. Use 'jwt' or a custom object.")
        return
    if isinstance(auth, dict):
        method, path = auth.get("method"), auth.get("path")
        if method != "custom":
            out.error("auth.method", 'auth.method must be \'custom\'. For JWT use "auth": "jwt".')
        if not path:
            out.error("auth.path", "auth.path is required for custom auth.")
        else:
            _check_import_path("auth.path", path, out, base_dir)
        return
    out.error("auth", 'auth must be null, \'jwt\', or {"method": "custom", "path": ...}.')


def _check_authorization(config: dict, out: _Collector, base_dir: Path | None) -> None:
    value = config.get("authorization")
    if value is None:
        return
    if isinstance(value, str):
        if ":" in value:
            _check_import_path("authorization", value, out, base_dir)
        elif value.strip().lower() not in BUILTIN_AUTHORIZATION:
            out.error(
                "authorization",
                f"Unknown authorization '{value}'. Use one of "
                f"{', '.join(BUILTIN_AUTHORIZATION)} or a module:attribute path.",
            )
        return
    if not isinstance(value, dict):
        out.error("authorization", "authorization must be a string, an RBAC object, or null.")
        return
    _check_rbac(value, out)


def _check_rbac(value: dict, out: _Collector) -> None:
    backend = str(value.get("backend") or value.get("type") or "").lower()
    if backend not in ("rbac", "role_based", "roles"):
        out.error("authorization.backend", "authorization.backend must be 'rbac'.")
    roles = value.get("roles") if "roles" in value else value.get("role_scopes")
    if roles is not None:
        if not isinstance(roles, dict):
            out.error("authorization.roles", "roles must map role names to scope lists.")
        else:
            for role, scopes in roles.items():
                if not _is_str_list(scopes):
                    out.error(f"authorization.roles.{role}", "Scopes must be a list of strings.")
    if not roles:
        out.warning("authorization.roles", "No roles defined. Only default scopes apply.")
    default_scopes = value.get("default_scopes")
    if default_scopes is not None and not _is_str_list(default_scopes):
        out.error("authorization.default_scopes", "default_scopes must be a list of strings.")
    isolation = value.get("isolation", "owner")
    if isolation not in ("owner", "none"):
        out.error("authorization.isolation", "isolation must be 'owner' or 'none'.")


def _check_rate_limit(config: dict, out: _Collector) -> None:
    data = config.get("rate_limit")
    if data is None:
        return
    if not isinstance(data, dict):
        out.error("rate_limit", "rate_limit must be an object.")
        return

    from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig

    probe = dict(data)
    redis_value = probe.get("redis")
    redis_url = redis_value.get("url") if isinstance(redis_value, dict) else redis_value
    if isinstance(redis_url, str) and _has_unresolved_env(redis_url):
        # The server expands $VAR at startup; it is fine for it to be unset while editing.
        out.warning(
            "rate_limit.redis.url",
            f"{redis_url} is not set in this shell. Make sure it is set when the API runs.",
        )
        placeholder = "redis://placeholder"
        probe["redis"] = (
            {**redis_value, "url": placeholder} if isinstance(redis_value, dict) else placeholder
        )

    config_obj = _quietly(lambda: RateLimitConfig.from_dict(probe), out, "rate_limit")
    if config_obj is None:
        return
    if config_obj.backend == "redis" and not redis_url:
        out.warning(
            "rate_limit.redis.url",
            "The redis backend needs a URL unless a Redis client is bound in InjectQ.",
        )
    if config_obj.enabled and config_obj.backend == "memory":
        out.warning(
            "rate_limit.backend",
            "The memory backend counts per process. Use redis with more than one worker.",
        )


def _check_websocket(config: dict, out: _Collector) -> None:
    data = config.get("websocket")
    if data is None:
        return
    from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig

    _quietly(lambda: WebSocketConfig.from_dict(data), out, "websocket")


def _check_token_revocation(config: dict, out: _Collector, base_dir: Path | None) -> None:
    value = config.get("token_revocation")
    if value is None or value in ("", "redis", "memory"):
        if value == "redis" and not (config.get("redis") or os.environ.get("REDIS_URL")):
            out.warning("token_revocation", "redis needs the redis key or REDIS_URL.")
        if value and not config.get("auth"):
            out.warning("token_revocation", "Auth is off, so there are no tokens to revoke.")
        return
    if not isinstance(value, str):
        out.error("token_revocation", "token_revocation must be redis, memory or module:attr.")
        return
    _check_import_path("token_revocation", value, out, base_dir)


def _check_graph_introspection(config: dict, out: _Collector) -> None:
    value = config.get("graph_introspection")
    if value is not None and value not in ("open", "scoped"):
        out.error("graph_introspection", "graph_introspection must be open or scoped.")


def _check_runs(config: dict, out: _Collector) -> None:
    data = config.get("runs")
    if data is None:
        return
    from tenxgraph_api.src.app.core.config.graph_config import RunsConfig

    _quietly(lambda: RunsConfig.from_dict(data), out, "runs")


def _check_ag_ui(config: dict, out: _Collector) -> None:
    data = config.get("ag_ui")
    if data is None:
        return
    from tenxgraph_api.src.app.core.config.graph_config import AgUiConfig

    _quietly(lambda: AgUiConfig.from_dict(data), out, "ag_ui")


def _check_media(config: dict, out: _Collector) -> None:
    data = config.get("media")
    if data is None:
        return
    from tenxgraph_api.src.app.core.config.graph_config import MediaConfig

    _quietly(lambda: MediaConfig.from_dict(data), out, "media")


def _check_observability(config: dict, out: _Collector) -> None:
    data = config.get("observability")
    if data is None:
        return
    if not isinstance(data, dict):
        out.error("observability", "observability must be an object.")
        return
    level = data.get("level", "standard")
    if level not in _OBSERVABILITY_LEVELS:
        out.error(
            "observability.level", f"level must be one of {', '.join(_OBSERVABILITY_LEVELS)}."
        )
    user_id_mode = data.get("user_id_mode", "raw")
    if user_id_mode not in _USER_ID_MODES:
        out.error(
            "observability.user_id_mode",
            f"user_id_mode must be one of {', '.join(_USER_ID_MODES)}.",
        )
    any_enabled = False
    for backend in ("logfire", "langsmith"):
        block = data.get(backend)
        if block is None:
            continue
        if not isinstance(block, dict):
            out.error(f"observability.{backend}", f"{backend} must be an object.")
            continue
        enabled = block.get("enabled", False)
        if not isinstance(enabled, bool):
            out.error(f"observability.{backend}.enabled", "enabled must be true or false.")
        any_enabled = any_enabled or enabled is True
    if not any_enabled:
        out.warning(
            "observability", "Neither Logfire nor LangSmith is enabled, so nothing is traced."
        )


def _check_plugins(config: dict, out: _Collector, base_dir: Path | None) -> None:
    for key in ("injectq", "store", "checkpointer", "thread_name_generator"):
        value = config.get(key)
        if value is None or value == "":
            continue
        _check_import_path(key, value, out, base_dir)
    if config.get("checkpointer"):
        out.warning(
            "checkpointer",
            "The API server does not apply this key yet. Pass the checkpointer to "
            "graph.compile() instead.",
        )

    redis = config.get("redis")
    if redis is None or redis == "":
        return
    if not isinstance(redis, str):
        out.error("redis", "redis must be a URL string.")
    elif "$" in redis:
        out.warning("redis", "Top-level redis is used as-is; $VAR is not expanded.")


def _check_remote_tools(config: dict, out: _Collector) -> None:
    if "remote_tools" not in config or config["remote_tools"] is None:
        return
    from tenxgraph_api.src.app.core.config.graph_config import validate_remote_tools

    _quietly(lambda: validate_remote_tools(config["remote_tools"]), out, "remote_tools")


def _check_test(config: dict, out: _Collector) -> None:
    data = config.get("test")
    if data is None:
        return
    if not isinstance(data, dict):
        out.error("test", "test must be an object.")
        return
    if "path" in data and not isinstance(data["path"], str):
        out.error("test.path", "path must be a string.")
    if "coverage" in data and not isinstance(data["coverage"], bool):
        out.error("test.coverage", "coverage must be true or false.")
    threshold = data.get("coverage_threshold")
    if threshold is not None and not (_is_int(threshold) and 0 <= threshold <= _MAX_PERCENT):
        out.error("test.coverage_threshold", "coverage_threshold must be a whole number 0-100.")


def _check_evaluation(config: dict, out: _Collector) -> None:
    data = config.get("evaluation")
    if data is None:
        return
    if not isinstance(data, dict):
        out.error("evaluation", "evaluation must be an object.")
        return
    for key in ("directory", "output_dir"):
        if key in data and not isinstance(data[key], str):
            out.error(f"evaluation.{key}", f"{key} must be a string.")
    threshold = data.get("threshold")
    if threshold is not None and not (_is_number(threshold) and 0 <= threshold <= 1):
        out.error("evaluation.threshold", "threshold must be a number between 0 and 1.")
    if "parallel" in data and not isinstance(data["parallel"], bool):
        out.error("evaluation.parallel", "parallel must be true or false.")
    concurrency = data.get("max_concurrency")
    if concurrency is not None and not (_is_int(concurrency) and concurrency >= 1):
        out.error("evaluation.max_concurrency", "max_concurrency must be a whole number >= 1.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_import_path(path: str, value: Any, out: _Collector, base_dir: Path | None) -> None:
    if not isinstance(value, str) or not _IMPORT_PATH.match(value):
        out.error(path, f"Expected module:attribute, got {value!r}.")
        return
    if base_dir is None:
        return
    module = value.split(":", 1)[0]
    candidate = base_dir.joinpath(*module.split("."))
    if not (candidate.with_suffix(".py").exists() or (candidate / "__init__.py").exists()):
        out.warning(path, f"Module '{module}' was not found next to 10xgraph.json.")


def _quietly(parse: Callable[[], Any], out: _Collector, path: str) -> Any:
    """Run a server-side parser, turning its exceptions into an error issue.

    The parsers log operational warnings (for example about the memory rate-limit
    backend); those are reported as issues here, so the logger is muted meanwhile.
    """
    api_logger = logging.getLogger("tenxgraph_api")
    previous = api_logger.disabled
    api_logger.disabled = True
    try:
        return parse()
    except (TypeError, ValueError) as exc:
        out.error(path, str(exc))
        return None
    finally:
        api_logger.disabled = previous


def _has_unresolved_env(value: str) -> bool:
    # Same rule as graph_config._expand_env, which raises for these at startup.
    return os.path.expandvars(value) == value and (value.startswith("$") or "${" in value)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
