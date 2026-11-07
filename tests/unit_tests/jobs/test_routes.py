"""The /v1/tasks routes on an in-memory broker, with a token auth and a fake schedule source."""

# ruff: noqa: S101

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from injectq.integrations.taskiq import setup_taskiq
from taskiq import InMemoryBroker, ScheduledTask, ScheduleSource

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.middleware import TaskMiddleware, wrap_tasks
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.route_guard import _iter_routes, find_unprotected_routes
from tenxgraph_api.src.app.core.cache_store import BaseCacheStore, InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.routers.setup_router import init_routes
from tenxgraph_api.src.app.routers.tasks import boot


routes = importlib.import_module("tenxgraph_api.src.app.routers.tasks.router")

ADD = "tests.add"
SECRET = "tests.secret"


class TokenAuth(BaseAuth):
    """The bearer token is the user id."""

    def authenticate(self, request, response, credential):
        return {"user_id": credential.credentials} if credential else {}


class AuthOn:
    def auth_config(self):
        return {"method": "custom", "path": "x:y"}


class FakeSource(ScheduleSource):
    def __init__(self) -> None:
        self.schedules: dict[str, ScheduledTask] = {}

    async def get_schedules(self) -> list[ScheduledTask]:
        return list(self.schedules.values())

    async def add_schedule(self, schedule: ScheduledTask) -> None:
        self.schedules[schedule.schedule_id] = schedule

    async def delete_schedule(self, schedule_id: str) -> None:
        self.schedules.pop(schedule_id, None)


class Env:
    def __init__(self, monkeypatch, auth: bool = True) -> None:
        self.store = InMemoryCacheStore()
        self.source = FakeSource()
        container = InjectQ()
        container.bind_instance(GraphConfig, AuthOn())
        container.bind_instance(BaseAuth, TokenAuth() if auth else None, allow_none=True)
        container.bind_instance(BaseCacheStore, self.store)
        self.broker = InMemoryBroker(await_inplace=True).with_middlewares(TaskMiddleware())
        self.seen: list[dict] = []

        @self.broker.task(task_name=ADD, http=True)
        async def add(a: int, b: int, config: dict) -> int:
            self.seen.append(config)
            return a + b

        @self.broker.task(task_name=SECRET)
        async def secret() -> str:
            return "no"

        setup_taskiq(container, self.broker)
        wrap_tasks(self.broker)
        monkeypatch.setattr(routes, "broker", self.broker)
        monkeypatch.setattr(routes, "schedule_source", self.source)

        app = FastAPI()
        setup_fastapi(container, app)
        init_errors_handler(app)
        app.include_router(routes.router)
        self.app = app
        self.client = TestClient(app)

    def call(self, method: str, path: str, user: str = "u1", **kwargs: Any):
        headers = {"Authorization": f"Bearer {user}"}
        return self.client.request(method, path, headers=headers, **kwargs)


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


def test_run_then_read_status_and_result(env):
    resp = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [2], "kwargs": {"b": 3}})
    assert resp.status_code == 202
    task_id = resp.json()["data"]["task_id"]

    data = env.call("GET", f"/v1/tasks/{task_id}").json()["data"]
    assert data["status"] == "success"
    assert data["result"] == 5
    assert data["error"] is None
    assert data["owner"] == "u1"
    # The run config is built on the server from the caller's Identity.
    config = env.seen[0]
    assert config["user_id"] == "u1"
    assert config["task_id"] == task_id
    assert config["user"]["user_id"] == "u1"


def test_auth_off_runs_as_anonymous(monkeypatch):
    off = Env(monkeypatch, auth=False)
    task_id = off.client.post(f"/v1/tasks/{ADD}", json={"args": [1, 1]}).json()["data"]["task_id"]
    data = off.client.get(f"/v1/tasks/{task_id}").json()["data"]
    assert data["owner"] == "anonymous"
    assert off.seen[0]["user_id"] == "anonymous"
    assert "user" not in off.seen[0]


def test_task_without_http_label_and_unknown_task_are_404(env):
    assert env.call("POST", f"/v1/tasks/{SECRET}", json={}).status_code == 404
    assert env.call("POST", "/v1/tasks/nope", json={}).status_code == 404


@pytest.mark.parametrize("kwargs", [{"a": 1, "b": 2, "user_id": "u2"}, {"config": {}}])
def test_reserved_argument_is_422(env, kwargs):
    resp = env.call("POST", f"/v1/tasks/{ADD}", json={"kwargs": kwargs})
    assert resp.status_code == 422
    assert "set by the server" in resp.json()["error"]["message"]


def test_too_many_positional_args_is_422(env):
    resp = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [1, 2, {}]})
    assert resp.status_code == 422


def test_other_user_gets_404_on_task(env):
    task_id = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [1, 1]}).json()["data"]["task_id"]
    assert env.call("GET", f"/v1/tasks/{task_id}", user="u2").status_code == 404
    assert env.call("POST", f"/v1/tasks/{task_id}/cancel", user="u2").status_code == 404
    assert env.call("GET", "/v1/tasks/missing").status_code == 404


def test_cancel_a_queued_task(env, monkeypatch):
    async def no_worker(message):  # the message waits in the queue
        return None

    monkeypatch.setattr(env.broker, "kick", no_worker)
    task_id = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [1, 1]}).json()["data"]["task_id"]
    assert env.call("GET", f"/v1/tasks/{task_id}").json()["data"]["status"] == "queued"
    resp = env.call("POST", f"/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"task_id": task_id, "status": "cancelled"}


def test_schedule_create_read_patch_cancel(env):
    body = {"args": [1, 2], "schedule": {"every": 3}}
    resp = env.call("POST", f"/v1/tasks/{ADD}", json=body)
    assert resp.status_code == 202
    schedule_id = resp.json()["data"]["schedule_id"]
    added = env.source.schedules[schedule_id]
    assert added.interval == 3
    assert added.args == [1, 2]
    label = json.loads(added.labels[state.CONFIG_LABEL])
    assert label["user_id"] == "u1"
    assert label["user"]["user_id"] == "u1"

    data = env.call("GET", f"/v1/tasks/{schedule_id}").json()["data"]
    assert data["schedule"]["every"] == 3
    assert data["task_name"] == ADD
    assert "config" not in data

    resp = env.call("PATCH", f"/v1/tasks/{schedule_id}", json={"schedule": {"cron": "*/5 * * * *"}})
    assert resp.status_code == 200
    changed = env.source.schedules[schedule_id]
    assert changed.cron == "*/5 * * * *"
    assert changed.interval is None
    assert changed.args == [1, 2]

    resp = env.call("POST", f"/v1/tasks/{schedule_id}/cancel")
    assert resp.json()["data"] == {"schedule_id": schedule_id, "deleted": True}
    assert env.source.schedules == {}
    assert env.call("GET", f"/v1/tasks/{schedule_id}").status_code == 404


def test_schedule_at_a_time(env):
    body = {"schedule": {"at": "2030-01-01T00:00:00Z"}, "args": [1, 2]}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    assert env.source.schedules[schedule_id].time is not None


def test_other_user_gets_404_on_schedule(env):
    body = {"args": [1, 2], "schedule": {"every": 3}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    patch = {"schedule": {"every": 5}}
    assert env.call("GET", f"/v1/tasks/{schedule_id}", user="u2").status_code == 404
    assert env.call("PATCH", f"/v1/tasks/{schedule_id}", user="u2", json=patch).status_code == 404
    assert env.call("POST", f"/v1/tasks/{schedule_id}/cancel", user="u2").status_code == 404
    assert env.source.schedules[schedule_id].interval == 3


@pytest.mark.parametrize(
    "schedule",
    [{}, {"every": 3, "cron": "* * * * *"}, {"every": 0}, {"cron": "not cron"}, {"in": 3}],
)
def test_bad_schedule_is_422(env, schedule):
    resp = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [1, 2], "schedule": schedule})
    assert resp.status_code == 422
    assert env.source.schedules == {}


def test_schedule_without_redis_source_is_501(env, monkeypatch):
    monkeypatch.setattr(routes, "schedule_source", None)
    body = {"args": [1, 2], "schedule": {"every": 3}}
    resp = env.call("POST", f"/v1/tasks/{ADD}", json=body)
    assert resp.status_code == 501
    assert "Scheduling needs Redis" in resp.json()["error"]["message"]


def test_routes_only_with_worker_and_all_guarded():
    def paths(app: FastAPI) -> set[str]:  # include_router is lazy: walk like the guard does
        return {path for path, _, _ in _iter_routes(app.routes)}

    without = FastAPI()
    init_routes(without)
    assert not [path for path in paths(without) if path.startswith("/v1/tasks")]

    with_tasks = FastAPI()
    init_routes(with_tasks, tasks=True)
    expected = {"/v1/tasks/{name}", "/v1/tasks/{task_id}", "/v1/tasks/{task_id}/cancel"}
    assert expected <= paths(with_tasks)
    assert find_unprotected_routes(with_tasks) == []


def test_tasks_permissions_are_known():
    from tenxgraph_api.src.app.core.auth.permissions_registry import ALL_PERMISSIONS

    assert {"tasks:trigger", "tasks:read", "tasks:cancel"} <= set(ALL_PERMISSIONS)


def test_boot_imports_modules_and_globs(tmp_path, monkeypatch):
    (tmp_path / "pkg_tasks_a.py").write_text("VALUE = 1\n")
    (tmp_path / "jobs_dir").mkdir()
    (tmp_path / "jobs_dir" / "b_tasks.py").write_text("VALUE = 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    boot._import_task_modules(str(tmp_path / "10xgraph.json"), ["pkg_tasks_a", "jobs_dir/*.py"])
    assert importlib.import_module("pkg_tasks_a").VALUE == 1
    assert importlib.import_module("jobs_dir.b_tasks").VALUE == 2
