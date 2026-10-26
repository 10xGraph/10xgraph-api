import os

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
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
from tenxgraph.utils.validators import ValidationError

from tenxgraph_api.src.app.core.config.setup_middleware import setup_middleware
from tenxgraph_api.src.app.core.exceptions.handle_errors import (
    _sanitize_error_message,
    init_errors_handler,
)
from tenxgraph_api.src.app.core.exceptions.resources_exceptions import ResourceNotFoundError
from tenxgraph_api.src.app.core.exceptions.user_exception import (
    UserAccountError,
    UserPermissionError,
)


HTTP_NOT_FOUND = 404


def setup_app(mode: str = "development"):
    """Helper to set up app with specified mode."""
    os.environ["MODE"] = mode

    # In production, wildcard CORS combined with credentials is refused at
    # startup (it would reflect any Origin back as trusted+credentialed). A real
    # production deployment must declare its origins, so these tests do too.
    if mode == "production":
        os.environ["ORIGINS"] = "https://example.com"
    else:
        os.environ.pop("ORIGINS", None)

    from tenxgraph_api.src.app.core.config.settings import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    setup_middleware(app)
    init_errors_handler(app)
    return app


def cleanup_env():
    """Clean up environment variables."""
    if "MODE" in os.environ:
        del os.environ["MODE"]


def test_http_exception_handler_returns_error_payload():
    """Test HTTPException handler in development mode."""
    app = setup_app("development")

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=404, detail="nope")

    client = TestClient(app)
    r = client.get("/boom")
    assert r.status_code == HTTP_NOT_FOUND
    body = r.json()
    assert body["error"]["code"] == "HTTPException"
    assert body["error"]["message"] == "nope"

    cleanup_env()


def test_http_exception_handler_production_mode():
    """Production hides a 5xx detail but keeps a 4xx one."""
    app = setup_app("production")

    @app.get("/server")
    def server():
        raise HTTPException(status_code=500, detail="Internal details exposed")

    @app.get("/client/{code}")
    def client_error(code: int):
        raise HTTPException(status_code=code, detail=f"client message {code}")

    client = TestClient(app)
    r = client.get("/server")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "HTTPException"
    assert body["error"]["message"] != "Internal details exposed"

    for code in (401, 403, 404, 413, 422, 429):
        r = client.get(f"/client/{code}")
        assert r.status_code == code
        assert r.json()["error"]["message"] == f"client message {code}"

    for code in (502, 503, 504):
        r = client.get(f"/client/{code}")
        assert r.json()["error"]["message"] != f"client message {code}"

    cleanup_env()


def test_request_validation_error_handler_development():
    """Test RequestValidationError handler in development mode."""
    app = setup_app("development")

    @app.post("/test")
    def test_endpoint(value: int):
        return {"value": value}

    client = TestClient(app)
    r = client.post("/test", json={"value": "not_an_int"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"

    cleanup_env()


def test_request_validation_error_handler_production():
    """Test RequestValidationError handler sanitizes in production."""
    app = setup_app("production")

    @app.post("/test")
    def test_endpoint(value: int):
        return {"value": value}

    client = TestClient(app)
    r = client.post("/test", json={"value": "not_an_int"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    # In production, details should be empty or not present
    details = body["error"].get("details", [])
    if details:
        assert len(details) == 0

    cleanup_env()


def test_value_error_handler_development():
    """Test ValueError handler in development mode."""
    app = setup_app("development")

    @app.get("/value-error")
    def value_error():
        raise ValueError("Invalid value provided")

    client = TestClient(app)
    r = client.get("/value-error")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Invalid value provided"

    cleanup_env()


def test_value_error_handler_production():
    """Test ValueError handler sanitizes in production."""
    app = setup_app("production")

    @app.get("/value-error")
    def value_error():
        raise ValueError("Sensitive error details")

    client = TestClient(app)
    r = client.get("/value-error")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Invalid input provided."

    cleanup_env()


def test_user_account_error_handler():
    """Test UserAccountError handler."""
    app = setup_app("development")

    @app.get("/account-error")
    def account_error():
        raise UserAccountError(message="Account not found", error_code="ACCOUNT_001")

    client = TestClient(app)
    r = client.get("/account-error")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "ACCOUNT_001"
    assert body["error"]["message"] == "Account not found"

    cleanup_env()


def test_user_permission_error_handler():
    """Test UserPermissionError handler."""
    app = setup_app("development")

    @app.get("/permission-error")
    def permission_error():
        raise UserPermissionError(message="Permission denied")

    client = TestClient(app)
    r = client.get("/permission-error")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "PERMISSION_ERROR"
    assert body["error"]["message"] == "Permission denied"

    cleanup_env()


def test_resource_not_found_error_handler():
    """Test ResourceNotFoundError handler."""
    app = setup_app("development")

    @app.get("/not-found")
    def not_found():
        raise ResourceNotFoundError(message="Resource not found")

    client = TestClient(app)
    r = client.get("/not-found")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["error"]["message"] == "Resource not found"

    cleanup_env()


def test_validation_error_handler_development():
    """Test tenxgraph ValidationError handler in development."""
    app = setup_app("development")

    @app.get("/validation-error")
    def validation_error():
        raise ValidationError("Invalid data", "INVALID_FORMAT")

    client = TestClient(app)
    r = client.get("/validation-error")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "AGENTFLOW_VALIDATION_ERROR"

    cleanup_env()


def test_validation_error_handler_production():
    """Test tenxgraph ValidationError handler in production."""
    app = setup_app("production")

    @app.get("/validation-error")
    def validation_error():
        raise ValidationError("Invalid data details", "INVALID_FORMAT")

    client = TestClient(app)
    r = client.get("/validation-error")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "AGENTFLOW_VALIDATION_ERROR"

    cleanup_env()


def test_graph_error_handler_development():
    """Test GraphError handler in development mode."""
    app = setup_app("development")

    @app.get("/graph-error")
    def graph_error():
        raise GraphError("Graph failed", error_code="GRAPH_001")

    client = TestClient(app)
    r = client.get("/graph-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "GRAPH_001"
    assert body["error"]["message"] == "Graph failed"

    cleanup_env()


def test_graph_error_handler_production():
    """Test GraphError handler sanitizes in production."""
    app = setup_app("production")

    @app.get("/graph-error")
    def graph_error():
        raise GraphError("Graph execution failed with details", error_code="GRAPH_001")

    client = TestClient(app)
    r = client.get("/graph-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "GRAPH_001"

    cleanup_env()


def test_node_error_handler_development():
    """Test NodeError handler in development mode."""
    app = setup_app("development")

    @app.get("/node-error")
    def node_error():
        raise NodeError("Node failed", error_code="NODE_001")

    client = TestClient(app)
    r = client.get("/node-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "NODE_001"

    cleanup_env()


def test_graph_recursion_error_handler():
    """Test GraphRecursionError handler."""
    app = setup_app("development")

    @app.get("/recursion-error")
    def recursion_error():
        raise GraphRecursionError("Recursion limit exceeded", error_code="GRAPH_RECURSION_001")

    client = TestClient(app)
    r = client.get("/recursion-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "GRAPH_RECURSION_001"

    cleanup_env()


def test_storage_error_handler():
    """Test StorageError handler."""
    app = setup_app("development")

    @app.get("/storage-error")
    def storage_error():
        raise StorageError("Cannot access storage", error_code="STORAGE_001")

    client = TestClient(app)
    r = client.get("/storage-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "STORAGE_001"

    cleanup_env()


def test_transient_storage_error_handler():
    """Test TransientStorageError handler."""
    app = setup_app("development")

    @app.get("/transient-storage-error")
    def transient_storage_error():
        raise TransientStorageError(
            "Storage temporarily unavailable", error_code="TRANSIENT_STORAGE_001"
        )

    client = TestClient(app)
    r = client.get("/transient-storage-error")
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "TRANSIENT_STORAGE_001"

    cleanup_env()


def test_metrics_error_handler():
    """Test MetricsError handler."""
    app = setup_app("development")

    @app.get("/metrics-error")
    def metrics_error():
        raise MetricsError("Cannot collect metrics", error_code="METRICS_001")

    client = TestClient(app)
    r = client.get("/metrics-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "METRICS_001"

    cleanup_env()


def test_schema_version_error_handler():
    """Test SchemaVersionError handler."""
    app = setup_app("development")

    @app.get("/schema-version-error")
    def schema_version_error():
        raise SchemaVersionError("Incompatible schema version", error_code="SCHEMA_VERSION_001")

    client = TestClient(app)
    r = client.get("/schema-version-error")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "SCHEMA_VERSION_001"

    cleanup_env()


def test_serialization_error_handler():
    """Test SerializationError handler."""
    app = setup_app("development")

    @app.get("/serialization-error")
    def serialization_error():
        raise SerializationError("Cannot serialize data", error_code="SERIALIZATION_001")

    client = TestClient(app)
    r = client.get("/serialization-error")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "SERIALIZATION_001"

    cleanup_env()


def test_sanitize_error_message_development():
    """Test _sanitize_error_message returns full message in development."""
    message = "Detailed error message"
    result = _sanitize_error_message(message, "GRAPH_001", False)
    assert result == "Detailed error message"


def test_sanitize_error_message_production():
    """Test _sanitize_error_message sanitizes in production."""
    result = _sanitize_error_message("Detailed error", "GRAPH_001", True)
    assert result == "An error occurred executing the graph."

    result = _sanitize_error_message("Detailed error", "NODE_001", True)
    assert result == "An error occurred in a graph node."

    result = _sanitize_error_message("Detailed error", "STORAGE_001", True)
    assert result == "An error occurred accessing storage."

    result = _sanitize_error_message("Detailed error", "VALIDATION_ERROR", True)
    assert result == "The request data is invalid. Please check your input."


def test_sanitize_error_message_unknown_code():
    """Test _sanitize_error_message returns generic message for unknown codes."""
    result = _sanitize_error_message("Detailed error", "UNKNOWN_ERROR", True)
    assert result == "An unexpected error occurred. Please contact support."


def test_client_input_error_in_a_validator_keeps_its_message_in_production():
    """A request size limit is written for the client, so production must not hide it."""
    from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema

    app = setup_app("production")

    @app.post("/run")
    async def run(payload: GraphInputSchema):
        return {"ok": True}

    client = TestClient(app)
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}] * 501
    r = client.post("/run", json={"messages": messages})
    assert r.status_code == 422
    assert "at most 500 messages" in r.json()["error"]["message"]

    r = client.post("/run", json={"messages": "not a list"})
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "The request data is invalid. Please check your input."

    cleanup_env()
