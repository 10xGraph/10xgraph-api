from fastapi import Request
from starlette.requests import Request as StarletteRequest

from tenxgraph_api.src.app.utils.response_helper import (
    error_response,
    merge_metadata,
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






def test_error_response_without_request_id_middleware():
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    req: Request = StarletteRequest(scope, DummyReceive())  # type: ignore[call-arg]
    meta = merge_metadata(None, req, "m")
    assert meta["request_id"] == "unknown"
    assert meta["timestamp"]
    resp = error_response(req, error_code="APP_ERROR", message="boom", status_code=400)
    assert resp.status_code == 400
    assert b"unknown" in resp.body
