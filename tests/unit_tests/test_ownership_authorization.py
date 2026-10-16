"""Unit tests for OwnershipAuthorizationBackend and mode-based default selection."""

from __future__ import annotations

import pytest

from tenxgraph_api.src.app.core.auth.authorization import (
    DefaultAuthorizationBackend,
    OwnershipAuthorizationBackend,
)


class _FakeCheckpointer:
    """Resolves thread ownership globally, like PgCheckpointer.aget_thread_owner."""

    def __init__(self, owners: dict[str, str]):
        self.owners = owners  # thread_id -> owner user_id

    async def aget_thread_owner(self, thread_id):
        return self.owners.get(str(thread_id))


def _backend(owners: dict[str, str]) -> OwnershipAuthorizationBackend:
    return OwnershipAuthorizationBackend(checkpointer=_FakeCheckpointer(owners))


ALICE = {"user_id": "alice"}
MALLORY = {"user_id": "mallory"}


@pytest.mark.asyncio
async def test_unauthenticated_is_denied():
    b = _backend({})
    assert await b.authorize({}, "checkpointer", "read", resource_id="t1") is False


@pytest.mark.asyncio
async def test_non_thread_resource_is_allowed():
    b = _backend({})
    # store scopes itself by user_id; ownership backend does not gate it
    assert await b.authorize(ALICE, "store", "read", resource_id="mem1") is True


@pytest.mark.asyncio
async def test_no_resource_id_is_allowed():
    b = _backend({})
    assert await b.authorize(ALICE, "checkpointer", "read", resource_id=None) is True


@pytest.mark.asyncio
async def test_owner_can_read_own_thread():
    b = _backend({"t1": "alice"})
    assert await b.authorize(ALICE, "checkpointer", "read", resource_id="t1") is True


@pytest.mark.asyncio
async def test_non_owner_cannot_read_thread():
    b = _backend({"t1": "alice"})
    assert await b.authorize(MALLORY, "checkpointer", "read", resource_id="t1") is False


@pytest.mark.asyncio
async def test_non_owner_cannot_delete_or_fix_thread():
    b = _backend({"t1": "alice"})
    assert await b.authorize(MALLORY, "checkpointer", "delete", resource_id="t1") is False
    assert await b.authorize(MALLORY, "graph", "fix", resource_id="t1") is False
    assert await b.authorize(MALLORY, "graph", "stop", resource_id="t1") is False


@pytest.mark.asyncio
async def test_invoke_on_new_thread_is_allowed():
    """A run targeting a thread that does not exist yet is a new session -> allow."""
    b = _backend({})  # no threads exist
    assert await b.authorize(ALICE, "graph", "invoke", resource_id="new-thread") is True
    assert await b.authorize(ALICE, "graph", "stream", resource_id="new-thread") is True


@pytest.mark.asyncio
async def test_invoke_and_stream_on_other_users_thread_are_denied():
    """The core fix: you cannot run (invoke/stream) another user's existing thread."""
    b = _backend({"t1": "alice"})
    assert await b.authorize(MALLORY, "graph", "invoke", resource_id="t1") is False
    assert await b.authorize(MALLORY, "graph", "stream", resource_id="t1") is False


@pytest.mark.asyncio
async def test_owner_can_invoke_and_stream_own_thread():
    b = _backend({"t1": "alice"})
    assert await b.authorize(ALICE, "graph", "invoke", resource_id="t1") is True
    assert await b.authorize(ALICE, "graph", "stream", resource_id="t1") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "action"),
    [
        ("checkpointer", "read"),
        ("checkpointer", "delete"),
        ("graph", "stop"),
        ("graph", "fix"),
    ],
)
async def test_non_create_actions_on_an_unowned_thread_are_denied(resource, action):
    """An unowned thread holds nothing of the caller's; only creating it is allowed."""
    b = _backend({})
    assert await b.authorize(ALICE, resource, action, resource_id="ghost") is False


@pytest.mark.asyncio
async def test_checkpointer_write_may_create_an_unowned_thread():
    b = _backend({})
    assert await b.authorize(ALICE, "checkpointer", "write", resource_id="ghost") is True


@pytest.mark.asyncio
async def test_unsupported_checkpointer_fails_closed():
    class _NoOwnerSupport:
        async def aget_thread_owner(self, thread_id):
            raise NotImplementedError

    b = OwnershipAuthorizationBackend(checkpointer=_NoOwnerSupport())
    assert await b.authorize(ALICE, "checkpointer", "read", resource_id="t1") is False
    assert await b.authorize(ALICE, "graph", "invoke", resource_id="t1") is False


@pytest.mark.asyncio
async def test_no_checkpointer_fails_closed():
    b = OwnershipAuthorizationBackend(checkpointer=None)
    # Property tries the DI container; in this unit context nothing is bound -> None.
    b._checkpointer = None
    assert b.checkpointer is None
    assert await b.authorize(ALICE, "checkpointer", "read", resource_id="t1") is False
    assert await b.authorize(ALICE, "graph", "invoke", resource_id="t1") is False


@pytest.mark.asyncio
async def test_no_checkpointer_still_allows_requests_without_a_thread():
    b = OwnershipAuthorizationBackend(checkpointer=None)
    b._checkpointer = None
    assert await b.authorize(ALICE, "checkpointer", "read", resource_id=None) is True


@pytest.mark.asyncio
async def test_resolver_error_fails_closed():
    class _Boom:
        async def aget_thread_owner(self, thread_id):
            raise RuntimeError("db down")

    b = OwnershipAuthorizationBackend(checkpointer=_Boom())
    assert await b.authorize(ALICE, "checkpointer", "read", resource_id="t1") is False


@pytest.mark.asyncio
async def test_repeated_checks_hit_backing_lookup_once():
    """Scalability guarantee: ownership is cached, so N requests for a thread cost one
    DB lookup, not N."""

    class _Counting:
        def __init__(self):
            self.calls = 0

        async def aget_thread_owner(self, thread_id):
            self.calls += 1
            return "alice" if str(thread_id) == "t1" else None

    cp = _Counting()
    b = OwnershipAuthorizationBackend(checkpointer=cp)
    for _ in range(5):
        assert await b.authorize(ALICE, "checkpointer", "read", resource_id="t1") is True
        assert await b.authorize(MALLORY, "graph", "invoke", resource_id="t1") is False
    assert cp.calls == 1  # resolved once, then served from cache


def test_builtin_and_mode_defaults(monkeypatch):
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    resolve = loader._resolve_authorization_backend

    assert isinstance(resolve("ownership"), OwnershipAuthorizationBackend)
    assert isinstance(resolve("allow_all"), DefaultAuthorizationBackend)
    assert isinstance(resolve("none"), DefaultAuthorizationBackend)
    with pytest.raises(ValueError):
        resolve("bogus")

    class _Mode:
        def __init__(self, mode):
            self.MODE = mode

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Mode("production"))
    assert isinstance(resolve(None), OwnershipAuthorizationBackend)

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Mode("development"))
    assert isinstance(resolve(None), DefaultAuthorizationBackend)


# ---------------------------------------------------------------------------
# boot-time check: ownership needs a checkpointer that can name owners
# ---------------------------------------------------------------------------


def test_supports_owner_lookup():
    from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer

    from tenxgraph_api.src.app.core.auth.authorization import supports_owner_lookup

    class _Inherits(InMemoryCheckpointer):
        pass

    class _Bare(BaseCheckpointer):  # type: ignore[misc]
        pass

    _Bare.__abstractmethods__ = frozenset()  # instantiate without the abstract surface

    assert supports_owner_lookup(InMemoryCheckpointer()) is True
    assert supports_owner_lookup(_Inherits()) is True
    assert supports_owner_lookup(_Bare()) is False
    assert supports_owner_lookup(None) is False
    assert supports_owner_lookup(_FakeCheckpointer({})) is True
    assert supports_owner_lookup(object()) is False


def test_assert_ownership_supported_refuses_unsupported_checkpointer():
    from tenxgraph.storage.checkpointer import BaseCheckpointer

    from tenxgraph_api.src.app.core.auth.authorization import (
        RoleBasedAuthorizationBackend,
        assert_ownership_supported,
    )

    class _Bare(BaseCheckpointer):  # type: ignore[misc]
        pass

    _Bare.__abstractmethods__ = frozenset()

    for backend in (OwnershipAuthorizationBackend(), RoleBasedAuthorizationBackend({})):
        with pytest.raises(RuntimeError, match="Refusing to start"):
            assert_ownership_supported(backend, _Bare())
        with pytest.raises(RuntimeError, match="no checkpointer"):
            assert_ownership_supported(backend, None)
        assert_ownership_supported(backend, _FakeCheckpointer({}))  # fine

    # Backends that do not check ownership do not care.
    assert_ownership_supported(DefaultAuthorizationBackend(), None)


def test_auth_enabled_defaults_to_ownership_in_every_mode(monkeypatch):
    """MODE=development with auth on used to default to allow_all (scope none)."""
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    class _Mode:
        def __init__(self, mode):
            self.MODE = mode

    resolve = loader._resolve_authorization_backend
    for mode in ("development", "production"):
        monkeypatch.setattr(settings_mod, "get_settings", lambda m=mode: _Mode(m))
        backend = resolve(None, None, auth_enabled=True)
        assert isinstance(backend, OwnershipAuthorizationBackend)
        assert backend.isolation_scope() == "owner"

    # Auth off in development: no identity, nothing to own, so the permissive backend.
    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Mode("development"))
    assert isinstance(resolve(None, None, auth_enabled=False), DefaultAuthorizationBackend)


def test_explicit_allow_all_with_auth_warns_in_production(monkeypatch, caplog):
    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    class _Prod:
        MODE = "production"

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _Prod())
    with caplog.at_level("WARNING", logger="tenxgraph_api.loader"):
        backend = loader._resolve_authorization_backend("allow_all", None, auth_enabled=True)
    assert isinstance(backend, DefaultAuthorizationBackend)
    assert "every authenticated user" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="tenxgraph_api.loader"):
        loader._resolve_authorization_backend("allow_all", None, auth_enabled=False)
    assert "every authenticated user" not in caplog.text


@pytest.mark.asyncio
async def test_attach_binds_ownership_when_auth_is_on_in_development(monkeypatch):
    """End to end through attach_all_modules: auth on + no authorization key -> ownership."""
    from unittest.mock import MagicMock, patch

    from injectq import InjectQ
    from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer

    from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
    from tenxgraph_api.src.app.core.config import settings as settings_mod
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig
    from tenxgraph_api.src.app.loader import attach_all_modules

    real_settings = settings_mod.get_settings()
    monkeypatch.setattr(real_settings, "MODE", "development")

    config = MagicMock(spec=GraphConfig)
    config.graph_path = "mod:graph"
    config.auth_config.return_value = {"method": "jwt"}
    config.thread_name_generator_path = None
    config.authorization_path = None
    config.store_path = None
    config.redis_url = None
    config.remote_tools = []
    config.runs = RunsConfig()

    container = InjectQ()
    container.bind_instance(BaseCheckpointer, InMemoryCheckpointer())
    with (
        patch("tenxgraph_api.src.app.loader.load_graph", return_value=MagicMock()),
        patch("tenxgraph_api.src.app.loader.load_and_bind_auth"),
        patch("tenxgraph_api.src.app.core.config.media_settings.get_media_settings"),
    ):
        await attach_all_modules(config, container)
    assert isinstance(container.get(AuthorizationBackend), OwnershipAuthorizationBackend)
