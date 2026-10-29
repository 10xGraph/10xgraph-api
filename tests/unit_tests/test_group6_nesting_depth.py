"""G-128: deeply nested client state is refused with VALIDATION_ERROR before the run starts."""

# ruff: noqa: S101

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tenxgraph_api.src.app.core.auth.request_config import MAX_NESTING_DEPTH, nesting_depth_error
from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema


def _chain(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(depth):
        value = {"k": value}
    return value


MSG = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_depth_check_does_not_recurse() -> None:
    assert nesting_depth_error(_chain(50_000), "initial_state") is not None


def test_depth_at_limit_is_accepted() -> None:
    assert nesting_depth_error(_chain(MAX_NESTING_DEPTH - 1), "x") is None
    assert nesting_depth_error(_chain(MAX_NESTING_DEPTH), "x") is not None


@pytest.mark.parametrize("field", ["initial_state", "config"])
def test_schema_refuses_deep_dicts(field: str) -> None:
    GraphInputSchema(messages=MSG, **{field: _chain(10)})
    with pytest.raises(ValidationError, match="levels deep"):
        GraphInputSchema(messages=MSG, **{field: _chain(5000)})


def test_schema_refuses_deep_resume() -> None:
    with pytest.raises(ValidationError, match="levels deep"):
        GraphInputSchema(resume=[_chain(5000)])


def test_route_answers_422_validation_error() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    init_errors_handler(app)

    @app.post("/t")
    async def t(body: GraphInputSchema):
        return {"ok": True}

    client = TestClient(app)
    ok = client.post("/t", json={"messages": MSG, "initial_state": _chain(10)})
    assert ok.status_code == 200
    resp = client.post("/t", json={"messages": MSG, "initial_state": _chain(500)})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
