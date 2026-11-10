"""LOG_FORMAT=json output, request context on log lines, and LOG_FORMAT validation."""

# ruff: noqa: S101

import io
import json
import logging

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from tenxgraph_api.src.app.core.config.settings import Settings
from tenxgraph_api.src.app.core.config.setup_logs import (
    JsonFormatter,
    RequestContextFilter,
)
from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
from tenxgraph_api.src.app.core.request_context import get_request_context, set_request_user
from tenxgraph_api.src.app.core.utils.log_sanitizer import SanitizingFormatter


def _logger(formatter: logging.Formatter, name: str):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SanitizingFormatter(formatter))
    handler.addFilter(RequestContextFilter())
    log = logging.getLogger(name)
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log, stream


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_shape_and_exception():
    log, stream = _logger(JsonFormatter(), "test_json_shape")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed %s", "x")
    (entry,) = _lines(stream)
    assert entry["level"] == "ERROR"
    assert entry["logger"] == "test_json_shape"
    assert entry["message"] == "failed x"
    assert entry["ts"].endswith("Z") and len(entry["ts"]) == len("2026-01-01T00:00:00.000Z")
    assert "ValueError: boom" in entry["exc_info"]
    assert "request_id" not in entry


def test_json_mode_still_masks_secrets():
    log, stream = _logger(JsonFormatter(), "test_json_mask")
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    log.info("calling with api_key=%s", secret)
    out = stream.getvalue()
    assert secret not in out
    assert _lines(stream)[0]["message"] == "calling with api_key=sk-a...6789"
    # Text already baked into the message (f-strings) is redacted too.
    log.info(f"calling with api_key={secret}")
    assert "***API_KEY***" in _lines(stream)[1]["message"]


def _app():
    seen: list = []

    async def http(request):
        logging.getLogger("test_json_ctx").info("in http")
        seen.append(get_request_context())
        return PlainTextResponse("ok")

    async def ws(websocket):
        await websocket.accept()
        set_request_user("u-1")
        logging.getLogger("test_json_ctx").info("in ws")
        await websocket.close()

    app = Starlette(routes=[Route("/", http), WebSocketRoute("/ws", ws)])
    app.add_middleware(RequestIDMiddleware)
    return app, seen


def test_request_context_in_http_and_ws_log_lines():
    log, stream = _logger(JsonFormatter(), "test_json_ctx")
    app, seen = _app()
    client = TestClient(app)
    resp = client.get("/")
    with client.websocket_connect("/ws"):
        pass
    http_entry, ws_entry = _lines(stream)
    assert http_entry["request_id"] == resp.headers["X-Request-ID"]
    assert http_entry["transport"] == "http"
    assert ws_entry["transport"] == "ws"
    assert ws_entry["user_id"] == "u-1"
    assert ws_entry["request_id"] != http_entry["request_id"]
    assert seen[0].client_ip == "testclient"
    assert get_request_context() is None  # reset after the request


def test_text_mode_unchanged():
    fmt = logging.Formatter("%(name)s - %(levelname)s - %(message)s")
    log, stream = _logger(fmt, "test_text_mode")
    log.info("hello")
    assert stream.getvalue() == "test_text_mode - INFO - hello\n"


def test_log_format_validation():
    assert Settings(LOG_FORMAT="JSON").LOG_FORMAT == "json"
    assert Settings().LOG_FORMAT == "text"
    with pytest.raises(ValidationError, match="LOG_FORMAT"):
        Settings(LOG_FORMAT="xml")
