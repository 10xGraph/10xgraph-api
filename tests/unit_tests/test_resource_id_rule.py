"""One rule for client-named ids (thread, message, memory): G-127."""

# ruff: noqa: S101

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi

from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.request_config import resource_id_error
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler


class _Auth(BaseAuth):
    def authenticate(self, request, response, credential):
        return {"user_id": "alice"}


class _AuthOn:
    introspection_scoped = False

    def auth_config(self):
        return {"method": "custom", "path": "x:y"}


def _client() -> TestClient:
    container = InjectQ()
    container.bind_instance(GraphConfig, _AuthOn())
    container.bind_instance(BaseAuth, _Auth())
    container.bind_instance(AuthorizationBackend, DefaultAuthorizationBackend())
    app = FastAPI()
    setup_fastapi(container, app)
    init_errors_handler(app)
    guard = Depends(RequirePermission("checkpointer", "read"))

    @app.get("/v1/threads/{thread_id}/messages/{message_id}")
    async def message(thread_id: str, message_id: str, user: dict[str, Any] = guard):
        return {"ok": True}

    @app.get("/v1/store/memories/{memory_id}")
    async def memory(memory_id: str, user: dict[str, Any] = guard):
        return {"ok": True}

    @app.post("/v1/graph/stop")
    async def stop(body: dict[str, Any], user: dict[str, Any] = guard):
        return {"ok": True}

    return TestClient(app)


_H = {"Authorization": "Bearer t"}
BAD_IDS = ["a" * 129, "a b", "a/b", "a\n", "é"]
GOOD_IDS = [
    "1234567890123456789",  # Snowflake
    "3f2b8c1e-9d4a-4c1b-8f6e-0a1b2c3d4e5f",  # UUID
    "mem0:user@example.com_1.2-3",
    "a" * 128,
]


@pytest.mark.parametrize("value", GOOD_IDS)
def test_api_made_ids_pass(value):
    assert resource_id_error(value, "thread_id") is None
    assert resource_id_error(int(value) if value.isdigit() else value, "thread_id") is None


@pytest.mark.parametrize("value", [*BAD_IDS, "", ["x"], {"a": 1}])
def test_odd_ids_fail(value):
    assert resource_id_error(value, "thread_id")


def test_missing_id_is_not_this_rules_business():
    assert resource_id_error(None, "thread_id") is None


@pytest.mark.parametrize("value", ["a" * 129, "a b", "a;b"])
def test_bad_path_ids_are_422_validation_error(value):
    client = _client()
    for url in (
        f"/v1/threads/{value}/messages/m1",
        f"/v1/threads/t1/messages/{value}",
        f"/v1/store/memories/{value}",
    ):
        res = client.get(url, headers=_H)
        assert res.status_code == 422, url
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_good_path_ids_reach_the_route():
    client = _client()
    for value in GOOD_IDS:
        assert client.get(f"/v1/threads/{value}/messages/{value}", headers=_H).status_code == 200
        assert client.get(f"/v1/store/memories/{value}", headers=_H).status_code == 200


def test_bad_body_thread_id_is_422():
    client = _client()
    res = client.post("/v1/graph/stop", json={"thread_id": "x" * 5000}, headers=_H)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    res = client.post("/v1/graph/stop", json={"config": {"thread_id": "a b"}}, headers=_H)
    assert res.status_code == 422
    ok = client.post("/v1/graph/stop", json={"thread_id": GOOD_IDS[0]}, headers=_H)
    assert ok.status_code == 200
