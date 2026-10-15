"""``runs`` config and the server-picked thread id mode."""

# ruff: noqa: S101

import pytest

from tenxgraph_api.cli.config_editor import validate_config as validate
from tenxgraph_api.src.app.core.auth.authorization import (
    OwnershipAuthorizationBackend,
    RoleBasedAuthorizationBackend,
)
from tenxgraph_api.src.app.core.config.graph_config import (
    DEFAULT_MAX_CONCURRENT_RUNS_PER_USER,
    GraphConfig,
    RunsConfig,
)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_defaults():
    runs = RunsConfig.from_dict({})
    assert runs == RunsConfig()
    assert runs.thread_ids == "client"
    assert runs.server_thread_ids is False
    assert runs.config_keys is None
    assert runs.state_keys is None
    assert runs.max_concurrent_per_user == DEFAULT_MAX_CONCURRENT_RUNS_PER_USER
    assert runs.timeout is None


def test_full_config():
    runs = RunsConfig.from_dict(
        {
            "thread_ids": "server",
            "config_keys": ["locale", " tenant "],
            "state_keys": [],
            "max_concurrent_per_user": 3,
            "timeout": 90,
        }
    )
    assert runs.server_thread_ids is True
    assert runs.config_keys == frozenset({"locale", "tenant"})
    assert runs.state_keys == frozenset()
    assert runs.max_concurrent_per_user == 3
    assert runs.timeout == 90.0


@pytest.mark.parametrize("value", [0, None])
def test_zero_or_null_concurrency_means_unlimited(value):
    assert RunsConfig.from_dict({"max_concurrent_per_user": value}).max_concurrent_per_user is None


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ("server", "runs must be an object"),
        ({"thread_ids": "anyone"}, "thread_ids"),
        ({"config_keys": "locale"}, "config_keys"),
        ({"state_keys": [1]}, "state_keys"),
        ({"max_concurrent_per_user": -1}, "max_concurrent_per_user"),
        ({"max_concurrent_per_user": "5"}, "max_concurrent_per_user"),
        ({"max_concurrent_per_user": True}, "max_concurrent_per_user"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": -5}, "timeout"),
        ({"timeout": "60"}, "timeout"),
    ],
)
def test_invalid_values_are_rejected(data, match):
    with pytest.raises(ValueError, match=match):
        RunsConfig.from_dict(data)


def test_graph_config_property(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "graph:app", "runs": {"thread_ids": "server"}}')
    assert GraphConfig(str(path)).runs.server_thread_ids is True
    path.write_text('{"agent": "graph:app"}')
    assert GraphConfig(str(path)).runs == RunsConfig()


def test_config_editor_knows_and_validates_runs():
    base = {"agent": "graph:app"}
    assert validate({**base, "runs": {"thread_ids": "server"}}) == []
    issues = validate({**base, "runs": {"thread_ids": "nobody"}})
    assert [i["path"] for i in issues if i["level"] == "error"] == ["runs"]


# ---------------------------------------------------------------------------
# server-picked thread ids
# ---------------------------------------------------------------------------


class _Owners:
    def __init__(self, owners):
        self.owners = owners

    async def aget_thread_owner(self, thread_id):
        return self.owners.get(str(thread_id))


ALICE = {"user_id": "alice"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_cls", [OwnershipAuthorizationBackend, RoleBasedAuthorizationBackend]
)
async def test_server_mode_refuses_client_named_new_threads(backend_cls):
    backend = backend_cls(checkpointer=_Owners({"t1": "alice"}), server_thread_ids=True)
    for resource, action in (
        ("graph", "invoke"),
        ("graph", "stream"),
        ("checkpointer", "write"),
    ):
        assert await backend.authorize(ALICE, resource, action, resource_id="new-id") is False
    # Existing, owned threads and requests that name no thread still work.
    assert await backend.authorize(ALICE, "graph", "invoke", resource_id="t1") is True
    assert await backend.authorize(ALICE, "graph", "invoke", resource_id=None) is True


@pytest.mark.asyncio
async def test_client_mode_lets_a_client_name_a_new_thread():
    backend = OwnershipAuthorizationBackend(checkpointer=_Owners({}))
    assert backend.server_thread_ids is False
    assert await backend.authorize(ALICE, "graph", "invoke", resource_id="new-id") is True


@pytest.mark.parametrize(
    "authorization",
    ["ownership", {"backend": "rbac", "roles": {}}, None],
)
def test_loader_passes_the_mode_to_ownership_backends(monkeypatch, authorization):
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    class _Prod:
        MODE = "production"

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Prod())
    backend = loader._resolve_authorization_backend(authorization, None, server_thread_ids=True)
    assert isinstance(backend, OwnershipAuthorizationBackend)
    assert backend.server_thread_ids is True


def test_loader_logs_when_the_mode_cannot_apply(caplog):
    from injectq import InjectQ

    from tenxgraph_api.src.app import loader

    with caplog.at_level("INFO", logger="tenxgraph_api.loader"):
        loader.load_and_bind_authorization(InjectQ(), "allow_all", None, server_thread_ids=True)
    assert "does not check thread ownership" in caplog.text


@pytest.mark.parametrize(
    ("ag_ui", "server", "refused"), [(True, True, True), (True, False, False), (False, True, False)]
)
def test_ag_ui_with_server_thread_ids_is_refused_at_boot(ag_ui, server, refused):
    from types import SimpleNamespace

    from tenxgraph_api.src.app.core.auth.authorization import DefaultAuthorizationBackend
    from tenxgraph_api.src.app.loader import _refuse_ag_ui_with_server_thread_ids

    config = SimpleNamespace(
        ag_ui=SimpleNamespace(enabled=ag_ui),
        runs=RunsConfig(thread_ids="server" if server else "client"),
    )
    if refused:
        with pytest.raises(ValueError, match="ag_ui"):
            _refuse_ag_ui_with_server_thread_ids(config, OwnershipAuthorizationBackend())
    else:
        _refuse_ag_ui_with_server_thread_ids(config, OwnershipAuthorizationBackend())
    # Backends that do not refuse client ids are unaffected.
    _refuse_ag_ui_with_server_thread_ids(config, DefaultAuthorizationBackend())
