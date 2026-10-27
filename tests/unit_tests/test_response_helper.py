from fastapi import Request
from starlette.requests import Request as StarletteRequest

from tenxgraph_api.src.app.utils.response_helper import (
    error_response,
    merge_metadata,
    success_response,
)


HTTP_OK = 200
HTTP_I_AM_A_TEAPOT = 418


class DummyReceive:
    async def __call__(self):  # type: ignore[override]
        return {"type": "http.request"}


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
    }
    req: Request = StarletteRequest(scope, DummyReceive())  # type: ignore[call-arg]
    # attach state
    req.state.request_id = "req-1"
    req.state.timestamp = "2025-01-01T00:00:00"
    return req


def test_success_response_contains_metadata():
    req = _make_request()
    resp = success_response({"ok": True}, req, message="OK")
    assert resp.status_code == HTTP_OK
    body = resp.body
    assert b"request_id" in body and b"timestamp" in body


def test_error_response_contains_error_fields():
    req = _make_request()
    resp = error_response(
        req,
        error_code="APP_ERROR",
        message="boom",
        status_code=HTTP_I_AM_A_TEAPOT,
    )
    assert resp.status_code == HTTP_I_AM_A_TEAPOT
    body = resp.body
    assert b"APP_ERROR" in body and b"boom" in body


def test_error_response_without_request_id_middleware():
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    req: Request = StarletteRequest(scope, DummyReceive())  # type: ignore[call-arg]
    meta = merge_metadata(None, req, "m")
    assert meta["request_id"] == "unknown"
    assert meta["timestamp"]
    resp = error_response(req, error_code="APP_ERROR", message="boom", status_code=400)
    assert resp.status_code == 400
    assert b"unknown" in resp.body
