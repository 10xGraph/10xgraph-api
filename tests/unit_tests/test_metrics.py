"""The ``metrics`` block: config, MeterProvider setup, API wiring. The venv has no OpenTelemetry,
so the SDK, exporters and instrumentor are replaced by mocks in ``sys.modules``."""

import asyncio
import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tenxgraph_api.src.app.core.auth.route_guard import (
    DEFAULT_PUBLIC_PATHS,
    find_unprotected_routes,
)
from tenxgraph_api.src.app.core.config import metrics as metrics_module
from tenxgraph_api.src.app.core.config import setup_middleware as sm
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.metrics import (
    MetricsConfig,
    flush_telemetry,
    native_telemetry_off,
    resolve_use_metrics_endpoint,
    setup_metrics,
)


HTTP_OK = 200
HTTP_BAD_REQUEST = 400
PROMETHEUS_PORT = 9464


def _otel_modules(**extra):
    modules = {
        name: MagicMock()
        for name in (
            "opentelemetry",
            "opentelemetry.metrics",
            "opentelemetry.trace",
            "opentelemetry.sdk",
            "opentelemetry.sdk.trace",
            "opentelemetry.sdk.trace.export",
            "opentelemetry.sdk.metrics",
            "opentelemetry.sdk.metrics.view",
            "opentelemetry.sdk.metrics.export",
            "opentelemetry.sdk.resources",
            "opentelemetry.exporter",
            "opentelemetry.exporter.otlp",
            "opentelemetry.exporter.otlp.proto",
            "opentelemetry.exporter.otlp.proto.http",
            "opentelemetry.exporter.otlp.proto.http.metric_exporter",
            "opentelemetry.exporter.prometheus",
            "opentelemetry.instrumentation",
            "opentelemetry.instrumentation.fastapi",
        )
    }
    modules["opentelemetry"].metrics = modules["opentelemetry.metrics"]
    modules["opentelemetry"].trace = modules["opentelemetry.trace"]
    modules.update(extra)
    return modules


def _cfg(**kwargs):
    return MetricsConfig.model_validate(
        {"enabled": True, "export": "otlp", "otlp": {"endpoint": "http://c:4318"}, **kwargs}
    )


# ── config ────────────────────────────────────────────────────────────────────


def test_defaults_are_off_and_valid():
    cfg = MetricsConfig()
    assert not cfg.enabled
    assert cfg.scrape_path is None
    assert cfg.prometheus.path == "/metrics"
    assert cfg.prometheus.worker_port == PROMETHEUS_PORT
    assert cfg.latency_buckets_ms[0] == 50


def test_export_required_when_enabled():
    with pytest.raises(ValidationError, match="export is required"):
        MetricsConfig(enabled=True)


def test_otlp_endpoint_required_unless_env_set(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    with pytest.raises(ValidationError, match=r"otlp\.endpoint is required"):
        MetricsConfig(enabled=True, export="otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://x/v1/metrics")
    assert MetricsConfig(enabled=True, export="otlp").enabled


@pytest.mark.parametrize("buckets", [[], [0, 10], [-5, 10], [100, 50], [10, 10]])
def test_buckets_must_be_positive_and_sorted(buckets):
    with pytest.raises(ValidationError, match="latency_buckets_ms"):
        MetricsConfig(latency_buckets_ms=buckets)


def test_unknown_key_and_bad_export_rejected():
    with pytest.raises(ValidationError):
        MetricsConfig(bogus=1)
    with pytest.raises(ValidationError):
        MetricsConfig(enabled=True, export="statsd")


def test_scrape_path_only_for_enabled_prometheus():
    assert MetricsConfig(enabled=True, export="prometheus").scrape_path == "/metrics"
    assert _cfg().scrape_path is None


def test_graph_config_exposes_metrics_and_raises_on_invalid(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "a:b", "metrics": {"enabled": true}}')
    with pytest.raises(ValidationError):
        _ = GraphConfig(str(path)).metrics
    path.write_text('{"agent": "a:b"}')
    assert not GraphConfig(str(path)).metrics.enabled


# ── setup_metrics ─────────────────────────────────────────────────────────────


def test_setup_otlp_pushes_to_v1_metrics_with_views_and_resource(monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    modules = _otel_modules()
    with (
        patch.dict("sys.modules", modules),
        patch("tenxgraph.utils.metrics.setup_otel_metrics") as bridge,
    ):
        provider = setup_metrics(_cfg(), role="api")

    sdk = modules["opentelemetry.sdk.metrics"]
    assert provider is sdk.MeterProvider.return_value
    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
    exporter = modules["opentelemetry.exporter.otlp.proto.http.metric_exporter"]
    exporter.OTLPMetricExporter.assert_called_once_with(endpoint="http://c:4318/v1/metrics")
    export = modules["opentelemetry.sdk.metrics.export"]
    export.PeriodicExportingMetricReader.assert_called_once_with(
        exporter.OTLPMetricExporter.return_value
    )
    view = modules["opentelemetry.sdk.metrics.view"]
    units = [c.kwargs["instrument_unit"] for c in view.View.call_args_list]
    assert units == ["ms", "s"]
    seconds = view.ExplicitBucketHistogramAggregation.call_args_list[1].args[0]
    assert seconds[0] == pytest.approx(0.05)
    resource = modules["opentelemetry.sdk.resources"].Resource.create.call_args.args[0]
    assert "service.name" in resource
    assert resource["service.instance.id"].endswith(f"-{os.getpid()}")
    modules["opentelemetry.metrics"].set_meter_provider.assert_called_once_with(provider)
    provider.get_meter.assert_called_once_with("10xgraph")
    bridge.assert_called_once_with(provider.get_meter.return_value)


def test_global_meter_provider_set_earlier_warns_and_bridges_to_ours():
    modules = _otel_modules()  # get_meter_provider returns another object: set was refused
    with (
        patch.dict("sys.modules", modules),
        patch("tenxgraph.utils.metrics.setup_otel_metrics") as bridge,
        patch.object(metrics_module.logger, "warning") as warning,
    ):
        provider = setup_metrics(_cfg(), role="api")
    assert "Another global MeterProvider" in warning.call_args.args[0]
    bridge.assert_called_once_with(provider.get_meter.return_value)


def test_no_warning_when_our_provider_is_global():
    modules = _otel_modules()
    sdk = modules["opentelemetry.sdk.metrics"]
    modules[
        "opentelemetry.metrics"
    ].get_meter_provider.return_value = sdk.MeterProvider.return_value
    with (
        patch.dict("sys.modules", modules),
        patch("tenxgraph.utils.metrics.setup_otel_metrics"),
        patch.object(metrics_module.logger, "warning") as warning,
    ):
        setup_metrics(_cfg(), role="api")
    warning.assert_not_called()


def test_setup_failure_warns_and_returns_none():
    modules = _otel_modules()
    exporter = modules["opentelemetry.exporter.otlp.proto.http.metric_exporter"]
    exporter.OTLPMetricExporter.side_effect = ValueError("bad endpoint")
    with (
        patch.dict("sys.modules", modules),
        patch("tenxgraph.utils.metrics.setup_otel_metrics") as bridge,
        patch.object(metrics_module.logger, "warning") as warning,
    ):
        assert setup_metrics(_cfg(), role="api") is None
    assert "bad endpoint" in str(warning.call_args.args[1])
    modules["opentelemetry.metrics"].set_meter_provider.assert_not_called()
    bridge.assert_not_called()


def test_otlp_endpoint_already_ending_in_v1_metrics_is_kept():
    modules = _otel_modules()
    cfg = _cfg(otlp={"endpoint": "http://prom:9090/api/v1/otlp/v1/metrics"})
    with patch.dict("sys.modules", modules), patch("tenxgraph.utils.metrics.setup_otel_metrics"):
        setup_metrics(cfg, role="api")
    exporter = modules["opentelemetry.exporter.otlp.proto.http.metric_exporter"]
    exporter.OTLPMetricExporter.assert_called_once_with(
        endpoint="http://prom:9090/api/v1/otlp/v1/metrics"
    )


def test_otlp_without_endpoint_lets_the_exporter_read_the_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://env/v1/metrics")
    modules = _otel_modules()
    cfg = MetricsConfig(enabled=True, export="otlp")
    with patch.dict("sys.modules", modules), patch("tenxgraph.utils.metrics.setup_otel_metrics"):
        setup_metrics(cfg, role="api")
    exporter = modules["opentelemetry.exporter.otlp.proto.http.metric_exporter"]
    exporter.OTLPMetricExporter.assert_called_once_with(endpoint=None)


def test_prometheus_api_logs_info_and_starts_no_server(caplog):
    modules = _otel_modules(prometheus_client=MagicMock())
    cfg = MetricsConfig(enabled=True, export="prometheus")
    with (
        patch.dict("sys.modules", modules),
        patch("tenxgraph.utils.metrics.setup_otel_metrics"),
        caplog.at_level(logging.INFO, logger="10xgraph-api"),
    ):
        setup_metrics(cfg, role="api")
    modules["opentelemetry.exporter.prometheus"].PrometheusMetricReader.assert_called_once_with()
    modules["prometheus_client"].start_http_server.assert_not_called()
    assert any(
        "one process" in r.message or "this process only" in r.message for r in caplog.records
    )


def test_prometheus_worker_starts_server():
    modules = _otel_modules(prometheus_client=MagicMock())
    cfg = MetricsConfig(enabled=True, export="prometheus", prometheus={"worker_port": 9999})
    with patch.dict("sys.modules", modules), patch("tenxgraph.utils.metrics.setup_otel_metrics"):
        setup_metrics(cfg, role="worker")
    modules["prometheus_client"].start_http_server.assert_called_once_with(9999)


def test_prometheus_worker_port_in_use_warns_and_continues():
    client = MagicMock()
    client.start_http_server.side_effect = OSError("Address already in use")
    modules = _otel_modules(prometheus_client=client)
    cfg = MetricsConfig(enabled=True, export="prometheus")
    with (
        patch.dict("sys.modules", modules),
        patch("tenxgraph.utils.metrics.setup_otel_metrics"),
        patch.object(metrics_module.logger, "warning") as warning,
    ):
        provider = setup_metrics(cfg, role="worker")
    assert provider is not None
    message = warning.call_args.args[0] % warning.call_args.args[1:]
    assert str(PROMETHEUS_PORT) in message
    assert "OTLP push" in message
    assert "--workers 1" in message


def test_missing_packages_warn_and_return_none():
    with (
        patch.dict("sys.modules", {"opentelemetry": None}),
        patch.object(metrics_module.logger, "warning") as warning,
    ):
        assert setup_metrics(_cfg(), role="api") is None
    assert "10xgraph-api[metrics]" in warning.call_args.args[1]


def _flush(meter_provider, tracer_provider):
    trace = MagicMock()
    trace.get_tracer_provider.return_value = tracer_provider
    otel = MagicMock(trace=trace)
    with patch.dict("sys.modules", {"opentelemetry": otel, "opentelemetry.trace": trace}):
        asyncio.run(flush_telemetry(meter_provider))


def test_flush_telemetry_shuts_down_metrics_and_flushes_ai_tracing_with_a_bound():
    meter, tracer = MagicMock(), MagicMock()
    tracer.force_flush.return_value = True
    with patch.object(metrics_module.logger, "warning") as warning:
        _flush(meter, tracer)
    meter.shutdown.assert_called_once_with(timeout_millis=5000)
    tracer.force_flush.assert_called_once_with(timeout_millis=5000)
    tracer.shutdown.assert_not_called()  # a provider owned by Logfire stays usable
    warning.assert_not_called()


def test_flush_telemetry_never_raises_and_warns():
    meter, tracer = MagicMock(), MagicMock()
    meter.shutdown.side_effect = RuntimeError("collector down")
    tracer.force_flush.return_value = False  # the SDK's timeout result
    with patch.object(metrics_module.logger, "warning") as warning:
        _flush(meter, tracer)
        _flush(None, object())  # nothing configured: no force_flush
    messages = [c.args[0] % c.args[1:] for c in warning.call_args_list]
    assert any("Metrics shutdown failed" in m and "collector down" in m for m in messages)
    assert any("AI tracing flush did not finish" in m for m in messages)
    assert len(messages) == 2


def test_flush_telemetry_is_bounded_when_a_provider_ignores_the_timeout(monkeypatch):
    monkeypatch.setattr(metrics_module, "TELEMETRY_FLUSH_SECONDS", 0.05)
    meter = MagicMock()
    meter.shutdown.side_effect = lambda **kw: time.sleep(1.5)
    started = time.monotonic()
    with patch.object(metrics_module.logger, "warning") as warning:
        _flush(meter, None)
    # asyncio.run joins the leftover thread after the bound, so allow for the 1.5 s sleep.
    assert time.monotonic() - started < 2.5
    message = warning.call_args.args[0] % warning.call_args.args[1:]
    assert "Metrics shutdown did not finish within 0.05s" in message


def test_native_fastapi_telemetry_is_turned_off_when_supported():
    def new_init(self, *, title="", telemetry=None):
        """FastAPI with native telemetry."""

    def old_init(self, *, title=""):
        """FastAPI before native telemetry."""

    with patch.object(FastAPI, "__init__", new_init):
        assert native_telemetry_off() == {
            "telemetry": {"tracing": False, "metrics": False, "logs": False}
        }
    with patch.object(FastAPI, "__init__", old_init):
        assert native_telemetry_off() == {}


# ── API wiring ────────────────────────────────────────────────────────────────


def _graph_config(metrics: MetricsConfig, observability=None):
    config = MagicMock()
    config.metrics = metrics
    config.observability = observability
    config.rate_limit = None
    return config


def _settings(otel_enabled=False):
    settings = MagicMock()
    settings.OTEL_ENABLED = otel_enabled
    settings.ORIGINS = "http://localhost"
    settings.CORS_ALLOW_CREDENTIALS = True
    settings.MODE = "development"
    settings.ALLOWED_HOST = "*"
    settings.SECURITY_HEADERS_ENABLED = False
    settings.MAX_REQUEST_SIZE = 1_000_000
    return settings


def test_single_instrument_call_with_noop_tracer_when_otel_disabled():
    modules = _otel_modules()
    provider = MagicMock()
    app = FastAPI()
    with (
        patch.dict("sys.modules", modules),
        patch.object(sm, "get_settings", return_value=_settings(False)),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics", return_value=provider),
    ):
        sm.setup_middleware(app, graph_config=_graph_config(_cfg()), container=None)

    instrument = modules["opentelemetry.instrumentation.fastapi"].FastAPIInstrumentor.instrument_app
    instrument.assert_called_once()
    kwargs = instrument.call_args.kwargs
    assert (
        kwargs["tracer_provider"] is modules["opentelemetry.trace"].NoOpTracerProvider.return_value
    )
    assert kwargs["meter_provider"] is provider
    assert app.state.meter_provider is provider


def test_metrics_and_legacy_otel_share_one_instrument_call_and_one_meter_provider(caplog):
    modules = _otel_modules()
    provider = MagicMock()
    app = FastAPI()
    settings = _settings(True)
    settings.OTEL_SERVICE_NAME = "svc"
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = None
    with (
        patch.dict("sys.modules", modules),
        patch.object(sm, "get_settings", return_value=settings),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics", return_value=provider),
        patch.object(sm, "_setup_otel_metrics") as legacy_metrics,
        patch.object(sm, "_attach_otel_publisher"),
        caplog.at_level(logging.INFO, logger="10xgraph-api"),
    ):
        sm.setup_middleware(app, graph_config=_graph_config(_cfg()), container=None)

    legacy_metrics.assert_not_called()
    assert any("metrics come from the metrics block" in r.message for r in caplog.records)
    instrument = modules["opentelemetry.instrumentation.fastapi"].FastAPIInstrumentor.instrument_app
    instrument.assert_called_once()
    assert "tracer_provider" not in instrument.call_args.kwargs  # the global SDK provider
    assert instrument.call_args.kwargs["meter_provider"] is provider


@pytest.mark.parametrize("failure", ["no_endpoint", "no_grpc_exporter"])
def test_failed_legacy_otel_still_gets_http_metrics_from_the_metrics_block(failure):
    modules = _otel_modules()
    settings = _settings(True)
    settings.OTEL_SERVICE_NAME = "svc"
    if failure == "no_endpoint":
        settings.MODE = "production"
        settings.OTEL_EXPORTER_OTLP_ENDPOINT = None
    else:
        settings.OTEL_EXPORTER_OTLP_ENDPOINT = "http://c:4317"
        modules["opentelemetry.exporter.otlp.proto.grpc.trace_exporter"] = None  # ImportError
    provider = MagicMock()
    app = FastAPI()
    with (
        patch.dict("sys.modules", modules),
        patch.object(sm, "get_settings", return_value=settings),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics", return_value=provider),
        patch.object(sm, "_attach_otel_publisher"),
    ):
        sm.setup_middleware(app, graph_config=_graph_config(_cfg()), container=None)

    instrument = modules["opentelemetry.instrumentation.fastapi"].FastAPIInstrumentor.instrument_app
    instrument.assert_called_once()
    kwargs = instrument.call_args.kwargs
    assert (
        kwargs["tracer_provider"] is modules["opentelemetry.trace"].NoOpTracerProvider.return_value
    )
    assert kwargs["meter_provider"] is provider


def test_scrape_path_skips_the_host_check_like_the_probes():
    cfg = MetricsConfig(enabled=True, export="prometheus")
    fake_client = MagicMock()
    fake_client.CONTENT_TYPE_LATEST = "text/plain"
    fake_client.generate_latest.return_value = b"up 1\n"
    settings = _settings(False)
    settings.ALLOWED_HOST = "api.example.com"
    app = FastAPI()
    app.get("/other")(lambda: {"ok": True})
    with (
        patch.dict("sys.modules", {**_otel_modules(), "prometheus_client": fake_client}),
        patch.object(sm, "get_settings", return_value=settings),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics", return_value=MagicMock()),
    ):
        sm.setup_middleware(app, graph_config=_graph_config(cfg), container=None)
        client = TestClient(app)
        for host in ("10.1.2.3:8000", "api:8000"):
            assert client.get("/metrics", headers={"host": host}).status_code == HTTP_OK
        assert (
            client.get("/other", headers={"host": "10.1.2.3:8000"}).status_code == HTTP_BAD_REQUEST
        )
        assert (
            client.get("/metrics/x", headers={"host": "10.1.2.3:8000"}).status_code
            == HTTP_BAD_REQUEST
        )


def test_legacy_otel_without_metrics_block_keeps_legacy_metrics():
    settings = MagicMock()
    settings.OTEL_SERVICE_NAME = "svc"
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = None
    settings.MODE = "development"
    modules = _otel_modules()
    with (
        patch.dict("sys.modules", modules),
        patch.object(sm, "_setup_otel_metrics") as legacy_metrics,
    ):
        assert sm.setup_otel_provider(settings) is True
    legacy_metrics.assert_called_once()


def test_prometheus_route_serves_registry_without_auth_and_passes_the_guard():
    modules = _otel_modules()
    cfg = MetricsConfig(enabled=True, export="prometheus", prometheus={"path": "/scrape"})
    fake_client = MagicMock()
    fake_client.CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    fake_client.generate_latest.return_value = b"# HELP up\nup 1\n"
    app = FastAPI()
    with (
        patch.dict("sys.modules", {**modules, "prometheus_client": fake_client}),
        patch.object(sm, "get_settings", return_value=_settings(False)),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics", return_value=MagicMock()),
    ):
        sm.setup_middleware(app, graph_config=_graph_config(cfg), container=None)
        response = TestClient(app).get("/scrape")

    assert response.status_code == HTTP_OK
    assert response.content == b"# HELP up\nup 1\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert find_unprotected_routes(app, DEFAULT_PUBLIC_PATHS | {"/scrape"}) == []
    assert find_unprotected_routes(app) == ["GET /scrape"]


def test_scrape_path_is_excluded_from_rate_limiting():
    from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
    from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware

    cfg = MetricsConfig(enabled=True, export="prometheus")
    graph_config = _graph_config(cfg)
    graph_config.rate_limit = RateLimitConfig.from_dict({"exclude_paths": ["/docs"]})
    modules = _otel_modules(prometheus_client=MagicMock())
    app = FastAPI()
    with (
        patch.dict("sys.modules", modules),
        patch.object(sm, "get_settings", return_value=_settings(False)),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics", return_value=MagicMock()),
    ):
        sm.setup_middleware(app, graph_config=graph_config, container=MagicMock())

    limiter = next(m for m in app.user_middleware if m.cls is RateLimitMiddleware)
    assert limiter.kwargs["config"].exclude_paths == ("/docs", "/metrics")


def test_metrics_off_does_nothing():
    modules = _otel_modules()
    app = FastAPI()
    with (
        patch.dict("sys.modules", modules),
        patch.object(sm, "get_settings", return_value=_settings(False)),
        patch.object(sm, "init_sentry"),
        patch.object(sm, "setup_metrics") as setup,
    ):
        sm.setup_middleware(app, graph_config=_graph_config(MetricsConfig()), container=None)
    setup.assert_not_called()
    modules[
        "opentelemetry.instrumentation.fastapi"
    ].FastAPIInstrumentor.instrument_app.assert_not_called()


# ── observability.otlp.use_metrics_endpoint ──────────────────────────────────


OBS = {"otlp": {"enabled": True, "endpoint": None, "use_metrics_endpoint": True}}


def test_use_metrics_endpoint_copies_the_endpoint_without_mutating():
    resolved = resolve_use_metrics_endpoint(OBS, _cfg())
    assert resolved["otlp"]["endpoint"] == "http://c:4318"
    assert resolved["otlp"]["enabled"] is True
    assert OBS["otlp"]["endpoint"] is None


def test_use_metrics_endpoint_strips_v1_metrics_and_warns_for_prometheus():
    cfg = _cfg(otlp={"endpoint": "http://prom:9090/api/v1/otlp/v1/metrics/"})
    with patch.object(sm.logger, "warning") as warning:
        resolved = resolve_use_metrics_endpoint(OBS, cfg)
    assert resolved["otlp"]["endpoint"] == "http://prom:9090/api/v1/otlp"
    assert "Prometheus accepts metrics only" in warning.call_args.args[0]


def test_use_metrics_endpoint_collector_does_not_warn():
    cfg = _cfg(otlp={"endpoint": "http://collector:4318/v1/metrics"})
    with patch.object(sm.logger, "warning") as warning:
        resolved = resolve_use_metrics_endpoint(OBS, cfg)
    assert resolved["otlp"]["endpoint"] == "http://collector:4318"
    warning.assert_not_called()


@pytest.mark.parametrize(
    "metrics",
    [MetricsConfig(), MetricsConfig(enabled=True, export="prometheus")],
)
def test_use_metrics_endpoint_without_otlp_metrics_warns_and_disables(metrics):
    with patch.object(sm.logger, "warning") as warning:
        resolved = resolve_use_metrics_endpoint(OBS, metrics)
    assert resolved["otlp"]["enabled"] is False
    assert "use_metrics_endpoint" in warning.call_args.args[0]
    assert OBS["otlp"]["enabled"] is True


def test_setup_observability_hands_resolved_otlp_to_core():
    container = MagicMock()
    container.try_get.return_value = None
    graph_config = _graph_config(_cfg(), observability={"level": "standard", **OBS})
    with (
        patch("tenxgraph.runtime.publisher.exporters.setup_observability") as core_setup,
        patch.object(sm, "_bind_publisher"),
    ):
        sm._setup_observability(container, graph_config)
    core_setup.assert_called_once()
    assert core_setup.call_args.args[1]["otlp"]["endpoint"] == "http://c:4318"


def test_setup_observability_runs_for_otlp_alone():
    container = MagicMock()
    container.try_get.return_value = None
    obs = {"otlp": {"enabled": True, "endpoint": "http://t:4318"}}
    with (
        patch("tenxgraph.runtime.publisher.exporters.setup_observability") as core_setup,
        patch.object(sm, "_bind_publisher"),
    ):
        sm._setup_observability(container, _graph_config(MetricsConfig(), observability=obs))
    core_setup.assert_called_once()
