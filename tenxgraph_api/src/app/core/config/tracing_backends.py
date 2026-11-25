"""TracerProvider and exporters for the ``observability`` block of ``10xgraph.json``.

Logfire, LangSmith, Phoenix and a generic OTLP endpoint can be enabled at once; they share
one TracerProvider, so one span goes to every enabled exporter. The publisher that writes
the spans (``BackendsPublisher``) is bound by ``observability._setup_observability``.

Secrets stay in the environment (``LOGFIRE_TOKEN``, ``LANGSMITH_API_KEY``,
``PHOENIX_API_KEY``, ``OTEL_EXPORTER_OTLP_HEADERS``), never in the config.
"""

import inspect
from typing import Any

from tenxgraph.runtime.publisher._backends import (
    _build_langsmith_processor,
    _build_otlp_processor,
    _build_phoenix_processor,
    _guard_logfire,
    _new_tracer_provider,
    _phoenix_project,
)
from tenxgraph.runtime.publisher.identity import validate_user_id_mode

from .backends_publisher import enabled_backends
from .settings import logger


LANGSMITH_ENDPOINT = "https://api.smith.langchain.com/otel"


def _logfire_accepts_resource_attributes() -> bool:
    """True when the installed ``logfire.configure`` takes ``resource_attributes`` (logfire>=5)."""
    import logfire

    return "resource_attributes" in inspect.signature(logfire.configure).parameters


def configure_tracing_backends(config: dict[str, Any]) -> None:
    """Install the TracerProvider and span processors for every enabled backend.

    Expected ``config`` shape (the ``observability`` block)::

        {
            "level": "full",
            "user_id_mode": "raw",  # or "hash" / "mask"
            "logfire": {
                "enabled": True,
                "service_name": "svc",
                "send_to_logfire": True,
                "console": False,
            },
            "langsmith": {"enabled": True, "project": "my-project", "endpoint": None},
            "phoenix": {"enabled": True, "project": "my-project", "endpoint": None},
            "otlp": {"enabled": True, "endpoint": "http://collector:4318"},
        }

    Other keys in a backend block are ignored. For ``otlp``, a null ``endpoint`` needs
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``.

    With Logfire enabled, ``logfire.configure()`` installs the provider and the other
    backends' processors go in as ``additional_span_processors``. A Phoenix project then goes
    to ``logfire.configure(resource_attributes=...)`` as ``openinference.project.name`` when
    the installed logfire supports it (logfire>=5); otherwise a warning is logged and
    ``OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=<name>`` is the fallback.

    Raises:
        ImportError: If a requested backend's package is not installed.
        ValueError: If LangSmith has no API key, OTLP has no endpoint, or ``user_id_mode``
            is not one of ``raw``, ``hash``, ``mask``.
    """
    validate_user_id_mode(config.get("user_id_mode") or "raw")
    backends = enabled_backends(config)
    if not backends:
        return

    if "logfire" in backends:
        _guard_logfire()

    processors: list[Any] = []
    phoenix_project: str | None = None
    if "langsmith" in backends:
        langsmith_cfg = config.get("langsmith") or {}
        processors.append(
            _build_langsmith_processor(
                project=langsmith_cfg.get("project"),
                endpoint=langsmith_cfg.get("endpoint") or LANGSMITH_ENDPOINT,
            )
        )
    if "phoenix" in backends:
        phoenix_cfg = config.get("phoenix") or {}
        processors.append(_build_phoenix_processor(endpoint=phoenix_cfg.get("endpoint")))
        phoenix_project = _phoenix_project(phoenix_cfg.get("project"))
    if "otlp" in backends:
        otlp_cfg = config.get("otlp") or {}
        processors.append(_build_otlp_processor(endpoint=otlp_cfg.get("endpoint") or None))

    if "logfire" in backends:
        _configure_logfire(config.get("logfire") or {}, processors, phoenix_project)
        return

    from opentelemetry import trace

    provider = _new_tracer_provider(phoenix_project)
    for processor in processors:
        provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


def _configure_logfire(
    logfire_cfg: dict[str, Any], processors: list[Any], phoenix_project: str | None
) -> None:
    """Let ``logfire.configure()`` install the one TracerProvider, with ``processors`` on it."""
    import logfire

    kwargs: dict[str, Any] = {"send_to_logfire": bool(logfire_cfg.get("send_to_logfire", True))}
    if logfire_cfg.get("service_name") is not None:
        kwargs["service_name"] = logfire_cfg["service_name"]
    if logfire_cfg.get("console") is not None:
        kwargs["console"] = logfire_cfg["console"]
    if processors:
        kwargs["additional_span_processors"] = processors
    if phoenix_project:
        if _logfire_accepts_resource_attributes():
            kwargs["resource_attributes"] = {"openinference.project.name": phoenix_project}
        else:
            logger.warning(
                "Phoenix project %r is ignored: logfire.configure() has no "
                "resource_attributes before logfire 5.0. Upgrade logfire to >=5 or set "
                "OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=<name>. Spans go to "
                "Phoenix's default project.",
                phoenix_project,
            )
    logfire.configure(**kwargs)
