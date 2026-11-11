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
    ID_GENERATOR_NAMES,
    KNOWN_TOP_LEVEL_KEYS,
    THREAD_NAME_CHOICES,
    section_for_key,
)
from tenxgraph_api.src.app.core.auth.auth_config import check_auth_config


Issue = dict[str, str]

_IMPORT_PATH = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
# RbacConfig reports a bad role as "role 'name': ..." on the whole auth object.
_ROLE_IN_MESSAGE = re.compile(r"role '([^']+)'")
_OBSERVABILITY_LEVELS = ("spans", "standard", "full")
_USER_ID_MODES = ("raw", "hash", "mask")
_TRACE_DESTINATIONS = ("logfire", "langsmith", "phoenix", "otlp")
_MAX_PERCENT = 100
_AUDIT_KEYS = ("enabled", "sinks", "include_reads")
# Settings that live inside cache_store; at the top level the server refuses them.
_CACHE_STORE_SETTINGS = ("redis", "rate_limit", "token_revocation")


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
    _check_cache_store(config, out, base_dir)
    _check_websocket(config, out)
    _check_runs(config, out)
    _check_ag_ui(config, out)
    _check_media(config, out)
    _check_observability(config, out)
    _check_metrics(config, out)
    _check_audit(config, out)
    _check_wiring(config, out, base_dir)
    _check_remote_tools(config, out)
    _check_test(config, out)
    _check_evaluation(config, out)
    _check_worker(config, out)
    out.issues.extend(production_readiness_warnings(config))

    for key in config:
        if key not in KNOWN_TOP_LEVEL_KEYS and key not in _CACHE_STORE_SETTINGS:
            out.warning(key, f"'{key}' is not read by 10xGraph. It is kept as-is.")
    return out.issues


def production_readiness_warnings(config: dict) -> list[Issue]:
    """Return warnings for settings that are fine locally but risky in production.

    Reads only the JSON, never ``.env`` or the environment. Auth stays the developer's
    choice, so these are warnings, never errors. The auth-off warning comes from
    ``check_auth_config``, run in :func:`validate_config`.
    """
    out = _Collector()
    auth = _auth_is_on(config)

    cache_store = config.get("cache_store")
    cache_store = cache_store if isinstance(cache_store, dict) else {}
    store_type = cache_store.get("type", "memory")
    rate_limit = cache_store.get("rate_limit")
    if rate_limit is None or (isinstance(rate_limit, dict) and rate_limit.get("enabled") is False):
        out.warning(
            "cache_store.rate_limit",
            "Rate limiting is off, so one caller can exhaust the service. Turn it on under "
            "cache_store.rate_limit, with a redis or custom cache store for more than one "
            "worker, before production.",
        )
    elif isinstance(rate_limit, dict):
        if store_type == "memory":
            out.warning(
                "cache_store.rate_limit",
                "The memory cache store counts requests per process, so with more than one "
                "worker each worker has its own budget. Use redis or custom to share counts.",
            )
        if auth and rate_limit.get("by", "ip") == "ip":
            out.warning(
                "cache_store.rate_limit.by",
                "Counting requests per IP with auth on shares one budget between users behind a "
                'proxy. Use "by": "user", or set trusted_proxy_headers behind your proxy.',
            )
        exclude = rate_limit.get("exclude_paths")
        if isinstance(exclude, list) and "/redoc" in exclude and "/redocs" not in exclude:
            out.warning(
                "cache_store.rate_limit.exclude_paths",
                'The ReDoc page is served at /redocs, not /redoc; "/redoc" excludes nothing.',
            )

    revocation = cache_store.get("token_revocation")
    revocation_off = not revocation or (
        isinstance(revocation, dict) and revocation.get("enabled") is False
    )
    if auth and revocation_off:
        out.warning(
            "cache_store.token_revocation",
            "Token revocation is off, so a leaked or logged-out token works until it expires. "
            'Set "token_revocation": true under cache_store, with a redis or custom cache store, '
            "and revoke with `10xgraph revoke`.",
        )
    elif revocation and not revocation_off and store_type == "memory":
        out.warning(
            "cache_store.token_revocation",
            "10xgraph revoke cannot reach an in-memory store; use redis or custom to share "
            "revocations.",
        )
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


def _auth_is_on(config: dict) -> bool:
    auth = config.get("auth")
    if isinstance(auth, dict):
        return auth.get("method", "none") != "none"
    return bool(auth)


def _check_auth(config: dict, out: _Collector, base_dir: Path | None) -> None:
    """Run the loader's own auth rules; each issue lands on the field it names.

    ``production=True`` so the editor always nudges about auth being off, as it did before.
    """
    for issue in check_auth_config(config, production=True):
        field, named = issue.field, _ROLE_IN_MESSAGE.match(issue.message)
        if field == "auth" and named:
            field = f"auth.roles.{named[1]}"  # shown beside that role's row
        out.add(issue.level, field, issue.message)
    auth = config.get("auth")
    path = auth.get("path") if isinstance(auth, dict) else None
    if isinstance(path, str) and _IMPORT_PATH.match(path):
        _check_import_path("auth.path", path, out, base_dir)


def _check_websocket(config: dict, out: _Collector) -> None:
    data = config.get("websocket")
    if data is None:
        return
    from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig

    _quietly(lambda: WebSocketConfig.from_dict(data), out, "websocket")


def _check_cache_store(config: dict, out: _Collector, base_dir: Path | None) -> None:
    from tenxgraph_api.src.app.core.config.graph_config import (
        CacheStoreConfig,
        check_cache_store_settings,
    )

    # Top-level redis, rate_limit and token_revocation: the parser names where they go.
    for key in _CACHE_STORE_SETTINGS:
        if key in config:
            _quietly(lambda key=key: check_cache_store_settings({key: config[key]}), out, key)  # type: ignore[misc]

    data = config.get("cache_store")
    if data is None:
        return
    errors = len(out.issues)
    parsed = _quietly(lambda: CacheStoreConfig.parse(data), out, "cache_store")
    if parsed is None:
        # Point an error at the field it names, so the page shows it beside the input.
        for issue in out.issues[errors:]:
            for field in ("url", "path", "type"):
                if issue["message"].startswith(f"cache_store.{field}"):
                    issue["path"] = f"cache_store.{field}"
        return
    if parsed.type == "custom":
        _check_import_path("cache_store.path", parsed.path, out, base_dir)
    if parsed.type == "redis" and parsed.url is None and not os.environ.get("REDIS_URL"):
        out.warning(
            "cache_store.url",
            "Empty, so the server uses REDIS_URL, which is not set in this shell. Make sure it "
            "is set when the API runs, or set the URL here.",
        )
    if parsed.url and _has_unresolved_env(parsed.url):
        # The server expands $VAR at startup; it is fine for it to be unset while editing.
        out.warning(
            "cache_store.url",
            f"{parsed.url} is not set in this shell. Make sure it is set when the API runs.",
        )
    if parsed.token_revocation is not None and not _auth_is_on(config):
        out.warning(
            "cache_store.token_revocation", "Auth is off, so there are no tokens to revoke."
        )


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
    level = data.get("level", "full")
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
    enabled_blocks: dict[str, dict] = {}
    for backend in _TRACE_DESTINATIONS:
        block = data.get(backend)
        if block is None:
            continue
        if not isinstance(block, dict):
            out.error(f"observability.{backend}", f"{backend} must be an object.")
            continue
        enabled = block.get("enabled", False)
        if not isinstance(enabled, bool):
            out.error(f"observability.{backend}.enabled", "enabled must be true or false.")
        if enabled is True:
            enabled_blocks[backend] = block
        console = block.get("console")
        if backend == "logfire" and console is not None and console is not False:
            out.error(
                "observability.logfire.console",
                "console must be false or left out. Logfire takes False or a ConsoleOptions "
                "object, which JSON cannot express.",
            )
    if "otlp" in enabled_blocks:
        _check_otlp_tracing(config, enabled_blocks["otlp"], out)
    if not enabled_blocks:
        out.warning(
            "observability",
            "None of Logfire, LangSmith, Phoenix or OTLP is enabled, so nothing is traced.",
        )


def _check_otlp_tracing(config: dict, otlp: dict, out: _Collector) -> None:
    shared = otlp.get("use_metrics_endpoint")
    if shared is not None and not isinstance(shared, bool):
        out.error(
            "observability.otlp.use_metrics_endpoint", "use_metrics_endpoint must be true or false."
        )
        return
    endpoint = otlp.get("endpoint")
    if shared:
        metrics = config.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        otlp_cfg = metrics.get("otlp")
        url = otlp_cfg.get("endpoint") if isinstance(otlp_cfg, dict) else None
        if not (metrics.get("enabled") is True and metrics.get("export") == "otlp" and url):
            out.warning(
                "observability.otlp.use_metrics_endpoint",
                "Metrics are not set to OTLP push with an endpoint, so there is no endpoint to "
                "share and the OTLP destination stays off.",
            )
            return
        endpoint = url
    elif not endpoint:
        if not os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            out.error(
                "observability.otlp.endpoint",
                "OTLP needs an endpoint, or turn on Use the metrics endpoint.",
            )
        return
    if isinstance(endpoint, str) and "/api/v1/otlp" in endpoint:
        out.warning(
            "observability.otlp.use_metrics_endpoint" if shared else "observability.otlp.endpoint",
            "This is a Prometheus OTLP URL. Prometheus takes metrics only, so trace exports "
            "will fail. Use a collector (OTel Collector, Grafana Alloy) that takes both.",
        )


def _check_metrics(config: dict, out: _Collector) -> None:
    data = config.get("metrics")
    if data is None:
        return
    if not isinstance(data, dict):
        out.error("metrics", "metrics must be an object.")
        return
    from pydantic import ValidationError

    from tenxgraph_api.src.app.core.config.metrics import MetricsConfig

    try:
        MetricsConfig.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            message = err["msg"].removeprefix("Value error, ")
            path = ".".join(["metrics", *(str(part) for part in err["loc"])])
            # A rule on the whole model names its field in the message.
            named = re.match(r"(metrics\.[\w.]+) ", message)
            out.error(named.group(1) if named else path, message)


def _check_audit(config: dict, out: _Collector) -> None:
    data = config.get("audit")
    if data is None:
        return
    if not isinstance(data, dict):
        out.error("audit", "audit must be an object.")
        return
    for key in data:
        if key not in _AUDIT_KEYS:
            out.error(
                f"audit.{key}", f"audit.{key} is not a setting. Use {', '.join(_AUDIT_KEYS)}."
            )
    for key in ("enabled", "include_reads"):
        if key in data and not isinstance(data[key], bool):
            out.error(f"audit.{key}", f"audit.{key} must be true or false.")
    sinks = data.get("sinks")
    if sinks is None:
        return
    if not isinstance(sinks, list) or not sinks:
        out.error("audit.sinks", "audit.sinks must be a non-empty list.")
        return
    for sink in sinks:
        if sink not in ("log", "publisher") and not (
            isinstance(sink, str) and _IMPORT_PATH.match(sink)
        ):
            out.error(
                "audit.sinks",
                f'Unknown sink {sink!r}. Use "log", "publisher" or "module:Class".',
            )


def _check_wiring(config: dict, out: _Collector, base_dir: Path | None) -> None:
    for key in ("injectq", "store", "checkpointer"):
        value = config.get(key)
        if value is None or value == "":
            continue
        _check_import_path(key, value, out, base_dir)
    mismatch = config.get("store_allow_tenant_mismatch")
    if mismatch is not None and not isinstance(mismatch, bool):
        out.error(
            "store_allow_tenant_mismatch", "store_allow_tenant_mismatch must be true or false."
        )
    if config.get("checkpointer"):
        out.warning(
            "checkpointer",
            "The server uses the checkpointer passed to graph.compile(); this key is "
            "informational.",
        )
    _check_choice("id_generator", config, ID_GENERATOR_NAMES, out, base_dir)
    _check_choice("thread_name_generator", config, THREAD_NAME_CHOICES, out, base_dir)


def _check_choice(
    key: str, config: dict, names: tuple[str, ...], out: _Collector, base_dir: Path | None
) -> None:
    """A built-in name or ``module:attr``; unset and empty mean the default."""
    value = config.get(key)
    if value is None or value == "" or value in names:
        return
    if not isinstance(value, str) or ":" not in value:
        out.error(key, f"{key} must be one of {', '.join(names)} or module:attribute.")
        return
    _check_import_path(key, value, out, base_dir)


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
    if "open_report" in data and not isinstance(data["open_report"], bool):
        out.error("evaluation.open_report", "open_report must be true or false.")
    concurrency = data.get("max_concurrency")
    if concurrency is not None and not (_is_int(concurrency) and concurrency >= 1):
        out.error("evaluation.max_concurrency", "max_concurrency must be a whole number >= 1.")


_WORKER_TYPES = {
    "broker": ("redis_stream", "rabbitmq", "kafka", "memory"),
    "result_backend": ("redis", "memory"),
}


def _check_worker(config: dict, out: _Collector) -> None:
    data = config.get("worker")
    if data is None:
        return
    from pydantic import ValidationError

    from tenxgraph_api.jobs.config import WorkerConfig

    refused: set[str] = set()
    if isinstance(data, dict):
        for part, allowed in _WORKER_TYPES.items():
            value = (data.get(part) or {}).get("type") if isinstance(data.get(part), dict) else None
            if value is not None and value not in allowed:
                path = f"worker.{part}.type"
                refused.add(path)
                out.error(path, f"{value!r} is not supported. Supported: {', '.join(allowed)}.")

    try:
        WorkerConfig.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            path = ".".join(["worker", *(str(part) for part in err["loc"])])
            if path not in refused:
                out.error(path, err["msg"].removeprefix("Value error, "))


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

    The parsers may log operational warnings; those are reported as issues here, so the
    logger is muted meanwhile.
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


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
