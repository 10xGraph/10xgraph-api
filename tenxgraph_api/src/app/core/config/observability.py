import os
import re
from typing import Any, cast

from fastapi import FastAPI
from injectq import InjectQ
from tenxgraph.runtime.publisher.identity import UserIdMode

from .graph_config import GraphConfig
from .metrics import resolve_use_metrics_endpoint
from .settings import logger


def _user_id_mode(graph_config: GraphConfig | None) -> UserIdMode:
    """``observability.user_id_mode`` from ``10xgraph.json``: how traces carry the user id."""
    obs_cfg = graph_config.observability if graph_config is not None else None
    return cast("UserIdMode", str((obs_cfg or {}).get("user_id_mode") or "raw"))


def _attach_otel_publisher(
    container: InjectQ, settings, graph_config: GraphConfig | None = None
) -> None:
    """Wire OtelPublisher into the container when OTEL_ENABLED=true.

    Merges with any publisher already bound in the container:
      - None bound         → bind OtelPublisher directly
      - CompositePublisher → add OtelPublisher to it
      - Single publisher   → wrap both in a new CompositePublisher
    """
    try:
        from tenxgraph.runtime.publisher.base_publisher import BasePublisher
        from tenxgraph.runtime.publisher.composite_publisher import CompositePublisher
        from tenxgraph.runtime.publisher.otel_publisher import ObservabilityLevel, OtelPublisher
    except ImportError:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry packages are not installed. "
            "Install with: pip install '10xgraph[otel]'"
        )
        return

    try:
        level = ObservabilityLevel(settings.OTEL_LEVEL.lower())
    except ValueError:
        logger.warning(
            "Invalid OTEL_LEVEL=%r, falling back to 'full'. Valid: spans, standard, full",
            settings.OTEL_LEVEL,
        )
        level = ObservabilityLevel.FULL

    otel_publisher = OtelPublisher(level=level, user_id_mode=_user_id_mode(graph_config))
    existing = container.try_get(BasePublisher)

    if existing is None:
        container.bind_instance(BasePublisher, otel_publisher)
        logger.info("OTEL: OtelPublisher bound (level=%s)", level)
    elif isinstance(existing, CompositePublisher):
        existing.add_publisher(otel_publisher)
        logger.info("OTEL: OtelPublisher added to existing CompositePublisher (level=%s)", level)
    else:
        container.bind_instance(BasePublisher, CompositePublisher([existing, otel_publisher]))
        logger.info(
            "OTEL: wrapped %s + OtelPublisher in CompositePublisher (level=%s)",
            type(existing).__name__,
            level,
        )


def _observability_publishers(publisher: Any) -> list[Any]:
    """Return the publishers (walking composites) whose ``kind`` is ``"observability"``."""
    if publisher is None:
        return []
    children = getattr(publisher, "_publishers", None)
    if isinstance(children, list | tuple):
        found: list[Any] = []
        for child in children:
            found.extend(_observability_publishers(child))
        return found
    return [publisher] if getattr(publisher, "kind", None) == "observability" else []


def _setup_observability(container: InjectQ | None, graph_config: GraphConfig | None) -> None:
    """Wire Logfire/LangSmith/Phoenix/OTLP from the ``observability`` block of ``10xgraph.json``.

    Configures the tracer provider(s)/exporters and binds an ``OtelPublisher``
    into the DI container. This runs at app-construction time, before the graph
    is loaded and compiled — the only reliable attach point, because InjectQ
    freezes the ``BasePublisher`` binding at ``container.compile()`` (which runs
    inside ``StateGraph.compile()``). The core ``StateGraph.compile()`` guard
    preserves a publisher already present in the container instead of clobbering
    it with ``None``.

    Secrets stay in the environment (``LOGFIRE_TOKEN``, ``LANGSMITH_API_KEY``, ``PHOENIX_API_KEY``);
    only non-secret settings live in ``10xgraph.json``.
    """
    if container is None or graph_config is None:
        return

    obs_cfg = graph_config.observability
    if not obs_cfg:
        return
    obs_cfg = resolve_use_metrics_endpoint(obs_cfg, graph_config.metrics)

    logfire_on = bool((obs_cfg.get("logfire") or {}).get("enabled", False))
    langsmith_on = bool((obs_cfg.get("langsmith") or {}).get("enabled", False))
    phoenix_on = bool((obs_cfg.get("phoenix") or {}).get("enabled", False))
    otlp_on = bool((obs_cfg.get("otlp") or {}).get("enabled", False))
    if not (logfire_on or langsmith_on or phoenix_on or otlp_on):
        return

    try:
        from tenxgraph.runtime.publisher.base_publisher import BasePublisher
        from tenxgraph.runtime.publisher.composite_publisher import CompositePublisher
        from tenxgraph.runtime.publisher.exporters import setup_observability
        from tenxgraph.runtime.publisher.otel_publisher import ObservabilityLevel, OtelPublisher
    except ImportError:
        logger.warning(
            "observability is configured but required packages are not installed. "
            "Install with: pip install '10xgraph[observability]'"
        )
        return

    # The graph may already carry its own observability publisher (Phoenix, LangSmith,
    # Logfire, plain OTel). A second one would duplicate spans, and OTel refuses to
    # replace the global tracer provider, so reuse it and apply nothing from the block.
    existing_obs = _observability_publishers(container.try_get(BasePublisher))
    if existing_obs:
        logger.warning(
            "observability is set in 10xgraph.json, but the graph already has an "
            "observability publisher (%s); reusing it. The observability block is not "
            "applied, including its level.",
            ", ".join(type(p).__name__ for p in existing_obs),
        )
        return

    # Configure the provider(s)/exporter(s) only. graph=None means no publisher
    # is attached here; we bind it into the container ourselves below so it is
    # in place before the graph compiles.
    try:
        setup_observability(None, obs_cfg)
    except (ImportError, ValueError) as exc:
        logger.warning("Observability setup skipped: %s", exc)
        return

    try:
        level = ObservabilityLevel(str(obs_cfg.get("level", "full")).lower())
    except ValueError:
        logger.warning(
            "Invalid observability level=%r, falling back to 'full'.",
            obs_cfg.get("level"),
        )
        level = ObservabilityLevel.FULL

    otel_publisher = OtelPublisher(level=level, user_id_mode=_user_id_mode(graph_config))
    existing = container.try_get(BasePublisher)

    if existing is None:
        container.bind_instance(BasePublisher, otel_publisher)
    elif isinstance(existing, CompositePublisher):
        existing.add_publisher(otel_publisher)
    else:
        container.bind_instance(BasePublisher, CompositePublisher([existing, otel_publisher]))

    logger.info(
        "Observability enabled (logfire=%s, langsmith=%s, phoenix=%s, otlp=%s, level=%s)",
        logfire_on,
        langsmith_on,
        phoenix_on,
        otlp_on,
        level,
    )


def setup_otel_provider(settings, *, metrics_owned: bool = False) -> bool:
    """Configure OpenTelemetry traces and metrics (API and worker share this).

    Metrics use a MeterProvider with the same resource and endpoint (OTLP gRPC, or the console
    exporter when none), and are bridged to core's counters and timers. Point the endpoint at a
    collector to forward them to Prometheus. The export interval honours the standard
    OTEL_METRIC_EXPORT_INTERVAL env var.

    Reads OTEL_EXPORTER_OTLP_ENDPOINT from settings. If unset, falls back to
    the standard OTEL_EXPORTER_OTLP_ENDPOINT env var (honoured automatically
    by the OTLP exporter). A ConsoleSpanExporter is used when no endpoint is
    configured and the app is not in production, so traces are visible locally
    without any collector. In production with no endpoint nothing is set up and a warning
    is logged. Returns True when a provider was installed.

    With ``metrics_owned`` the ``metrics`` block already installed the MeterProvider, so no
    second one is created here.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry packages are not installed. "
            "Install with: pip install '10xgraph-api[otel]'"
        )
        return False

    resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            logger.info("OTEL: exporting traces to %s", endpoint)
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp "
                "is not installed. Install with: pip install '10xgraph-api[otel]'"
            )
            return False
    elif settings.MODE == "production":
        # Printing spans to stdout would put request URLs and, at OTEL_LEVEL=full, message
        # content in the container logs, so production exports nowhere instead.
        logger.warning(
            "OTEL_ENABLED=true but OTEL_EXPORTER_OTLP_ENDPOINT is not set; traces are not "
            "exported. Console span output is development-only. Set the endpoint to export."
        )
        return False
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("OTEL: no endpoint configured, printing spans to console")

    trace.set_tracer_provider(provider)
    if metrics_owned:
        logger.info("OTEL: metrics come from the metrics block")
    else:
        _setup_otel_metrics(resource, endpoint)
    return True


def _setup_otel_metrics(resource, endpoint: str | None) -> None:
    """Install a MeterProvider and bridge core's metrics to it. Traces keep working on failure."""
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            ConsoleMetricExporter,
            PeriodicExportingMetricReader,
        )
        from tenxgraph.utils.metrics import setup_otel_metrics

        if endpoint:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

            exporter = OTLPMetricExporter(endpoint=endpoint)
        else:
            exporter = ConsoleMetricExporter()
    except ImportError:
        logger.warning(
            "OTEL metrics are not exported: opentelemetry packages are missing. "
            "Install with: pip install '10xgraph-api[otel]'"
        )
        return
    reader = PeriodicExportingMetricReader(exporter)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    setup_otel_metrics()
    logger.info("OTEL: metrics enabled")


def _excluded_urls(app: FastAPI, scrape_path: str) -> str:
    """Comma-separated URL regexes the instrumentation skips: the scrape route (so scraping does
    not show up in the HTTP metrics) plus whatever the standard env variables already exclude.
    Passing ``excluded_urls`` replaces those variables, hence they are merged here."""
    own = rf"^\w+://[^/]*{re.escape(app.root_path)}{re.escape(scrape_path)}(\?|$)"
    env = os.environ.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS") or os.environ.get(
        "OTEL_PYTHON_EXCLUDED_URLS", ""
    )
    return f"{env},{own}" if env else own


def _instrument_fastapi(
    app: FastAPI, *, tracing: bool, meter_provider=None, scrape_path: str | None = None
) -> bool:
    """The one ``instrument_app`` call (it can run only once per app).

    HTTP spans go to the global tracer provider only with the legacy ``OTEL_ENABLED`` path;
    otherwise they go to a no-op one, so they never reach an AI tracing backend. HTTP metrics
    go to ``meter_provider`` when given, else to the global one. ``scrape_path`` is left out of
    both.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.trace import NoOpTracerProvider
    except ImportError:
        logger.warning(
            "FastAPI instrumentation skipped: opentelemetry packages are not installed. "
            "Install with: pip install '10xgraph-api[otel]' or '10xgraph-api[metrics]'"
        )
        return False
    kwargs: dict[str, Any] = {}
    if scrape_path:
        kwargs["excluded_urls"] = _excluded_urls(app, scrape_path)
    if not tracing:
        kwargs["tracer_provider"] = NoOpTracerProvider()
    if meter_provider is not None:
        kwargs["meter_provider"] = meter_provider
    FastAPIInstrumentor.instrument_app(app, **kwargs)
    return True


def _setup_otel(app: FastAPI, settings, meter_provider=None, scrape_path=None) -> bool:
    """Configure OpenTelemetry tracing and instrument the FastAPI app.

    ``meter_provider`` is the one from the ``metrics`` block; it then owns the metrics.
    Returns True when the app was instrumented.
    """
    if not setup_otel_provider(settings, metrics_owned=meter_provider is not None):
        return False
    if not _instrument_fastapi(
        app, tracing=True, meter_provider=meter_provider, scrape_path=scrape_path
    ):
        return False
    logger.info("OTEL: FastAPI instrumentation active (service=%s)", settings.OTEL_SERVICE_NAME)
    return True
