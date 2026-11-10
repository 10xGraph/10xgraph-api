"""The ``metrics`` block of 10xgraph.json and the MeterProvider it sets up (API and worker).

Metrics (request rate, latency, task throughput, LLM latency) are separate from AI tracing
(``observability``): they never carry spans, and HTTP spans never reach an AI tracing backend.
"""

import asyncio
import inspect
import logging
import os
import socket
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .settings import get_settings, logger


DEFAULT_BUCKETS_MS: list[float] = [50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000]
METRICS_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
SEMCONV_ENV = "OTEL_SEMCONV_STABILITY_OPT_IN"
INSTALL_HINT = "pip install '10xgraph-api[metrics]'"
# Bound for the final metrics push and AI span flush at shutdown (each, run at the same time).
TELEMETRY_FLUSH_SECONDS = 5.0


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricsOtlpConfig(_Section):
    """``metrics.otlp``: base URL of an OTLP HTTP receiver; ``/v1/metrics`` is appended."""

    endpoint: str | None = None


class MetricsPrometheusConfig(_Section):
    """``metrics.prometheus``: ``path`` is the API scrape route, ``worker_port`` the worker's."""

    path: str = Field(default="/metrics", pattern=r"^/")
    worker_port: int = Field(default=9464, ge=1, le=65535)

    @field_validator("path")
    @classmethod
    def _not_root(cls, path: str) -> str:
        if path == "/":
            raise ValueError('metrics.prometheus.path must not be "/"')
        return path


class MetricsConfig(_Section):
    """``metrics`` in 10xgraph.json. Example::

        "metrics": {
            "enabled": true,
            "export": "otlp",
            "otlp": {"endpoint": "http://collector:4318"},
            "prometheus": {"path": "/metrics", "worker_port": 9464},
            "latency_buckets_ms": [50, 100, 250, 500, 1000, 2500, 5000]
        }

    Auth headers for OTLP come from the standard ``OTEL_EXPORTER_OTLP_HEADERS`` env var.
    """

    enabled: bool = False
    export: Literal["otlp", "prometheus"] | None = None
    otlp: MetricsOtlpConfig = Field(default_factory=MetricsOtlpConfig)
    prometheus: MetricsPrometheusConfig = Field(default_factory=MetricsPrometheusConfig)
    latency_buckets_ms: list[float] = Field(default_factory=lambda: list(DEFAULT_BUCKETS_MS))

    @model_validator(mode="after")
    def _check(self) -> "MetricsConfig":
        buckets = self.latency_buckets_ms
        if not buckets or buckets[0] <= 0 or buckets != sorted(set(buckets)):
            raise ValueError("metrics.latency_buckets_ms must be positive and strictly ascending")
        if self.enabled:
            if self.export is None:
                raise ValueError('metrics.export is required when enabled: "otlp" or "prometheus"')
            if self.export == "otlp" and not (
                self.otlp.endpoint
                or os.environ.get(METRICS_ENDPOINT_ENV)
                or os.environ.get(OTLP_ENDPOINT_ENV)
            ):
                raise ValueError(
                    "metrics.otlp.endpoint is required for export=otlp "
                    f"(or set {METRICS_ENDPOINT_ENV} or {OTLP_ENDPOINT_ENV})"
                )
        return self

    @property
    def scrape_path(self) -> str | None:
        """The API route Prometheus scrapes; None unless metrics are on with export=prometheus."""
        return self.prometheus.path if self.enabled and self.export == "prometheus" else None


def count_audit_sink_error(sink: str) -> None:
    """Count one failed audit sink call. A no-op until ``setup_metrics`` has run."""
    from tenxgraph.utils.metrics import counter

    counter("tenxgraph_audit_sink_errors_total").inc(attributes={"sink": sink})


def _otlp_metrics_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/metrics") else base + "/v1/metrics"


def resolve_use_metrics_endpoint(obs_cfg: dict, metrics_cfg: MetricsConfig) -> dict:
    """Copy ``obs_cfg`` with ``otlp.endpoint`` taken from ``metrics.otlp.endpoint`` when
    ``observability.otlp.use_metrics_endpoint`` is on. Without a usable metrics endpoint the
    otlp destination is turned off in the copy (with a warning) instead of crashing."""
    otlp = obs_cfg.get("otlp") or {}
    if not otlp.get("enabled") or not otlp.get("use_metrics_endpoint"):
        return obs_cfg
    otlp = dict(otlp)
    endpoint = None
    if metrics_cfg.enabled and metrics_cfg.export == "otlp":
        # The exporter reads the same env vars when metrics.otlp.endpoint is not set.
        endpoint = (
            metrics_cfg.otlp.endpoint
            or os.environ.get(METRICS_ENDPOINT_ENV)
            or os.environ.get(OTLP_ENDPOINT_ENV)
        )
    if endpoint:
        # Core appends /v1/traces to the base URL.
        base = endpoint.rstrip("/")
        otlp["endpoint"] = base.removesuffix("/v1/metrics")
        if "/api/v1/otlp" in base:
            logger.warning(
                "observability.otlp.use_metrics_endpoint points AI traces at a Prometheus OTLP "
                "URL (%s); Prometheus accepts metrics only, so trace exports will fail. Use an "
                "endpoint that takes both (OTel Collector, Grafana Alloy) or a separate one.",
                endpoint,
            )
    else:
        logger.warning(
            "observability.otlp.use_metrics_endpoint needs metrics enabled with export=otlp and "
            "an OTLP endpoint (metrics.otlp.endpoint or %s); the otlp trace destination is off.",
            METRICS_ENDPOINT_ENV,
        )
        otlp["enabled"] = False
    return {**obs_cfg, "otlp": otlp}


# The provider of the last setup_metrics call; a rebuild in one process shuts it down first.
_providers: list = []


def _use_stable_http_names() -> None:
    """Make sure ``OTEL_SEMCONV_STABILITY_OPT_IN`` has ``http`` (or ``http/dup``), keeping other
    values, so HTTP metrics are ``http.server.request.duration`` (seconds). Must run before the
    first instrumentation: OpenTelemetry reads the variable once and caches it."""
    values = [v.strip() for v in os.environ.get(SEMCONV_ENV, "").split(",") if v.strip()]
    if "http" not in values and "http/dup" not in values:
        os.environ[SEMCONV_ENV] = ",".join([*values, "http"])
    try:
        from opentelemetry.instrumentation._semconv import (
            _OpenTelemetrySemanticConventionStability as stability,
        )
        from opentelemetry.instrumentation._semconv import (
            _OpenTelemetryStabilitySignalType,
            _StabilityMode,
        )

        # The mode is cached on first use; a read before our merge may have missed "http".
        cached = (
            stability._initialized
            and stability._get_opentelemetry_stability_opt_in_mode(
                _OpenTelemetryStabilitySignalType.HTTP
            )
            == _StabilityMode.DEFAULT
        )
    except Exception:  # a best-effort diagnostic: never block the setup
        return
    if cached:
        logger.warning(
            "OpenTelemetry read %s before the metrics block set it: HTTP metrics will use the "
            "old names (http.server.duration, in ms). Set %s=http in the environment.",
            SEMCONV_ENV,
            SEMCONV_ENV,
        )


def _shutdown_previous_provider() -> None:
    """Shut down the provider of an earlier setup in this process (app built twice in tests or
    when embedding); this also unregisters its Prometheus collector, so families are not doubled."""
    while _providers:
        try:
            _providers.pop().shutdown()
        except Exception as exc:
            logger.warning("Previous MeterProvider shutdown failed: %r", exc)


def setup_metrics(metrics_cfg: MetricsConfig, *, role: Literal["api", "worker"]):
    """Install the global MeterProvider and bridge core's counters and timers to it.

    Returns the provider, or None when the OpenTelemetry packages are missing or the setup
    fails (never raises).
    """
    _use_stable_http_names()
    _shutdown_previous_provider()
    try:
        from opentelemetry import metrics
        from tenxgraph.utils.metrics import setup_otel_metrics

        provider = _build_provider(metrics_cfg)
    except ImportError:
        logger.warning("Metrics are not exported: packages are missing. Install: %s", INSTALL_HINT)
        return None
    except Exception as exc:
        logger.warning("Metrics are not exported: setup failed: %s", exc)
        return None

    _providers.append(provider)
    metrics.set_meter_provider(provider)
    if metrics.get_meter_provider() is not provider:
        # Set earlier by opentelemetry-instrument, logfire.configure() or an imported module.
        logger.warning(
            "Another global MeterProvider was installed before the metrics block; 10xgraph "
            "and HTTP metrics still use the metrics block, other libraries use the other one."
        )
    # The provider itself, not the global one, so a refused set_meter_provider loses nothing.
    setup_otel_metrics(provider.get_meter("10xgraph"))

    if metrics_cfg.export == "prometheus":
        _announce_scrape(metrics_cfg, role)
    logger.info("Metrics enabled (export=%s, role=%s)", metrics_cfg.export, role)
    return provider


def _build_provider(metrics_cfg: MetricsConfig):
    from opentelemetry.sdk.metrics import Histogram, MeterProvider
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource

    if metrics_cfg.export == "otlp":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        endpoint = metrics_cfg.otlp.endpoint
        exporter = OTLPMetricExporter(endpoint=_otlp_metrics_url(endpoint) if endpoint else None)
        reader = PeriodicExportingMetricReader(exporter)
    else:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        reader = PrometheusMetricReader()

    buckets_ms = metrics_cfg.latency_buckets_ms
    views = [
        View(
            instrument_type=Histogram,
            instrument_unit=unit,
            aggregation=ExplicitBucketHistogramAggregation([b / divisor for b in buckets_ms]),
        )
        for unit, divisor in (("ms", 1), ("s", 1000))
    ]
    resource = Resource.create(
        {
            "service.name": get_settings().OTEL_SERVICE_NAME,
            # One per process, so pushes from several processes do not overwrite each other.
            "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
        }
    )
    return MeterProvider(resource=resource, metric_readers=[reader], views=views)


def _announce_scrape(metrics_cfg: MetricsConfig, role: str) -> None:
    if role == "api":
        from tenxgraph_api.src.app.loader import _web_concurrency

        workers = _web_concurrency()
        logger.log(
            logging.WARNING if workers > 1 else logging.INFO,
            "Prometheus scrape at %s covers this process only; with several gunicorn "
            "workers (WEB_CONCURRENCY=%d) use OTLP push instead.",
            metrics_cfg.prometheus.path,
            workers,
        )
        return
    port = metrics_cfg.prometheus.worker_port
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
    except OSError as exc:
        # TaskIQ starts several worker processes by default; only the first gets the port.
        logger.warning(
            "Metrics port %d is not available (%s): this worker process is not scraped. "
            "Use OTLP push or run the worker with --workers 1.",
            port,
            exc,
        )


async def flush_telemetry(meter_provider) -> None:
    """Last step of the API and worker shutdown: the final metrics push (MeterProvider
    shutdown) and a flush of the AI tracing provider (force_flush only, so a provider owned by
    Logfire or another library stays usable). Both run in threads at the same time, each
    bounded by ``TELEMETRY_FLUSH_SECONDS``. Never raises; a timeout or failure is a WARNING."""
    calls = []
    if meter_provider is not None:
        calls.append(_bounded("Metrics shutdown", meter_provider.shutdown))
    tracer_provider = _tracer_provider()
    if tracer_provider is not None:
        calls.append(_bounded("AI tracing flush", tracer_provider.force_flush))
    await asyncio.gather(*calls)


def _tracer_provider():
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    provider = trace.get_tracer_provider()
    # The default proxy (nothing configured) has no force_flush.
    return provider if hasattr(provider, "force_flush") else None


async def _bounded(what: str, call) -> None:
    started = time.monotonic()
    try:
        # MeterProvider.shutdown honours timeout_millis; BatchSpanProcessor.force_flush does not
        # (it waits for the exporter's own timeout), so wait_for is the real bound there.
        result = await asyncio.wait_for(
            asyncio.to_thread(call, timeout_millis=int(TELEMETRY_FLUSH_SECONDS * 1000)),
            TELEMETRY_FLUSH_SECONDS,
        )
    except TimeoutError:
        result = False
    except Exception as exc:
        logger.warning("%s failed: %r", what, exc)
        return
    # force_flush returns False on timeout; MeterProvider.shutdown returns None at the deadline.
    if result is False or time.monotonic() - started >= TELEMETRY_FLUSH_SECONDS:
        logger.warning(
            "%s did not finish within %ss; the last data may be lost",
            what,
            TELEMETRY_FLUSH_SECONDS,
        )


def check_scrape_route(app, scrape_route) -> None:
    """Refuse to start when another route answers on the scrape path. The scrape path is open
    to the auth guard and the rate limit, so a real route there would lose both. Call after all
    routers are included. ``scrape_route`` is the route the metrics block added (or None)."""
    if scrape_route is None:
        return
    for route in app.routes:
        regex = getattr(route, "path_regex", None)
        if route is not scrape_route and regex is not None and regex.match(scrape_route.path):
            raise ValueError(
                f"metrics.prometheus.path {scrape_route.path!r} clashes with the route "
                f"{route.path!r}; choose another path such as /_metrics."
            )


def native_telemetry_off() -> dict:
    """Keyword arguments for ``FastAPI(...)`` that turn its native OpenTelemetry off.

    Newer FastAPI (with a ``telemetry`` parameter) traces, meters and logs every request into
    the global providers by default, so HTTP spans would reach an AI tracing backend (Phoenix,
    LangSmith, OTLP, Logfire). HTTP spans come only from FastAPIInstrumentor on the legacy
    ``OTEL_ENABLED`` path and HTTP metrics only from the ``metrics`` block. Empty on an older
    FastAPI.
    """
    from fastapi import FastAPI

    if "telemetry" not in inspect.signature(FastAPI.__init__).parameters:
        return {}
    return {"telemetry": {"tracing": False, "metrics": False, "logs": False}}
