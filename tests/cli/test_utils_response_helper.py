from typing import Any

from fastapi import Request
from starlette.types import Scope

from tenxgraph_api.src.app.utils.response_helper import (
    error_response,
    merge_metadata,
    success_response,
)


class DummyReceive:
    async def __call__(self):  # pragma: no cover
        return {"type": "http.request"}


class DummySend:
    async def __call__(self, message):  # pragma: no cover
        pass


def _build_request() -> Request:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 8000),
        "server": ("127.0.0.1", 8000),
    }
    request = Request(scope, DummyReceive())
    # Simulate middleware populated state
    request.state.request_id = "req-123"
    request.state.timestamp = 1234567890
    return request


def test_merge_metadata_with_existing():
    request = _build_request()
    meta = {"extra": "value"}
    merged = merge_metadata(meta, request, "Hello")
    assert merged["request_id"] == "req-123"
    assert merged["timestamp"] == 1234567890
    assert merged["message"] == "Hello"
    assert merged["extra"] == "value"


def test_merge_metadata_without_existing():
    request = _build_request()
    merged = merge_metadata(None, request, "Msg")
    assert merged == {
        "request_id": "req-123",
        "timestamp": 1234567890,
        "message": "Msg",
    }


def test_success_response_default():
    request = _build_request()
    resp = success_response({"key": "val"}, request)
    assert resp.status_code == 200
    payload: dict[str, Any] = resp.body  # type: ignore[attr-defined]
    # starlette Response stores bytes; decode the JSON body
    import json

    data = json.loads(resp.body)
    assert data["data"] == {"key": "val"}
    assert data["metadata"]["request_id"] == "req-123"


def test_success_response_custom():
    request = _build_request()
    resp = success_response(
        [1, 2, 3], request, message="Created", status_code=201, metadata={"foo": "bar"}
    )
    assert resp.status_code == 201
    import json

    data = json.loads(resp.body)
    assert data["data"] == [1, 2, 3]
    assert data["metadata"]["foo"] == "bar"
    assert data["metadata"]["message"] == "Created"


def test_error_response_basic():
    request = _build_request()
    resp = error_response(request, error_code="BAD", message="Failure")
    assert resp.status_code == 400
    import json

    data = json.loads(resp.body)
    assert data["error"]["code"] == "BAD"
    assert data["error"]["message"] == "Failure"
    assert data["error"]["details"] == []


def test_error_response_with_details():
    request = _build_request()
    details = []  # Could add structured detail objects if schema expected
    resp = error_response(
        request,
        error_code="VALIDATION_ERROR",
        message="Invalid",
        details=details,
        status_code=422,
        metadata={"foo": "bar"},
    )
    assert resp.status_code == 422
    import json

    data = json.loads(resp.body)
    assert data["metadata"]["foo"] == "bar"
    assert data["error"]["code"] == "VALIDATION_ERROR"
