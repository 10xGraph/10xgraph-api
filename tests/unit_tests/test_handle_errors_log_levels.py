"""B34: handled client errors log at WARNING without a traceback; server faults keep ERROR."""

import logging
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException
from tenxgraph.core.exceptions import GraphError, SchemaVersionError

from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.core.exceptions.resources_exceptions import ResourceNotFoundError
from tenxgraph_api.src.app.core.exceptions.user_exception import (
    UserAccountError,
    UserPermissionError,
)


def _client(raiser, mode: str = "development") -> TestClient:
    os.environ["MODE"] = mode
    if mode == "production":
        os.environ["ALLOWED_HOST"] = "api.example.com"
        os.environ["ORIGINS"] = "https://app.example.com"
    from tenxgraph_api.src.app.core.config.settings import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    init_errors_handler(app)

    @app.get("/boom/{item}")
    async def boom(item: int):
        raise raiser()

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_mode():
    yield
    os.environ.pop("MODE", None)
    os.environ.pop("ALLOWED_HOST", None)
    os.environ.pop("ORIGINS", None)
    from tenxgraph_api.src.app.core.config.settings import get_settings

    get_settings.cache_clear()


def _records(caplog):
    return [r for r in caplog.records if r.name == "10xgraph-api"]


@pytest.mark.parametrize(
    ("raiser", "status"),
    [
        (lambda: HTTPException(404, "missing"), 404),
        (lambda: HTTPException(401, "bad token"), 401),
        (lambda: HTTPException(403, "no scope"), 403),
        (lambda: HTTPException(429, "slow down"), 429),
        (lambda: ResourceNotFoundError(), 404),
        (lambda: UserPermissionError(), 403),
        (lambda: UserAccountError(status_code=401), 401),
        (lambda: ValueError("bad value"), 422),
        (lambda: SchemaVersionError("old"), 422),
    ],
)
@pytest.mark.parametrize("mode", ["development", "production"])
def test_client_errors_log_warning_without_traceback(caplog, raiser, status, mode):
    client = _client(raiser, mode)
    with caplog.at_level(logging.DEBUG):
        response = client.get("/boom/1")

    assert response.status_code == status
    records = _records(caplog)
    assert records
    assert not [r for r in records if r.levelno >= logging.ERROR]
    assert all(r.exc_info is None for r in records)
    assert "Traceback" not in caplog.text


def test_validation_error_logs_warning_without_traceback(caplog):
    client = _client(lambda: HTTPException(404, "x"))
    with caplog.at_level(logging.DEBUG):
        response = client.get("/boom/not-an-int")

    assert response.status_code == 422
    records = _records(caplog)
    assert not [r for r in records if r.levelno >= logging.ERROR]
    assert all(r.exc_info is None for r in records)


def test_client_error_log_line_has_method_path_status_code(caplog):
    client = _client(lambda: HTTPException(404, "missing"))
    with caplog.at_level(logging.WARNING):
        client.get("/boom/7")

    messages = [r.getMessage() for r in _records(caplog)]
    assert any("GET /boom/7 -> 404 HTTPException" in m for m in messages)


def test_path_is_escaped_in_log_line(caplog):
    client = _client(lambda: HTTPException(404, "missing"))
    with caplog.at_level(logging.WARNING):
        client.get("/boom/7%0AFAKE%20ENTRY")

    assert all("\n" not in r.getMessage() for r in _records(caplog))


def test_http_500_logs_error_with_traceback(caplog):
    client = _client(lambda: HTTPException(500, "server broke"))
    with caplog.at_level(logging.DEBUG):
        response = client.get("/boom/1")

    assert response.status_code == 500
    errors = [r for r in _records(caplog) if r.levelno == logging.ERROR]
    assert errors
    assert any(r.exc_info for r in errors)


def test_graph_error_logs_error_with_traceback(caplog):
    client = _client(lambda: GraphError("broken"))
    with caplog.at_level(logging.DEBUG):
        response = client.get("/boom/1")

    assert response.status_code == 500
    assert any(r.levelno == logging.ERROR and r.exc_info for r in _records(caplog))
