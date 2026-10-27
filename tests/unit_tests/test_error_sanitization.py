"""Unit tests for error message sanitization."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from tenxgraph_api.src.app.core.exceptions.handle_errors import (
    _sanitize_error_message,
    init_errors_handler,
)


def test_sanitize_error_message_in_production():
    """Test that error messages are sanitized in production."""
    # Test various error codes
    assert (
        _sanitize_error_message("Detailed internal error", "GRAPH_000", is_production=True)
        == "An error occurred executing the graph."
    )

    assert (
        _sanitize_error_message(
            "Database connection failed at 192.168.1.100", "STORAGE_001", is_production=True
        )
        == "An error occurred accessing storage."
    )

    assert (
        _sanitize_error_message(
            "Invalid field: user.password", "VALIDATION_ERROR", is_production=True
        )
        == "The request data is invalid. Please check your input."
    )


def test_sanitize_error_message_in_development():
    """Test that error messages are not sanitized in development."""
    detailed_message = "Detailed internal error with stack trace"

    result = _sanitize_error_message(detailed_message, "GRAPH_000", is_production=False)

    assert result == detailed_message


def test_sanitize_unknown_error_code():
    """Test sanitization of unknown error codes."""
    result = _sanitize_error_message("Some error", "UNKNOWN_ERROR_CODE", is_production=True)

    assert result == "An unexpected error occurred. Please contact support."


@pytest.fixture
def app_with_error_handlers():
    """Create a FastAPI app with error handlers."""
    app = FastAPI()

    # Add request ID middleware
    from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware

    app.add_middleware(RequestIDMiddleware)

    init_errors_handler(app)

    @app.get("/test-error")
    async def test_error():
        raise HTTPException(status_code=500, detail="Internal server error with details")

    @app.get("/test-validation")
    async def test_validation():
        raise ValueError("Invalid input: password field missing")

    return app


def test_http_exception_sanitized_in_production():
    """Test that HTTP exceptions are sanitized in production."""
    import os

    # Set environment before importing
    os.environ["MODE"] = "production"
    os.environ["ALLOWED_HOST"] = "api.example.com"
    os.environ["ORIGINS"] = "https://app.example.com"

    # Clear settings cache to pick up new environment
    from tenxgraph_api.src.app.core.config.settings import get_settings

    get_settings.cache_clear()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
    from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    init_errors_handler(app)

    @app.get("/test")
    async def test_endpoint():
        raise HTTPException(status_code=500, detail="Internal server error with details")

    client = TestClient(app)
    response = client.get("/test")

    assert response.status_code == 500
    error_data = response.json()

    # Message should be generic in production
    assert error_data["error"]["message"] == "An error occurred processing your request."

    # Cleanup
    del os.environ["MODE"]
    os.environ.pop("ALLOWED_HOST", None)
    os.environ.pop("ORIGINS", None)
    get_settings.cache_clear()


def test_http_exception_detailed_in_development():
    """Test that HTTP exceptions show details in development."""
    import os

    # Set environment before importing
    os.environ["MODE"] = "development"

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Clear settings cache
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
    from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler

    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    init_errors_handler(app)

    @app.get("/test")
    async def test_endpoint():
        raise HTTPException(status_code=500, detail="Internal server error with details")

    client = TestClient(app)
    response = client.get("/test")

    assert response.status_code == 500
    error_data = response.json()

    # In development, should show detailed message
    assert "Internal server error with details" in error_data["error"]["message"]

    # Cleanup
    del os.environ["MODE"]
    os.environ.pop("ALLOWED_HOST", None)
    os.environ.pop("ORIGINS", None)
    get_settings.cache_clear()


def test_validation_error_sanitized_in_production():
    """Test that validation errors are sanitized in production."""
    import os

    # Set environment before importing
    os.environ["MODE"] = "production"
    os.environ["ALLOWED_HOST"] = "api.example.com"
    os.environ["ORIGINS"] = "https://app.example.com"

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
    from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler

    # Clear cache
    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    init_errors_handler(app)

    @app.get("/test")
    async def test_endpoint():
        raise ValueError("Invalid input: password field missing")

    client = TestClient(app)
    response = client.get("/test")

    assert response.status_code == 422
    error_data = response.json()

    # Should show generic message in production
    assert error_data["error"]["message"] == "Invalid input provided."

    # Cleanup
    del os.environ["MODE"]
    os.environ.pop("ALLOWED_HOST", None)
    os.environ.pop("ORIGINS", None)


def test_error_response_includes_request_id(app_with_error_handlers):
    """Test that error responses include request ID."""
    client = TestClient(app_with_error_handlers)
    response = client.get("/test-error")

    assert "metadata" in response.json()
    assert "request_id" in response.json()["metadata"]


def test_all_error_code_prefixes_covered():
    """Test that all major error code prefixes have generic messages."""
    error_codes = [
        "VALIDATION_ERROR",
        "GRAPH_000",
        "NODE_000",
        "STORAGE_000",
        "METRICS_000",
        "SCHEMA_VERSION_000",
        "SERIALIZATION_000",
    ]

    for error_code in error_codes:
        result = _sanitize_error_message("Detailed error message", error_code, is_production=True)

        # Should not return the original message
        assert result != "Detailed error message"
        # Should return a generic message
        assert len(result) > 0
