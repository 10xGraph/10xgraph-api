"""Unit tests for loader.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from injectq import InjectQ
from tenxgraph.core import CompiledGraph
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.store import BaseStore

from tenxgraph_api import BaseAuth
from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RemoteToolConfig
from tenxgraph_api.src.app.loader import (
    attach_all_modules,
    load_and_bind_auth,
    load_and_bind_authorization,
    load_auth,
    load_authorization,
    load_checkpointer,
    load_container,
    load_graph,
    load_store,
    load_thread_name_generator,
)
from tenxgraph_api.src.app.utils.thread_name_generator import ThreadNameGenerator


@pytest.mark.asyncio
async def test_load_graph_invalid_format():
    with pytest.raises(ValueError, match="Invalid graph path format"):
        await load_graph("invalid_path_no_colon")


@pytest.mark.asyncio
async def test_load_graph_success_callable():
    mock_graph = MagicMock(spec=CompiledGraph)
    mock_callable = MagicMock(return_value=mock_graph)

    mock_module = MagicMock()
    mock_module.my_graph = mock_callable

    with patch("importlib.import_module", return_value=mock_module) as mock_import:
        result = await load_graph("my_module:my_graph")
        assert result == mock_graph
        mock_import.assert_called_once_with("my_module")


@pytest.mark.asyncio
async def test_load_graph_success_async_callable():
    mock_graph = MagicMock(spec=CompiledGraph)
    mock_callable = AsyncMock(return_value=mock_graph)

    mock_module = MagicMock()
    mock_module.my_graph = mock_callable

    with patch("importlib.import_module", return_value=mock_module) as mock_import:
        result = await load_graph("my_module:my_graph")
        assert result == mock_graph


@pytest.mark.asyncio
async def test_load_graph_success_non_callable():
    class NonCallableGraph(CompiledGraph):
        def __init__(self):
            pass

    non_callable = NonCallableGraph()
    mock_module = MagicMock()
    mock_module.my_graph = non_callable

    with patch("importlib.import_module", return_value=mock_module):
        result = await load_graph("my_module:my_graph")
        assert result == non_callable


@pytest.mark.asyncio
async def test_load_graph_errors():
    # RuntimeError: app is None
    mock_module = MagicMock()
    mock_module.my_graph = None
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Failed to obtain a runnable graph"):
            await load_graph("my_module:my_graph")

    # TypeError: Loaded object is not a CompiledGraph
    mock_module.my_graph = "not a graph"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Loaded object is not a CompiledGraph"):
            await load_graph("my_module:my_graph")

    # ModuleNotFoundError
    with patch("importlib.import_module", side_effect=ModuleNotFoundError("No module named foo")):
        with pytest.raises(ModuleNotFoundError):
            await load_graph("foo:bar")

    # AttributeError
    mock_module = MagicMock()
    del mock_module.bar
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(AttributeError):
            await load_graph("foo:bar")


def test_load_checkpointer():
    assert load_checkpointer(None) is None

    with pytest.raises(ValueError, match="Invalid checkpointer path format"):
        load_checkpointer("invalid_no_colon")

    mock_cp = MagicMock(spec=BaseCheckpointer)
    mock_module = MagicMock()
    mock_module.cp = mock_cp
    with patch("importlib.import_module", return_value=mock_module):
        assert load_checkpointer("mod:cp") == mock_cp

    # RuntimeError
    mock_module.cp = None
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Failed to obtain a BaseCheckpointer"):
            load_checkpointer("mod:cp")

    # TypeError
    mock_module.cp = "not cp"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Loaded object is not a BaseCheckpointer"):
            load_checkpointer("mod:cp")

    # ModuleNotFoundError
    with patch("importlib.import_module", side_effect=ModuleNotFoundError()):
        with pytest.raises(ModuleNotFoundError):
            load_checkpointer("mod:cp")

    # AttributeError
    mock_module = MagicMock()
    del mock_module.cp
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(AttributeError):
            load_checkpointer("mod:cp")


def test_load_store():
    assert load_store(None) is None

    with pytest.raises(ValueError, match="Invalid store path format"):
        load_store("invalid")

    mock_store = MagicMock(spec=BaseStore)
    mock_module = MagicMock()
    mock_module.store = mock_store
    with patch("importlib.import_module", return_value=mock_module):
        assert load_store("mod:store") == mock_store

    # RuntimeError
    mock_module.store = None
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Failed to obtain a BaseStore"):
            load_store("mod:store")

    # TypeError
    mock_module.store = "not store"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Loaded object is not a BaseStore"):
            load_store("mod:store")

    # ModuleNotFoundError
    with patch("importlib.import_module", side_effect=ModuleNotFoundError()):
        with pytest.raises(ModuleNotFoundError):
            load_store("mod:store")


def test_load_container():
    assert load_container(None) is None

    mock_container = MagicMock(spec=InjectQ)
    mock_module = MagicMock()
    mock_module.container = mock_container
    with patch("importlib.import_module", return_value=mock_module):
        assert load_container("mod:container") == mock_container
        mock_container.activate.assert_called_once()

    # Exception cases
    mock_module.container = "not container"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(Exception, match="Failed to load InjectQ"):
            load_container("mod:container")


def test_load_auth():
    assert load_auth(None) is None

    with pytest.raises(ValueError, match="Invalid auth path format"):
        load_auth("invalid")

    class CustomAuth(BaseAuth):
        def authenticate(self, *args, **kwargs):
            pass

    # Class subclass of BaseAuth
    mock_module = MagicMock()
    mock_module.auth = CustomAuth
    with patch("importlib.import_module", return_value=mock_module):
        result = load_auth("mod:auth")
        assert isinstance(result, CustomAuth)

    # Instance of BaseAuth
    auth_instance = CustomAuth()
    mock_module.auth = auth_instance
    with patch("importlib.import_module", return_value=mock_module):
        assert load_auth("mod:auth") == auth_instance

    # TypeError
    mock_module.auth = "not auth"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(
            Exception, match="Loaded object is not a subclass or instance of BaseAuth"
        ):
            load_auth("mod:auth")

    # ModuleNotFoundError
    with patch("importlib.import_module", side_effect=ModuleNotFoundError()):
        with pytest.raises(ModuleNotFoundError):
            load_auth("mod:auth")


def test_load_authorization():
    assert load_authorization(None) is None

    class CustomAuthorization(AuthorizationBackend):
        def authorize(self, *args, **kwargs):
            pass

    # Class subclass
    mock_module = MagicMock()
    mock_module.authorization = CustomAuthorization
    with patch("importlib.import_module", return_value=mock_module):
        result = load_authorization("mod:authorization")
        assert isinstance(result, CustomAuthorization)

    # Instance
    auth_instance = CustomAuthorization()
    mock_module.authorization = auth_instance
    with patch("importlib.import_module", return_value=mock_module):
        assert load_authorization("mod:authorization") == auth_instance

    # TypeError
    mock_module.authorization = "not authorization"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(
            Exception, match="Loaded object is not a subclass or instance of AuthorizationBackend"
        ):
            load_authorization("mod:authorization")


def test_load_thread_name_generator():
    assert load_thread_name_generator(None) is None

    class CustomGenerator(ThreadNameGenerator):
        async def generate_name(self, messages):
            return "name"

    # Class subclass
    mock_module = MagicMock()
    mock_module.generator = CustomGenerator
    with patch("importlib.import_module", return_value=mock_module):
        result = load_thread_name_generator("mod:generator")
        assert isinstance(result, CustomGenerator)

    # Instance
    gen_instance = CustomGenerator()
    mock_module.generator = gen_instance
    with patch("importlib.import_module", return_value=mock_module):
        assert load_thread_name_generator("mod:generator") == gen_instance

    # TypeError
    mock_module.generator = "not generator"
    with patch("importlib.import_module", return_value=mock_module):
        with pytest.raises(
            Exception, match="Loaded object is not a subclass or instance of ThreadNameGenerator"
        ):
            load_thread_name_generator("mod:generator")


def test_load_and_bind_auth():
    container = MagicMock(spec=InjectQ)

    # custom needs a path
    with pytest.raises(ValueError, match="requires a 'path'"):
        load_and_bind_auth(container, {"method": "custom"})

    # Path existence check failure
    with patch("pathlib.Path.exists", return_value=False):
        with pytest.raises(ValueError, match="Custom auth path does not exist"):
            load_and_bind_auth(container, {"method": "custom", "path": "custom_path.py:auth"})

    # Dotted path conversion to py file check
    with patch("pathlib.Path.exists", return_value=True):
        with patch("tenxgraph_api.src.app.loader.load_auth") as mock_load_auth:
            mock_auth_instance = MagicMock(spec=BaseAuth)
            mock_load_auth.return_value = mock_auth_instance

            load_and_bind_auth(container, {"method": "custom", "path": "my.auth.path:auth"})
            container.bind_instance.assert_called_with(
                BaseAuth, mock_auth_instance, allow_none=True
            )

    # "none" binds no backend
    load_and_bind_auth(container, {"method": "none"})
    container.bind_instance.assert_called_with(BaseAuth, None, allow_none=True)

    # An unknown method fails at startup instead of silently binding no backend
    with pytest.raises(ValueError, match="Unsupported auth method"):
        load_and_bind_auth(container, {"method": "oauth"})


def test_load_and_bind_auth_jwt_needs_no_path(monkeypatch, tmp_path):
    """The documented ``"auth": "jwt"`` config must boot (it has no path)."""
    from tenxgraph_api.src.app.core.auth.jwt_auth import JwtAuth
    from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    config_file = tmp_path / "10xgraph.json"
    config_file.write_text('{"agent": "graph.react:app", "auth": "jwt"}')

    container = MagicMock(spec=InjectQ)
    load_and_bind_auth(container, GraphConfig(str(config_file)).auth_config())

    args, kwargs = container.bind_instance.call_args
    assert args[0] == BaseAuth
    assert isinstance(args[1], JwtAuth)


def test_load_and_bind_authorization(monkeypatch):
    from tenxgraph_api.src.app.core.config import settings as settings_module

    # The unconfigured default depends on MODE; do not depend on which test cached the
    # settings first.
    development = settings_module.Settings(MODE="development", _env_file=None)
    monkeypatch.setattr(settings_module, "get_settings", lambda: development)
    container = MagicMock(spec=InjectQ)

    # Path provided
    mock_auth_backend = MagicMock(spec=AuthorizationBackend)
    with patch("tenxgraph_api.src.app.loader.load_authorization", return_value=mock_auth_backend):
        load_and_bind_authorization(container, "mod:auth")
        container.bind_instance.assert_called_once_with(AuthorizationBackend, mock_auth_backend)

    # Path is None
    container.reset_mock()
    load_and_bind_authorization(container, None)
    args, kwargs = container.bind_instance.call_args
    assert args[0] == AuthorizationBackend
    assert isinstance(args[1], DefaultAuthorizationBackend)


@pytest.mark.asyncio
async def test_attach_all_modules():
    config = MagicMock(spec=GraphConfig)
    config.graph_path = "mod:graph"
    config.auth_config.return_value = {"method": "none", "path": "path.py:auth"}
    config.thread_name_generator_path = "mod:generator"
    config.authorization_path = "mod:authorization"
    config.store_path = None
    config.cache_store = None
    config.redis_url = None
    config.remote_tools = [
        RemoteToolConfig(
            node="tools",
            name="read_clipboard",
            description="Read clipboard text.",
        )
    ]

    container = MagicMock(spec=InjectQ)

    mock_graph = MagicMock(spec=CompiledGraph)
    mock_generator = MagicMock(spec=ThreadNameGenerator)
    mock_auth_backend = MagicMock(spec=AuthorizationBackend)

    with (
        patch("tenxgraph_api.src.app.loader.load_graph", return_value=mock_graph),
        patch(
            "tenxgraph_api.src.app.loader.load_thread_name_generator", return_value=mock_generator
        ),
        patch("tenxgraph_api.src.app.loader.load_authorization", return_value=mock_auth_backend),
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "tenxgraph_api.src.app.core.config.media_settings.get_media_settings"
        ) as mock_get_media_settings,
    ):
        mock_get_media_settings.return_value = MagicMock()

        result = await attach_all_modules(config, container)

        assert result == mock_graph
        mock_graph.attach_remote_tools.assert_called_once_with(
            [config.remote_tools[0].to_tool_schema()], "tools"
        )
        # verify bindings
        container.bind_instance.assert_any_call(BaseAuth, None, allow_none=True)
        container.bind_instance.assert_any_call(ThreadNameGenerator, mock_generator)
        container.bind_instance.assert_any_call(AuthorizationBackend, mock_auth_backend)

        # Test branch where config has no thread name generator and no auth config
        config.thread_name_generator_path = None
        config.auth_config.return_value = None
        config.remote_tools = []

        container.reset_mock()
        await attach_all_modules(config, container)
        container.bind_instance.assert_any_call(ThreadNameGenerator, None, allow_none=True)
        container.bind_instance.assert_any_call(BaseAuth, None, allow_none=True)


# ---------------------------------------------------------------------------
# Boot check: ownership authz needs a checkpointer that can name owners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_on", [True, False])
async def test_attach_refuses_ownership_without_owner_lookup(auth_on):
    from tenxgraph.storage.checkpointer import BaseCheckpointer

    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    class _Bare(BaseCheckpointer):  # type: ignore[misc]
        pass

    _Bare.__abstractmethods__ = frozenset()

    config = MagicMock(spec=GraphConfig)
    config.graph_path = "mod:graph"
    config.auth_config.return_value = {"method": "jwt"} if auth_on else None
    config.thread_name_generator_path = None
    config.authorization_path = "ownership"
    config.store_path = None
    config.redis_url = None
    config.cache_store = None
    config.remote_tools = []

    container = InjectQ()
    container.bind_instance(BaseCheckpointer, _Bare())

    with (
        patch("tenxgraph_api.src.app.loader.load_graph", return_value=MagicMock()),
        patch("tenxgraph_api.src.app.loader.load_and_bind_auth"),
        patch("tenxgraph_api.src.app.core.config.media_settings.get_media_settings"),
    ):
        if auth_on:
            with pytest.raises(RuntimeError, match="Refusing to start"):
                await attach_all_modules(config, container)
        else:
            # Auth off: no owner checks ever run, so nothing to refuse.
            await attach_all_modules(config, container)
            assert isinstance(container.get(AuthorizationBackend), OwnershipAuthorizationBackend)


# ---------------------------------------------------------------------------
# G-28: per-process state warning
# ---------------------------------------------------------------------------


def _warn_container(checkpointer, cache_store=None):
    from unittest.mock import MagicMock

    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore

    container = MagicMock()
    container.try_get.side_effect = lambda key: (
        cache_store if key is BaseCacheStore else checkpointer
    )
    return container


def test_warns_for_in_memory_checkpointer_and_missing_redis_in_production(monkeypatch, caplog):
    import logging

    from tenxgraph.storage.checkpointer import InMemoryCheckpointer

    from tenxgraph_api.src.app import loader

    monkeypatch.setattr(loader, "_is_production", lambda: True)
    with caplog.at_level(logging.WARNING):
        loader.warn_per_process_state(_warn_container(InMemoryCheckpointer()))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "InMemoryCheckpointer" in text
    assert "The cache store is in-memory" in text


def test_no_warning_in_single_process_development(monkeypatch, caplog):
    import logging

    from tenxgraph.storage.checkpointer import InMemoryCheckpointer

    from tenxgraph_api.src.app import loader

    monkeypatch.setattr(loader, "_is_production", lambda: False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    with caplog.at_level(logging.WARNING):
        loader.warn_per_process_state(_warn_container(InMemoryCheckpointer()))
    assert not caplog.records


def test_web_concurrency_above_one_warns_even_in_development(monkeypatch, caplog):
    import logging
    from unittest.mock import MagicMock

    from tenxgraph_api.src.app import loader
    from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore, RedisCacheStore

    monkeypatch.setattr(loader, "_is_production", lambda: False)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with caplog.at_level(logging.WARNING):
        shared = RedisCacheStore(client=MagicMock())
        loader.warn_per_process_state(_warn_container(None, shared))
        assert not caplog.records
        loader.warn_per_process_state(_warn_container(None, InMemoryCacheStore()))
    assert "The cache store is in-memory" in caplog.text
