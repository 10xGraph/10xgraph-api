import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenxgraph_api.src.app.core.config import setup_middleware as setup_middleware_module
from tenxgraph_api.src.app.core.config.setup_middleware import (
    SelectiveGZipMiddleware,
    setup_middleware,
)


HTTP_OK = 200
MIN_REQUEST_ID_LEN = 10


def test_request_id_middleware_adds_headers():
    app = FastAPI()
    setup_middleware(app)

    @app.get("/echo")
    def echo():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/echo")
    assert r.status_code == HTTP_OK
    assert "X-Request-ID" in r.headers
    assert "X-Timestamp" in r.headers
    # Ensure stable format (uuid length 36) and iso-like timestamp
    assert len(r.headers["X-Request-ID"]) >= MIN_REQUEST_ID_LEN
    assert "T" in r.headers["X-Timestamp"]


@pytest.mark.asyncio
async def test_selective_gzip_middleware_excludes():
    from unittest.mock import AsyncMock

    called = []

    async def app(scope, receive, send):
        called.append((scope, receive, send))

    with patch(
        "tenxgraph_api.src.app.core.config.setup_middleware.GZipMiddleware"
    ) as MockGZipMiddleware:
        mock_gzip_instance = AsyncMock()
        MockGZipMiddleware.return_value = mock_gzip_instance

        middleware = SelectiveGZipMiddleware(app)

        # Test excluded path
        scope = {"type": "http", "path": "/v1/graph/stream"}
        receive = MagicMock()
        send = MagicMock()
        await middleware(scope, receive, send)

        assert len(called) == 1
        assert called[0][0] == scope
        mock_gzip_instance.assert_not_called()

        called.clear()
        mock_gzip_instance.reset_mock()

        # Test non-excluded path
        scope2 = {"type": "http", "path": "/v1/graph/other"}
        await middleware(scope2, receive, send)

        assert len(called) == 0
        mock_gzip_instance.assert_called_once_with(scope2, receive, send)


def test_setup_otel_import_error():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_otel

    settings = MagicMock()
    settings.OTEL_SERVICE_NAME = "test"
    with patch.dict("sys.modules", {"opentelemetry": None}):
        # setup_middleware should catch ImportError and return without raising
        _setup_otel(MagicMock(), settings)


def test_setup_otel_with_endpoint():
    settings = MagicMock()
    settings.OTEL_ENABLED = True
    settings.OTEL_SERVICE_NAME = "test-service"
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"

    mock_trace = MagicMock()
    mock_trace.trace = mock_trace
    mock_trace.__path__ = []
    mock_instrumentor = MagicMock()
    mock_resource = MagicMock()
    mock_provider = MagicMock()
    mock_exporter = MagicMock()
    mock_processor = MagicMock()

    modules = {
        "opentelemetry": mock_trace,
        "opentelemetry.trace": mock_trace,
        "opentelemetry.instrumentation.fastapi": mock_instrumentor,
        "opentelemetry.sdk.resources": mock_resource,
        "opentelemetry.sdk.trace": mock_provider,
        "opentelemetry.sdk.trace.export": mock_processor,
        "opentelemetry.exporter": MagicMock(__path__=[]),
        "opentelemetry.exporter.otlp": MagicMock(__path__=[]),
        "opentelemetry.exporter.otlp.proto": MagicMock(__path__=[]),
        "opentelemetry.exporter.otlp.proto.grpc": MagicMock(__path__=[]),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": mock_exporter,
    }

    with patch.dict("sys.modules", modules):
        mock_resource.Resource.create.return_value = "resource_obj"
        mock_provider.TracerProvider.return_value = mock_provider

        mock_exporter.OTLPSpanExporter = MagicMock()
        mock_processor.BatchSpanProcessor = MagicMock()

        from tenxgraph_api.src.app.core.config.setup_middleware import _setup_otel

        _setup_otel(MagicMock(), settings)

        mock_resource.Resource.create.assert_called_once_with({"service.name": "test-service"})
        mock_provider.TracerProvider.assert_called_once_with(resource="resource_obj")
        mock_exporter.OTLPSpanExporter.assert_called_once_with(endpoint="http://localhost:4317")
        mock_provider.add_span_processor.assert_called_once()
        mock_trace.set_tracer_provider.assert_called_once_with(mock_provider)
        mock_instrumentor.FastAPIInstrumentor.instrument_app.assert_called_once()


def test_setup_otel_no_endpoint():
    settings = MagicMock()
    settings.OTEL_ENABLED = True
    settings.OTEL_SERVICE_NAME = "test-service"
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = None

    mock_trace = MagicMock()
    mock_instrumentor = MagicMock()
    mock_resource = MagicMock()
    mock_provider = MagicMock()
    mock_processor = MagicMock()

    modules = {
        "opentelemetry": mock_trace,
        "opentelemetry.trace": mock_trace,
        "opentelemetry.instrumentation.fastapi": mock_instrumentor,
        "opentelemetry.sdk.resources": mock_resource,
        "opentelemetry.sdk.trace": mock_provider,
        "opentelemetry.sdk.trace.export": mock_processor,
    }

    with patch.dict("sys.modules", modules):
        mock_resource.Resource.create.return_value = "resource_obj"
        mock_provider.TracerProvider.return_value = mock_provider
        mock_processor.ConsoleSpanExporter = MagicMock()
        mock_processor.SimpleSpanProcessor = MagicMock()

        from tenxgraph_api.src.app.core.config.setup_middleware import _setup_otel

        _setup_otel(MagicMock(), settings)

        mock_processor.ConsoleSpanExporter.assert_called_once()
        mock_processor.SimpleSpanProcessor.assert_called_once()
        mock_provider.add_span_processor.assert_called_once()


def test_setup_otel_no_endpoint_in_production_skips_console():
    settings = MagicMock()
    settings.OTEL_ENABLED = True
    settings.OTEL_SERVICE_NAME = "test-service"
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = None
    settings.MODE = "production"

    mock_trace = MagicMock()
    mock_instrumentor = MagicMock()
    mock_provider = MagicMock()
    mock_processor = MagicMock()

    modules = {
        "opentelemetry": mock_trace,
        "opentelemetry.trace": mock_trace,
        "opentelemetry.instrumentation.fastapi": mock_instrumentor,
        "opentelemetry.sdk.resources": MagicMock(),
        "opentelemetry.sdk.trace": mock_provider,
        "opentelemetry.sdk.trace.export": mock_processor,
    }

    with patch.dict("sys.modules", modules):
        from tenxgraph_api.src.app.core.config import setup_middleware as module

        with patch.object(module.logger, "warning") as warning:
            module._setup_otel(MagicMock(), settings)

        mock_processor.ConsoleSpanExporter.assert_not_called()
        mock_provider.TracerProvider.return_value.add_span_processor.assert_not_called()
        mock_trace.trace.set_tracer_provider.assert_not_called()
        mock_instrumentor.FastAPIInstrumentor.instrument_app.assert_not_called()
        assert "not exported" in warning.call_args.args[0]


def test_setup_otel_grpc_exporter_import_error():
    settings = MagicMock()
    settings.OTEL_ENABLED = True
    settings.OTEL_SERVICE_NAME = "test-service"
    settings.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317"

    mock_trace = MagicMock()
    mock_instrumentor = MagicMock()
    mock_resource = MagicMock()
    mock_provider = MagicMock()
    mock_processor = MagicMock()

    modules = {
        "opentelemetry": mock_trace,
        "opentelemetry.trace": mock_trace,
        "opentelemetry.instrumentation.fastapi": mock_instrumentor,
        "opentelemetry.sdk.resources": mock_resource,
        "opentelemetry.sdk.trace": mock_provider,
        "opentelemetry.sdk.trace.export": mock_processor,
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None,
    }

    with patch.dict("sys.modules", modules):
        mock_resource.Resource.create.return_value = "resource_obj"
        mock_provider.TracerProvider.return_value = mock_provider

        from tenxgraph_api.src.app.core.config.setup_middleware import _setup_otel

        _setup_otel(MagicMock(), settings)
        # Should return gracefully on ImportError of grpc exporter


def test_attach_otel_publisher_import_error():
    with patch.dict("sys.modules", {"tenxgraph.runtime.publisher.base_publisher": None}):
        from tenxgraph_api.src.app.core.config.setup_middleware import _attach_otel_publisher

        container = MagicMock()
        _attach_otel_publisher(container, MagicMock())


def test_attach_otel_publisher_value_error():
    class FakeObservabilityLevel:
        STANDARD = "standard"

        def __init__(self, val):
            raise ValueError("invalid level")

    class FakeOtelPublisher:
        def __init__(self, level):
            self.level = level

    class FakeBasePublisher:
        pass

    modules = {
        "tenxgraph.runtime.publisher.base_publisher": MagicMock(BasePublisher=FakeBasePublisher),
        "tenxgraph.runtime.publisher.composite_publisher": MagicMock(),
        "tenxgraph.runtime.publisher.otel_publisher": MagicMock(
            ObservabilityLevel=FakeObservabilityLevel, OtelPublisher=FakeOtelPublisher
        ),
    }
    with patch.dict("sys.modules", modules):
        from tenxgraph_api.src.app.core.config.setup_middleware import _attach_otel_publisher

        container = MagicMock()
        container.try_get.return_value = None
        settings = MagicMock()
        settings.OTEL_LEVEL = "invalid-level"
        _attach_otel_publisher(container, settings)
        container.bind_instance.assert_called_once()


def test_attach_otel_publisher_no_existing():
    class FakeObservabilityLevel:
        STANDARD = "standard"

        def __init__(self, val):
            self.val = val

    class FakeOtelPublisher:
        def __init__(self, level):
            self.level = level

    class FakeBasePublisher:
        pass

    modules = {
        "tenxgraph.runtime.publisher.base_publisher": MagicMock(BasePublisher=FakeBasePublisher),
        "tenxgraph.runtime.publisher.composite_publisher": MagicMock(),
        "tenxgraph.runtime.publisher.otel_publisher": MagicMock(
            ObservabilityLevel=FakeObservabilityLevel, OtelPublisher=FakeOtelPublisher
        ),
    }
    with patch.dict("sys.modules", modules):
        from tenxgraph_api.src.app.core.config.setup_middleware import _attach_otel_publisher

        container = MagicMock()
        container.try_get.return_value = None
        settings = MagicMock()
        settings.OTEL_LEVEL = "standard"
        _attach_otel_publisher(container, settings)
        container.bind_instance.assert_called_once()


def test_attach_otel_publisher_existing_composite():
    class FakeObservabilityLevel:
        STANDARD = "standard"

        def __init__(self, val):
            self.val = val

    class FakeOtelPublisher:
        def __init__(self, level):
            self.level = level

    class FakeBasePublisher:
        pass

    class FakeCompositePublisher:
        def __init__(self, publishers=None):
            self.publishers = publishers or []

        def add_publisher(self, pub):
            self.publishers.append(pub)

    existing = FakeCompositePublisher()

    modules = {
        "tenxgraph.runtime.publisher.base_publisher": MagicMock(BasePublisher=FakeBasePublisher),
        "tenxgraph.runtime.publisher.composite_publisher": MagicMock(
            CompositePublisher=FakeCompositePublisher
        ),
        "tenxgraph.runtime.publisher.otel_publisher": MagicMock(
            ObservabilityLevel=FakeObservabilityLevel, OtelPublisher=FakeOtelPublisher
        ),
    }
    with patch.dict("sys.modules", modules):
        from tenxgraph_api.src.app.core.config.setup_middleware import _attach_otel_publisher

        container = MagicMock()
        container.try_get.return_value = existing
        settings = MagicMock()
        settings.OTEL_LEVEL = "standard"

        with patch(
            "tenxgraph_api.src.app.core.config.setup_middleware.isinstance", return_value=True
        ):
            _attach_otel_publisher(container, settings)


def test_attach_otel_publisher_existing_single():
    class FakeObservabilityLevel:
        STANDARD = "standard"

        def __init__(self, val):
            self.val = val

    class FakeOtelPublisher:
        def __init__(self, level):
            self.level = level

    class FakeBasePublisher:
        pass

    class FakeCompositePublisher:
        def __init__(self, publishers=None):
            self.publishers = publishers or []

    class SinglePublisher:
        pass

    existing = SinglePublisher()

    modules = {
        "tenxgraph.runtime.publisher.base_publisher": MagicMock(BasePublisher=FakeBasePublisher),
        "tenxgraph.runtime.publisher.composite_publisher": MagicMock(
            CompositePublisher=FakeCompositePublisher
        ),
        "tenxgraph.runtime.publisher.otel_publisher": MagicMock(
            ObservabilityLevel=FakeObservabilityLevel, OtelPublisher=FakeOtelPublisher
        ),
    }
    with patch.dict("sys.modules", modules):
        from tenxgraph_api.src.app.core.config.setup_middleware import _attach_otel_publisher

        container = MagicMock()
        container.try_get.return_value = existing
        settings = MagicMock()
        settings.OTEL_LEVEL = "standard"

        _attach_otel_publisher(container, settings)
        container.bind_instance.assert_called_once()


class MockSettings:
    OTEL_ENABLED = True
    OTEL_SERVICE_NAME = "test-service"
    OTEL_EXPORTER_OTLP_ENDPOINT = None
    ORIGINS = "http://localhost,http://localhost:3000"
    ALLOWED_HOST = "localhost,127.0.0.1"
    MAX_REQUEST_SIZE = 1024 * 1024
    SECURITY_HEADERS_ENABLED = True
    HSTS_ENABLED = True
    HSTS_MAX_AGE = 31536000
    HSTS_INCLUDE_SUBDOMAINS = True
    HSTS_PRELOAD = True
    FRAME_OPTIONS = "DENY"
    CONTENT_TYPE_OPTIONS = "nosniff"
    XSS_PROTECTION = "1; mode=block"
    REFERRER_POLICY = "no-referrer"
    PERMISSIONS_POLICY = "geolocation=()"
    CSP_POLICY = "default-src 'self'"


def test_setup_middleware_all():
    from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware

    app = FastAPI()
    settings = MockSettings()

    graph_config = MagicMock()
    rate_limit_config = MagicMock()
    rate_limit_config.requests = 100
    rate_limit_config.window = 60
    rate_limit_config.by = "ip"
    rate_limit_config.exclude_paths = None
    rate_limit_config.trusted_proxy_headers = True
    graph_config.rate_limit = rate_limit_config

    container = MagicMock()

    with (
        patch(
            "tenxgraph_api.src.app.core.config.setup_middleware.get_settings", return_value=settings
        ),
        patch("tenxgraph_api.src.app.core.config.setup_middleware.init_sentry") as mock_init_sentry,
        patch("tenxgraph_api.src.app.core.config.setup_middleware._setup_otel") as mock_setup_otel,
        patch(
            "tenxgraph_api.src.app.core.config.setup_middleware._attach_otel_publisher"
        ) as mock_attach,
    ):
        setup_middleware(app, graph_config=graph_config, container=container)

        mock_init_sentry.assert_called_once_with(settings)
        rate_limit = [m for m in app.user_middleware if m.cls is RateLimitMiddleware]
        assert len(rate_limit) == 1
        assert rate_limit[0].kwargs == {"config": rate_limit_config, "container": container}
        assert not hasattr(app.state, "rate_limit_backend")
        mock_setup_otel.assert_called_once_with(app, settings)
        mock_attach.assert_called_once_with(container, settings, graph_config)


# ── _setup_observability ──────────────────────────────────────────────────────


def _obs_modules(*, setup_side_effect=None, existing=None, composite_cls=None):
    """Fake tenxgraph.runtime.publisher modules used by _setup_observability."""

    class FakeBasePublisher:
        pass

    class FakeCompositePublisher:
        def __init__(self, publishers=None):
            self.publishers = list(publishers or [])

        def add_publisher(self, pub):
            self.publishers.append(pub)

    class FakeObservabilityLevel:
        STANDARD = "standard"

        def __init__(self, val):
            if val not in ("standard", "spans", "full"):
                raise ValueError(f"bad level {val}")
            self.val = val

        def __repr__(self):
            return f"level:{self.val}"

    class FakeOtelPublisher:
        def __init__(self, level=None):
            self.level = level

    setup_observability = MagicMock(side_effect=setup_side_effect)

    modules = {
        "tenxgraph.runtime.publisher.base_publisher": MagicMock(BasePublisher=FakeBasePublisher),
        "tenxgraph.runtime.publisher.composite_publisher": MagicMock(
            CompositePublisher=composite_cls or FakeCompositePublisher
        ),
        "tenxgraph.runtime.publisher.exporters": MagicMock(setup_observability=setup_observability),
        "tenxgraph.runtime.publisher.otel_publisher": MagicMock(
            ObservabilityLevel=FakeObservabilityLevel,
            OtelPublisher=FakeOtelPublisher,
        ),
    }
    return modules, setup_observability, FakeBasePublisher, FakeCompositePublisher


def _graph_config(observability):
    gc = MagicMock()
    gc.observability = observability
    return gc


def test_setup_observability_noop_when_container_none():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    # Should not raise when there is nothing to wire.
    _setup_observability(None, _graph_config({"logfire": {"enabled": True}}))


def test_setup_observability_noop_when_no_block():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    container = MagicMock()
    _setup_observability(container, _graph_config(None))
    container.bind_instance.assert_not_called()


def test_setup_observability_noop_when_no_backend_enabled():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    container = MagicMock()
    cfg = {"logfire": {"enabled": False}, "langsmith": {"enabled": False}}
    _setup_observability(container, _graph_config(cfg))
    container.bind_instance.assert_not_called()


def test_setup_observability_binds_when_none_existing():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    modules, setup_obs, _base, _comp = _obs_modules()
    cfg = {"level": "standard", "logfire": {"enabled": True, "service_name": "svc"}}
    container = MagicMock()
    container.try_get.return_value = None

    with patch.dict(sys.modules, modules):
        _setup_observability(container, _graph_config(cfg))

    # provider config invoked in graph=None (provider-only) mode
    setup_obs.assert_called_once()
    assert setup_obs.call_args[0][0] is None
    assert setup_obs.call_args[0][1] == cfg
    container.bind_instance.assert_called_once()


def test_setup_observability_adds_to_existing_composite():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    modules, _setup_obs, _base, comp_cls = _obs_modules()
    existing = comp_cls()
    cfg = {"langsmith": {"enabled": True, "project": "p"}}
    container = MagicMock()
    container.try_get.return_value = existing

    with patch.dict(sys.modules, modules):
        _setup_observability(container, _graph_config(cfg))

    assert len(existing.publishers) == 1
    container.bind_instance.assert_not_called()


def test_setup_observability_wraps_single_existing():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    modules, _setup_obs, _base, _comp = _obs_modules()

    class SinglePublisher:
        pass

    cfg = {"logfire": {"enabled": True}}
    container = MagicMock()
    container.try_get.return_value = SinglePublisher()

    with patch.dict(sys.modules, modules):
        _setup_observability(container, _graph_config(cfg))

    container.bind_instance.assert_called_once()


def test_setup_observability_skips_on_value_error():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    # setup_observability raising ValueError (e.g. LangSmith key missing) must
    # not crash startup and must not bind a publisher.
    modules, _setup_obs, _base, _comp = _obs_modules(
        setup_side_effect=ValueError("LANGSMITH_API_KEY not set")
    )
    cfg = {"langsmith": {"enabled": True}}
    container = MagicMock()
    container.try_get.return_value = None

    with patch.dict(sys.modules, modules):
        _setup_observability(container, _graph_config(cfg))

    container.bind_instance.assert_not_called()


def test_setup_observability_import_error_warns():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    cfg = {"logfire": {"enabled": True}}
    container = MagicMock()

    with patch.dict(sys.modules, {"tenxgraph.runtime.publisher.base_publisher": None}):
        _setup_observability(container, _graph_config(cfg))

    container.bind_instance.assert_not_called()


def test_setup_observability_invalid_level_falls_back():
    from tenxgraph_api.src.app.core.config.setup_middleware import _setup_observability

    modules, _setup_obs, _base, _comp = _obs_modules()
    cfg = {"level": "bogus", "logfire": {"enabled": True}}
    container = MagicMock()
    container.try_get.return_value = None

    with patch.dict(sys.modules, modules):
        _setup_observability(container, _graph_config(cfg))

    # Falls back to STANDARD and still binds.
    container.bind_instance.assert_called_once()


def test_user_id_mode_reads_the_observability_block():
    cfg = MagicMock()
    cfg.observability = {"user_id_mode": "hash"}
    assert setup_middleware_module._user_id_mode(cfg) == "hash"
    cfg.observability = None
    assert setup_middleware_module._user_id_mode(cfg) == "raw"
    assert setup_middleware_module._user_id_mode(None) == "raw"


def test_make_otel_publisher_passes_the_mode_only_when_set():
    calls = []

    class NewCorePublisher:
        def __init__(self, level, user_id_mode="raw"):
            calls.append((level, user_id_mode))

    setup_middleware_module._make_otel_publisher(NewCorePublisher, "standard", "raw")
    setup_middleware_module._make_otel_publisher(NewCorePublisher, "standard", "mask")
    assert calls == [("standard", "raw"), ("standard", "mask")]


def test_make_otel_publisher_falls_back_on_an_older_core():
    class OldCorePublisher:
        def __init__(self, level):
            self.level = level

    publisher = setup_middleware_module._make_otel_publisher(OldCorePublisher, "standard", "hash")
    assert publisher.level == "standard"
