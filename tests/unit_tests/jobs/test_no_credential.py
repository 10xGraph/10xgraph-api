"""Background tasks never get the caller's credential (USER_CREDENTIAL_PLAN: Decisions).

Task payloads and results sit in the broker and the result store, so a task triggered by an
authenticated user carries user data only. The bearer token must not be in the broker
message, the task's run config, the stored status or the status response.
"""

# ruff: noqa: S101

from __future__ import annotations

from typing import Any

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth

from .test_routes import ADD, Env


SECRET = "s3cr3t-task-91be"


class SecretTokenAuth(BaseAuth):
    """The bearer token is ``<user_id>.<SECRET>``; the default ``downstream_credential``."""

    def authenticate(self, request, response, credential):
        return {"user_id": credential.credentials.split(".")[0]} if credential else {}


def test_task_payload_config_and_status_never_hold_the_credential(monkeypatch):
    env = Env(monkeypatch, auth_impl=SecretTokenAuth())
    sent: list[Any] = []
    kick = env.broker.kick

    async def record(message):
        sent.append(message)
        return await kick(message)

    monkeypatch.setattr(env.broker, "kick", record)

    resp = env.call("POST", f"/v1/tasks/{ADD}", user=f"u1.{SECRET}", json={"args": [1, 2]})
    assert resp.status_code == 202, resp.text
    assert SECRET not in resp.text
    task_id = resp.json()["data"]["task_id"]
    status = env.call("GET", f"/v1/tasks/{task_id}", user=f"u1.{SECRET}")
    assert status.status_code == 200
    assert status.json()["data"]["result"] == 3
    assert SECRET not in status.text

    assert sent, "the task was not sent"
    for message in sent:
        assert SECRET not in repr(message)
        assert SECRET not in message.message.decode()
    config = env.seen[0]
    assert config["user_id"] == "u1"
    assert "credential" not in config
    assert SECRET not in repr(config)
    # Everything the cache store holds: task status, results, locks.
    assert env.store._values
    assert all(SECRET.encode() not in value for value, _ in env.store._values.values())
    assert SECRET not in repr(env.store._values)
