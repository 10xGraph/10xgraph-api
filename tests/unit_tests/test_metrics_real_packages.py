"""End-to-end check with the real OpenTelemetry and Prometheus packages (skipped without them)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.prometheus")
pytest.importorskip("opentelemetry.instrumentation.fastapi")
prometheus_client = pytest.importorskip("prometheus_client")

from tenxgraph_api.src.app.core.config import metrics as metrics_module
from tenxgraph_api.src.app.core.config import setup_middleware as sm
from tenxgraph_api.src.app.core.config.metrics import MetricsConfig


HTTP_OK = 200


def _build_app():
    graph_config = MagicMock()
    graph_config.metrics = MetricsConfig(
        enabled=True, export="prometheus", prometheus={"path": "/_scrape"}
    )
    graph_config.rate_limit = None
    graph_config.observability = None
    settings = MagicMock()
    settings.OTEL_ENABLED = False
    settings.ORIGINS = "http://localhost"
    settings.CORS_ALLOW_CREDENTIALS = True
    settings.MODE = "development"
    settings.ALLOWED_HOST = "*"
    settings.SECURITY_HEADERS_ENABLED = False
    settings.MAX_REQUEST_SIZE = 1_000_000
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: int) -> dict:
        return {"id": item_id}

    with (
        patch.object(sm, "get_settings", return_value=settings),
        patch.object(sm, "init_sentry"),
        patch("tenxgraph.utils.metrics.setup_otel_metrics"),
    ):
        sm.setup_middleware(app, graph_config=graph_config, container=None)
    return app


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    metrics_module._shutdown_previous_provider()  # unregisters the Prometheus collector


def test_scrape_has_http_route_histogram_without_the_scrape_route():
    client = TestClient(_build_app())
    assert client.get("/items/7").status_code == HTTP_OK
    assert client.get("/_scrape").status_code == HTTP_OK  # not itself measured
    body = client.get("/_scrape").text

    assert "http_server_request_duration_seconds_bucket" in body
    assert 'http_route="/items/{item_id}"' in body
    assert "/_scrape" not in body


def test_building_twice_does_not_duplicate_families():
    TestClient(_build_app()).get("/items/1")  # the first app has data when the second is built
    TestClient(_build_app()).get("/items/1")
    text = prometheus_client.generate_latest(prometheus_client.REGISTRY).decode()
    helps = [
        line for line in text.splitlines() if line.startswith("# HELP http_server_request_duration")
    ]
    assert len(helps) == 1
