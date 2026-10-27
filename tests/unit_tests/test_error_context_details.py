"""Dev-mode error responses carry the exception context as ``details``."""

import pytest
from fastapi.testclient import TestClient
from tenxgraph.core.exceptions import (
    GraphError,
    GraphRecursionError,
    MetricsError,
    NodeError,
    SchemaVersionError,
    SerializationError,
    StorageError,
    TransientStorageError,
)

from .test_handle_errors import cleanup_env, setup_app


CASES = [
    (GraphError, "GRAPH_000", 500),
    (NodeError, "NODE_000", 500),
    (GraphRecursionError, "RECURSION_000", 500),
    (StorageError, "STORAGE_000", 500),
    (TransientStorageError, "STORAGE_TRANSIENT_000", 503),
    (MetricsError, "METRICS_000", 500),
    (SchemaVersionError, "STORAGE_SCHEMA_000", 422),
    (SerializationError, "STORAGE_SERIALIZATION_000", 500),
]


def _client(mode: str, exc_cls) -> TestClient:
    app = setup_app(mode)

    @app.get("/boom")
    async def boom():
        raise exc_cls("it broke", context={"node_name": "process_data", "attempt": 3})

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("exc_cls", "code", "status"), CASES)
def test_development_returns_real_status_with_context_details(exc_cls, code, status):
    try:
        response = _client("development", exc_cls).get("/boom")
    finally:
        cleanup_env()

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert {
        "loc": ["node_name"],
        "msg": "process_data",
        "type": "context",
    } in error["details"]
    assert {"loc": ["attempt"], "msg": "3", "type": "context"} in error["details"]


@pytest.mark.parametrize(("exc_cls", "code", "status"), CASES)
def test_production_hides_details(exc_cls, code, status):
    try:
        response = _client("production", exc_cls).get("/boom")
    finally:
        cleanup_env()

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["details"] == []
    assert "process_data" not in response.text


def test_development_empty_context_gives_no_details():
    app = setup_app("development")

    @app.get("/boom")
    async def boom():
        raise NodeError("it broke")

    try:
        response = TestClient(app, raise_server_exceptions=False).get("/boom")
    finally:
        cleanup_env()

    assert response.status_code == 500
    assert response.json()["error"]["details"] == []
