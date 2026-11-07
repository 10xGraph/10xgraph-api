"""Jobs auth: reach on /v1/tasks, the worker's identity rebuild (A12) and system identities."""

# ruff: noqa: S101

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any

import pytest
from taskiq import TaskiqDepends
from taskiq.kicker import AsyncKicker

from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.context import TaskContext
from tenxgraph_api.jobs.middleware import wrap_tasks
from tenxgraph_api.src.app.core.auth.auth_config import AuthConfig
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth

from .test_routes import ADD, Env


ROLES = {
    "member": {"permissions": ["tasks:*"], "reach": "own"},
    "org_admin": {"inherits": ["member"], "reach": "tenant"},
    "root": {"permissions": ["*"], "reach": "all"},
    "reader": {"permissions": ["tasks:read"], "reach": "own"},
}
SYSTEMS = {
    "nightly": {"roles": ["member"], "tenant": {"org_id": "a"}},
    "readonly": {"roles": ["reader"], "tenant": {"org_id": "a"}},
}


def auth_config(roles: dict[str, Any] = ROLES, systems: dict[str, Any] = SYSTEMS) -> AuthConfig:
    return AuthConfig.model_validate(
        {
            "method": "custom",
            "path": "x:y",
            "tenant_claims": ["org_id"],
            "roles": roles,
            "system_identities": systems,
        }
    )


class Config:
    """Stands in for GraphConfig: only ``auth_config`` is read."""

    def __init__(self, auth: AuthConfig) -> None:
        self.auth = auth

    def auth_config(self) -> AuthConfig:
        return self.auth


class RoleAuth(BaseAuth):
    """The bearer token is ``user@org/role``."""

    def __init__(self, auth: AuthConfig) -> None:
        self.rbac = auth.rbac_config()

    def authenticate(self, request, response, credential):
        rest, _, role = credential.credentials.partition("/")
        user_id, _, org_id = rest.partition("@")
        return {"user_id": user_id, "org_id": org_id, "roles": [role]}


class Jobs:
    """The route Env with role auth, plus tasks with and without an ``identity`` label."""

    def __init__(self, monkeypatch, auth: bool = True) -> None:
        self.config = Config(auth_config())
        self.auth = RoleAuth(self.config.auth) if auth else None
        self.env = Env(monkeypatch, auth=auth, auth_impl=self.auth, graph_config=self.config)
        self.ran: list[dict] = []
        broker = self.env.broker

        @broker.task(task_name="tests.nightly", identity="nightly")
        async def nightly(config: dict) -> str:
            self.ran.append(config)
            return "done"

        @broker.task(task_name="tests.http_nightly", identity="nightly", http=True)
        async def http_nightly(config: dict) -> str:
            self.ran.append(config)
            return "done"

        @broker.task(task_name="tests.plain")
        async def plain(config: dict, ctx: Annotated[TaskContext, TaskiqDepends()]) -> str:
            self.ran.append(config)
            self.ran.append(ctx.config)
            return "done"

        self.nightly, self.http_nightly, self.plain = nightly, http_nightly, plain
        wrap_tasks(broker)

    def call(self, method: str, path: str, user: str, **kwargs: Any):
        return self.env.call(method, path, user=user, **kwargs)

    def run(self, user: str = "u1@a/member") -> str:
        resp = self.call("POST", f"/v1/tasks/{ADD}", user, json={"args": [1, 1]})
        assert resp.status_code == 202
        return resp.json()["data"]["task_id"]

    def schedule(self, user: str = "u1@a/member") -> str:
        body = {"args": [1, 2], "schedule": {"every": 3}}
        resp = self.call("POST", f"/v1/tasks/{ADD}", user, json=body)
        return resp.json()["data"]["schedule_id"]

    async def fire(self, schedule_id: str):
        """What the scheduler does on each tick: kick with the schedule's labels."""
        scheduled = self.env.source.schedules[schedule_id]
        kicker = AsyncKicker(scheduled.task_name, self.env.broker, scheduled.labels)
        return await (await kicker.kiq(*scheduled.args, **scheduled.kwargs)).wait_result(timeout=5)


@pytest.fixture
def jobs(monkeypatch):
    return Jobs(monkeypatch)


# ---- reach on /v1/tasks


def test_own_reach_sees_only_own_task_and_schedule(jobs):
    task_id, schedule_id = jobs.run(), jobs.schedule()
    for target in (task_id, schedule_id):
        assert jobs.call("GET", f"/v1/tasks/{target}", "u1@a/member").status_code == 200
        other = jobs.call("GET", f"/v1/tasks/{target}", "u2@a/member")
        assert other.status_code == 404
        assert other.json() == jobs.call("GET", "/v1/tasks/nope", "u2@a/member").json()
        assert jobs.call("POST", f"/v1/tasks/{target}/cancel", "u2@a/member").status_code == 404


def test_tenant_reach_reads_and_cancels_same_tenant_only(jobs):
    task_id, schedule_id = jobs.run(), jobs.schedule()
    for target in (task_id, schedule_id):
        assert jobs.call("GET", f"/v1/tasks/{target}", "boss@b/org_admin").status_code == 404
        assert jobs.call("POST", f"/v1/tasks/{target}/cancel", "boss@b/org_admin").status_code == 404
        assert jobs.call("GET", f"/v1/tasks/{target}", "boss@a/org_admin").status_code == 200
    data = jobs.call("GET", f"/v1/tasks/{task_id}", "boss@a/org_admin").json()["data"]
    assert (data["owner"], data["user_id"], data["tenant"]) == ("u1:org_id:a", "u1", {"org_id": "a"})
    assert jobs.call("POST", f"/v1/tasks/{task_id}/cancel", "boss@a/org_admin").status_code == 200
    resp = jobs.call("POST", f"/v1/tasks/{schedule_id}/cancel", "boss@a/org_admin")
    assert resp.json()["data"] == {"schedule_id": schedule_id, "deleted": True}
    assert jobs.env.source.schedules == {}


def test_reach_all_reads_and_cancels_any_tenant(jobs):
    task_id, schedule_id = jobs.run(), jobs.schedule()
    assert jobs.call("GET", f"/v1/tasks/{task_id}", "root@z/root").status_code == 200
    assert jobs.call("GET", f"/v1/tasks/{schedule_id}", "root@z/root").status_code == 200
    assert jobs.call("POST", f"/v1/tasks/{task_id}/cancel", "root@z/root").status_code == 200
    assert jobs.call("POST", f"/v1/tasks/{schedule_id}/cancel", "root@z/root").status_code == 200
    assert jobs.env.source.schedules == {}


@pytest.mark.parametrize("user", ["boss@a/org_admin", "root@z/root"])
def test_patch_schedule_stays_own(jobs, user):
    schedule_id = jobs.schedule()
    change = {"schedule": {"every": 9}}
    assert jobs.call("PATCH", f"/v1/tasks/{schedule_id}", user, json=change).status_code == 404
    assert jobs.env.source.schedules[schedule_id].interval == 3
    resp = jobs.call("PATCH", f"/v1/tasks/{schedule_id}", "u1@a/member", json=change)
    assert resp.status_code == 200


def test_reserved_system_id_is_refused_from_a_token(jobs):
    resp = jobs.call("POST", f"/v1/tasks/{ADD}", "__system__:nightly@a/root", json={"args": [1, 1]})
    assert resp.status_code == 401


# ---- the worker rebuilds the identity (A12)


async def test_http_run_gets_identity_rebuilt_from_current_roles(jobs):
    jobs.run()
    user = jobs.env.seen[0]["user"]
    assert (user["user_id"], user["kind"], user["roles"]) == ("u1", "user", ["member"])
    assert set(user["permissions"]) == {"tasks:trigger", "tasks:read", "tasks:cancel"}


async def test_changed_role_table_refuses_next_scheduled_run(jobs):
    schedule_id = jobs.schedule()
    assert (await jobs.fire(schedule_id)).return_value == 3
    # The role loses tasks:trigger (the worker restarted with a new config).
    jobs.auth.rbac = auth_config(roles={**ROLES, "member": ROLES["reader"]}).rbac_config()
    result = await jobs.fire(schedule_id)
    assert isinstance(result.error, PermissionError)
    assert "may not run this task" in str(result.error)
    assert len(jobs.env.seen) == 1
    # A removed role stops the run too.
    roles = {k: v for k, v in ROLES.items() if k not in {"member", "org_admin"}}
    jobs.auth.rbac = auth_config(roles=roles, systems={}).rbac_config()
    assert isinstance((await jobs.fire(schedule_id)).error, PermissionError)
    assert len(jobs.env.seen) == 1


async def test_refused_run_is_marked_failure(jobs):
    jobs.auth.rbac = auth_config(roles={**ROLES, "member": ROLES["reader"]}).rbac_config()
    # A run queued before the change, kicked as a stale queued message would be.
    claims = {"user_id": "u1", "org_id": "a", "roles": ["member"]}
    stored = {"user": {"user_id": "u1", "tenant": {"org_id": "a"}, "claims": claims}}
    task_id = uuid.uuid4().hex
    result = await _kiq(jobs.plain, stored, task_id)
    assert isinstance(result.error, PermissionError)
    assert jobs.ran == []
    assert (await state.read_status(jobs.env.store, task_id)).status == "failure"


async def test_custom_authorize_is_asked_with_task_name(jobs, monkeypatch):
    asked: list[tuple] = []

    async def authorize(identity, permission, resource=None):
        asked.append((identity.user_id, permission, resource))
        return False

    monkeypatch.setattr(jobs.auth, "authorize", authorize)
    result = await _kiq(jobs.nightly)
    assert isinstance(result.error, PermissionError)
    assert asked == [("__system__:nightly", "tasks:trigger", "tests.nightly")]
    assert jobs.ran == []


@pytest.mark.parametrize(
    "user",
    [
        {"user_id": "u1", "claims": {"user_id": "u1"}},  # tenant claim missing
        {"user_id": "u1", "claims": {"roles": "bad", "org_id": "a"}, "permissions": ["*"]},
        {"user_id": "__system__:x", "claims": {"org_id": "a"}},  # reserved id as a user
        {"user_id": "u1", "kind": "system", "tenant": {"org_id": "a"}},  # not a system id
        {"user_id": "__system__:gone", "kind": "system", "tenant": {"org_id": "a"}},
        {"user_id": ""},
    ],
)
async def test_invalid_stored_identity_does_not_run(jobs, user):
    result = await _kiq(jobs.plain, {"user": user})
    assert isinstance(result.error, PermissionError)
    assert jobs.ran == []


# ---- system identities


async def test_system_identity_runs_with_its_roles_only(jobs):
    task_id = uuid.uuid4().hex
    result = await _kiq(jobs.nightly, task_id=task_id)
    assert result.return_value == "done", result.error
    user = jobs.ran[0]["user"]
    assert user["kind"] == "system"
    assert user["user_id"] == "__system__:nightly"
    assert user["roles"] == ["member"]
    assert user["tenant"] == {"org_id": "a"}
    assert user["reach"] == "own"
    assert set(user["permissions"]) == {"tasks:trigger", "tasks:read", "tasks:cancel"}
    record = await state.read_status(jobs.env.store, task_id)
    assert (record.user_id, record.tenant) == ("__system__:nightly", {"org_id": "a"})


async def test_system_identity_cannot_exceed_its_roles(jobs):
    jobs.config.auth = auth_config(systems={"nightly": SYSTEMS["readonly"]})
    result = await _kiq(jobs.nightly)  # reader has no tasks:trigger
    assert isinstance(result.error, PermissionError)
    assert jobs.ran == []


async def test_stored_system_identity_is_rebuilt_from_current_entry(jobs):
    stale = {
        "user_id": "__system__:nightly",
        "kind": "system",
        "roles": ["root"],
        "permissions": ["*"],
        "reach": "all",
        "tenant": {"org_id": "a"},
    }
    result = await _kiq(jobs.plain, {"user": stale})
    assert result.return_value == "done", result.error
    user = jobs.ran[0]["user"]
    assert (user["roles"], user["reach"]) == (["member"], "own")
    assert "*" not in user["permissions"]
    assert jobs.ran[1]["user"] == user  # TaskContext.config sees the same identity
    jobs.config.auth = auth_config(systems={})
    assert isinstance((await _kiq(jobs.plain, {"user": stale})).error, PermissionError)


def test_label_does_not_override_http_caller(jobs):
    resp = jobs.call("POST", "/v1/tasks/tests.http_nightly", "root@z/root", json={})
    assert resp.status_code == 202
    user = jobs.ran[0]["user"]
    assert (user["user_id"], user["kind"], user["roles"]) == ("root", "user", ["root"])


async def test_label_does_not_override_python_caller(jobs):
    caller = {"user_id": "u9", "claims": {"user_id": "u9", "org_id": "a", "roles": ["member"]}}
    result = await _kiq(jobs.nightly, {"user": caller})
    assert result.return_value == "done", result.error
    assert jobs.ran[0]["user"]["user_id"] == "u9"


async def test_unknown_system_identity_aborts(jobs):
    jobs.config.auth = auth_config(systems={})
    result = await _kiq(jobs.nightly)
    assert isinstance(result.error, PermissionError)
    assert "not configured" in str(result.error)
    assert jobs.ran == []


async def test_python_kiq_without_label_runs_as_before(jobs):
    result = await _kiq(jobs.plain, {"user_id": "u1"})
    assert result.return_value == "done", result.error
    assert "user" not in jobs.ran[0]
    assert jobs.ran[0]["user_id"] == "u1"


async def test_auth_off_runs_label_task_without_identity_and_warns_once(monkeypatch, caplog):
    jobs = Jobs(monkeypatch, auth=False)
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api.jobs"):
        for _ in range(2):
            assert (await _kiq(jobs.nightly)).return_value == "done"
    assert all("user" not in config for config in jobs.ran)
    assert caplog.text.count("auth is off") == 1
    # An HTTP caller with auth off is unchanged too.
    task_id = jobs.env.client.post(f"/v1/tasks/{ADD}", json={"args": [1, 1]}).json()["data"]
    assert jobs.env.seen[-1] == {"user_id": "anonymous", "task_id": task_id["task_id"]}


# ---- helpers


async def _kiq(task, config: dict[str, Any] | None = None, task_id: str | None = None):
    kicker = task.kicker().with_task_id(task_id or uuid.uuid4().hex)
    if config is not None:
        kicker = kicker.with_labels(**{state.CONFIG_LABEL: json.dumps(config)})
    return await (await kicker.kiq()).wait_result(timeout=5)
