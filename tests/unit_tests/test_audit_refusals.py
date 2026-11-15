"""Audit events for refusals: validator rejection, oversized body, rate limit (429, 503)."""

# ruff: noqa: S101, PLR2004, SLF001

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tenxgraph.utils.validators import ValidationError

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.src.app.core.audit import set_sinks
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware, limiter
from tenxgraph_api.src.app.core.middleware.request_limits import RequestSizeLimitMiddleware


class _Recorder(BaseAuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def sink():
    recorder = _Recorder()
    set_sinks([recorder])
    yield recorder
    set_sinks([])


@pytest.fixture(autouse=True)
def _no_outage(monkeypatch):
    monkeypatch.setattr(limiter, "_failing", False)


def _summary(sink: _Recorder) -> list[tuple]:
    return [(e.event, e.result, e.reason) for e in sink.events]


def test_validator_rejection_emits_input_denied(sink):
    app = FastAPI()
    init_errors_handler(app)
    app.add_middleware(RequestIDMiddleware)

    @app.post("/run")
    async def run():
        raise ValidationError("secret prompt text", "content_policy")

    resp = TestClient(app).post("/run")
    assert resp.status_code == 422
    assert _summary(sink) == [("input", "denied", "validation")]
    assert "secret prompt text" not in sink.events[0].model_dump_json()
    assert sink.events[0].request_id is not None


def test_oversized_declared_body_emits_input_denied(sink):
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, max_size=10)
    app.add_middleware(RequestIDMiddleware)

    @app.post("/run")
    async def run():
        return {"ok": True}

    resp = TestClient(app).post("/run", content=b"x" * 50)
    assert resp.status_code == 413
    assert _summary(sink) == [("input", "denied", "request_too_large")]


def test_oversized_streamed_body_emits_one_event(sink):
    app = FastAPI()
    init_errors_handler(app)
    app.add_middleware(RequestSizeLimitMiddleware, max_size=10)
    app.add_middleware(RequestIDMiddleware)

    @app.post("/run")
    async def run(payload: dict):
        return payload

    def chunks():
        yield b'{"a": "'
        yield b"x" * 50
        yield b'"}'

    resp = TestClient(app).post("/run", content=chunks())
    assert resp.status_code == 413
    assert _summary(sink) == [("input", "denied", "request_too_large")]


class _BrokenStore(InMemoryCacheStore):
    async def acquire(self, key, limit, ttl):
        raise ConnectionError("down")


def _rate_app(cache, **overrides) -> TestClient:
    data = {"enabled": True, "requests": 1, "window": 60, "by": "ip"} | overrides
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=RateLimitConfig.from_dict(data), cache=cache)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return TestClient(app)


def test_rate_limit_429_emits_denied(sink):
    client = _rate_app(InMemoryCacheStore())
    assert client.get("/").status_code == 200
    assert _summary(sink) == []
    assert client.get("/").status_code == 429
    assert _summary(sink) == [("rate_limit", "denied", "limit_exceeded")]


def test_rate_limit_unavailable_503_emits_failed(sink):
    resp = _rate_app(_BrokenStore(), fail_open=False).get("/")
    assert resp.status_code == 503
    assert _summary(sink) == [("rate_limit", "failed", "unavailable")]


def test_denials_ignore_include_reads_false():
    recorder = _Recorder()
    set_sinks([recorder], include_reads=False)
    try:
        client = _rate_app(InMemoryCacheStore())
        client.get("/")
        client.get("/")
    finally:
        set_sinks([])
    assert _summary(recorder) == [("rate_limit", "denied", "limit_exceeded")]


def test_audit_off_changes_nothing():
    client = _rate_app(InMemoryCacheStore())
    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 429
