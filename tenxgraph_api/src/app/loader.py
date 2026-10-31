import importlib
import inspect
import logging
import os
import weakref
from collections.abc import Callable
from pathlib import Path

from injectq import InjectQ
from tenxgraph.core import CompiledGraph
from tenxgraph.core.graph.remote_tool import RemoteToolConfig as CoreRemoteToolConfig
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.store import BaseStore
from tenxgraph.utils.id_generator import (
    BaseIDGenerator,
    BigIntIDGenerator,
    DefaultIDGenerator,
    HexIDGenerator,
    IDType,
    IntIDGenerator,
    ShortIDGenerator,
    TimestampIDGenerator,
    UUIDGenerator,
)

from tenxgraph_api import BaseAuth
from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
    OwnershipAuthorizationBackend,
    RoleBasedAuthorizationBackend,
    assert_ownership_supported,
)
from tenxgraph_api.src.app.core.cache_store.base import BaseCacheStore
from tenxgraph_api.src.app.core.cache_store.loader import load_and_bind_cache_store
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.utils.thread_name_generator import (
    DefaultThreadNameGenerator,
    FirstMessageThreadNameGenerator,
    ThreadNameGenerator,
)


logger = logging.getLogger("tenxgraph_api.loader")


async def load_graph(path: str) -> CompiledGraph | None:
    if ":" not in path:
        raise ValueError(f"Invalid graph path format '{path}'. Expected 'module:attribute'.")

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)

        if callable(entry_point_obj):
            if inspect.iscoroutinefunction(entry_point_obj):
                app = await entry_point_obj()
            else:
                app = entry_point_obj()
        else:
            app = entry_point_obj

        if app is None:
            raise RuntimeError(f"Failed to obtain a runnable graph from {path}.")

        if isinstance(app, CompiledGraph):
            logger.info(f"Successfully loaded graph '{function_name}' from {path}.")
        else:
            raise TypeError("Loaded object is not a CompiledGraph.")

    except ModuleNotFoundError as e:
        logger.error(f"Module not found when loading graph from {path}: {e}")
        raise ModuleNotFoundError(f"Module not found for graph path '{path}': {e}")
    except AttributeError as e:
        logger.error(f"Attribute not found when loading graph from {path}: {e}")
        raise AttributeError(f"Attribute not found for graph path '{path}': {e}")
    except Exception as e:
        logger.error(f"Error loading graph from {path}: {e}")
        raise Exception(f"Failed to load graph from {path}: {e}")

    return app


def load_checkpointer(path: str | None) -> BaseCheckpointer | None:
    if not path:
        return None

    if ":" not in path:
        raise ValueError(f"Invalid checkpointer path format '{path}'. Expected 'module:attribute'.")

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)
        checkpointer = entry_point_obj

        if checkpointer is None:
            raise RuntimeError(f"Failed to obtain a BaseCheckpointer graph from {path}.")

        if isinstance(checkpointer, BaseCheckpointer):
            logger.info(f"Successfully loaded BaseCheckpointer '{function_name}' from {path}.")
        else:
            raise TypeError("Loaded object is not a BaseCheckpointer.")
    except ModuleNotFoundError as e:
        logger.error(f"Module not found when loading BaseCheckpointer from {path}: {e}")
        raise ModuleNotFoundError(f"Module not found for checkpointer path '{path}': {e}")
    except AttributeError as e:
        logger.error(f"Attribute not found when loading BaseCheckpointer from {path}: {e}")
        raise AttributeError(f"Attribute not found for checkpointer path '{path}': {e}")
    except Exception as e:
        logger.error(f"Error loading BaseCheckpointer from {path}: {e}")
        raise Exception(f"Failed to load BaseCheckpointer from {path}: {e}")

    return checkpointer


def load_store(path: str | None) -> BaseStore | None:
    if not path:
        return None

    if ":" not in path:
        raise ValueError(f"Invalid store path format '{path}'. Expected 'module:attribute'.")

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)
        store = entry_point_obj

        if store is None:
            raise RuntimeError(f"Failed to obtain a BaseStore from {path}.")

        if isinstance(store, BaseStore):
            logger.info(f"Successfully loaded graph '{function_name}' from {path}.")
        else:
            raise TypeError("Loaded object is not a BaseStore.")
    except ModuleNotFoundError as e:
        logger.error(f"Module not found when loading BaseStore from {path}: {e}")
        raise ModuleNotFoundError(f"Module not found for store path '{path}': {e}")
    except AttributeError as e:
        logger.error(f"Attribute not found when loading BaseStore from {path}: {e}")
        raise AttributeError(f"Attribute not found for store path '{path}': {e}")
    except Exception as e:
        logger.error(f"Error loading BaseStore from {path}: {e}")
        raise Exception(f"Failed to load BaseStore from {path}: {e}")

    return store


def load_cache_store(path: str) -> BaseCacheStore:
    """Load the ``cache_store`` from ``"module:attr"``: an instance or a zero-arg factory."""
    if ":" not in path:
        raise ValueError(f"Invalid cache_store path format '{path}'. Expected 'module:attribute'.")

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)
        cache_store = entry_point_obj
        if not isinstance(cache_store, BaseCacheStore) and callable(cache_store):
            cache_store = cache_store()

        if isinstance(cache_store, BaseCacheStore):
            logger.info(f"Successfully loaded BaseCacheStore '{function_name}' from {path}.")
        else:
            raise TypeError("Loaded object is not a BaseCacheStore or a factory returning one.")
    except ModuleNotFoundError as e:
        logger.error(f"Module not found when loading BaseCacheStore from {path}: {e}")
        raise ModuleNotFoundError(f"Module not found for cache_store path '{path}': {e}")
    except AttributeError as e:
        logger.error(f"Attribute not found when loading BaseCacheStore from {path}: {e}")
        raise AttributeError(f"Attribute not found for cache_store path '{path}': {e}")
    except Exception as e:
        logger.error(f"Error loading BaseCacheStore from {path}: {e}")
        raise Exception(f"Failed to load BaseCacheStore from {path}: {e}")

    return cache_store


def load_container(path: str | None) -> InjectQ | None:
    if not path:
        return None

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)
        container = entry_point_obj

        if container is None:
            raise RuntimeError(f"Failed to obtain a InjectQ from {path}.")

        if isinstance(container, InjectQ):
            logger.info(f"Successfully loaded InjectQ '{function_name}' from {path}.")
        else:
            raise TypeError("Loaded object is not a InjectQ.")
    except Exception as e:
        logger.error(f"Error loading InjectQ from {path}: {e}")
        raise Exception(f"Failed to load InjectQ from {path}: {e}")

    # if we have container, set it as the global instance
    if container:
        container.activate()

    return container


def load_auth(path: str | None) -> BaseAuth | None:
    if not path:
        return None

    if ":" not in path:
        raise ValueError(f"Invalid auth path format '{path}'. Expected 'module:attribute'.")

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)

        if inspect.isclass(entry_point_obj) and issubclass(entry_point_obj, BaseAuth):
            auth = entry_point_obj()
        elif isinstance(entry_point_obj, BaseAuth):
            auth = entry_point_obj
        else:
            raise TypeError("Loaded object is not a subclass or instance of BaseAuth.")

        logger.info(f"Successfully loaded BaseAuth '{function_name}' from {path}.")
    except ModuleNotFoundError as e:
        logger.error(f"Module not found when loading BaseAuth from {path}: {e}")
        raise ModuleNotFoundError(f"Module not found for auth path '{path}': {e}")
    except AttributeError as e:
        logger.error(f"Attribute not found when loading BaseAuth from {path}: {e}")
        raise AttributeError(f"Attribute not found for auth path '{path}': {e}")
    except Exception as e:
        logger.error(f"Error loading BaseAuth from {path}: {e}")
        raise Exception(f"Failed to load BaseAuth from {path}: {e}")

    return auth


def load_authorization(path: str | None) -> AuthorizationBackend | None:
    """
    Load authorization backend from the specified path.

    Args:
        path: Module path in format 'module:attribute' or None

    Returns:
        AuthorizationBackend instance or None if path is not provided

    Raises:
        Exception: If loading fails or object is not AuthorizationBackend
    """
    if not path:
        return None

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)

        # If it's a class, instantiate it; if it's an instance, use as is
        if inspect.isclass(entry_point_obj) and issubclass(entry_point_obj, AuthorizationBackend):
            authorization = entry_point_obj()
        elif isinstance(entry_point_obj, AuthorizationBackend):
            authorization = entry_point_obj
        else:
            raise TypeError("Loaded object is not a subclass or instance of AuthorizationBackend.")

        logger.info(f"Successfully loaded AuthorizationBackend '{function_name}' from {path}.")
    except Exception as e:
        logger.error(f"Error loading AuthorizationBackend from {path}: {e}")
        raise Exception(f"Failed to load AuthorizationBackend from {path}: {e}")

    return authorization


def load_thread_name_generator(path: str | None) -> ThreadNameGenerator | None:
    if not path:
        return None

    module_name_importable, function_name = path.split(":")

    try:
        module = importlib.import_module(module_name_importable)
        entry_point_obj = getattr(module, function_name)

        # If it's a class, instantiate it; if it's an instance, use as is
        if inspect.isclass(entry_point_obj) and issubclass(entry_point_obj, ThreadNameGenerator):
            thread_name_generator = entry_point_obj()
        elif isinstance(entry_point_obj, ThreadNameGenerator):
            thread_name_generator = entry_point_obj
        else:
            raise TypeError("Loaded object is not a subclass or instance of ThreadNameGenerator.")

        logger.info(f"Successfully loaded ThreadNameGenerator '{function_name}' from {path}.")
    except Exception as e:
        logger.error(f"Error loading ThreadNameGenerator from {path}: {e}")
        raise Exception(f"Failed to load ThreadNameGenerator from {path}: {e}")

    return thread_name_generator


def resolve_thread_name_generator(choice: str) -> ThreadNameGenerator | None:
    """The generator for a ``thread_name_generator`` choice; None for ``none`` (no names)."""
    if choice == "none":
        return None
    if choice == "random":
        return DefaultThreadNameGenerator()
    if choice == "first_message":
        return FirstMessageThreadNameGenerator()
    return load_thread_name_generator(choice)


def _snowflake_id_generator() -> BaseIDGenerator:
    from tenxgraph_api.src.app.utils import snowflake_id_generator

    if not snowflake_id_generator.HAS_SNOWFLAKE:
        raise ValueError(
            'id_generator "snowflake" needs the snowflakekit package: '
            'pip install "10xgraph-api[snowflakekit]"'
        )
    return snowflake_id_generator.SnowFlakeIdGenerator()


# ``id_generator`` names in 10xgraph.json (graph_config.ID_GENERATOR_NAMES): the concrete core
# generators, plus the API's Snowflake one (reads the SNOWFLAKE_* environment variables).
BUILTIN_ID_GENERATORS: dict[str, Callable[[], BaseIDGenerator]] = {
    "uuid": UUIDGenerator,
    "hex": HexIDGenerator,
    "short": ShortIDGenerator,
    "int": IntIDGenerator,
    "bigint": BigIntIDGenerator,
    "timestamp": TimestampIDGenerator,
    "snowflake": _snowflake_id_generator,
}


def load_id_generator(value: str) -> BaseIDGenerator:
    """The generator for an ``id_generator`` value: a built-in name or ``"module:attr"``.

    ``module:attr`` may name a ``BaseIDGenerator`` instance, a subclass, or a zero-argument
    factory returning one.
    """
    builtin = BUILTIN_ID_GENERATORS.get(value)
    if builtin is not None:
        return builtin()
    if ":" not in value:
        raise ValueError(
            f"id_generator must be one of {', '.join(BUILTIN_ID_GENERATORS)} or "
            f'"module:attr"; got {value!r}'
        )
    module_name, attr = value.split(":", 1)
    try:
        target = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"id_generator {value!r} cannot be loaded: {exc}") from exc
    generator = target if isinstance(target, BaseIDGenerator) else None
    if generator is None and callable(target):
        generator = target()
    if not isinstance(generator, BaseIDGenerator):
        raise TypeError(
            f"id_generator {value!r} is not a BaseIDGenerator instance, subclass or factory "
            "returning one"
        )
    return generator


# PgCheckpointer ``id_type`` -> what its thread and message id columns hold.
_PG_INTEGER_ID_TYPES = {"int": "integer", "bigint": "bigint"}


def _check_checkpointer_id_type(
    generator: BaseIDGenerator, name: str, checkpointer: object
) -> None:
    """Refuse a generator whose ids the checkpointer's id columns cannot store.

    Only ``PgCheckpointer`` declares its column type (``id_type``: ``string`` makes VARCHAR
    columns, ``int`` INTEGER, ``bigint`` BIGINT); other checkpointers store any id.
    """
    try:
        from tenxgraph.storage.checkpointer.pg_checkpointer import PgCheckpointer
    except ImportError:  # pragma: no cover - the module imports without its drivers
        return
    if not isinstance(checkpointer, PgCheckpointer):
        return
    raw = getattr(checkpointer, "id_type", "string")
    column = _PG_INTEGER_ID_TYPES.get(str(getattr(raw, "value", raw)), "string")
    makes = generator.id_type
    if makes == IDType.STRING and column != "string":
        fix = 'id_type="string"'
    elif (makes != IDType.STRING and column == "string") or (
        makes == IDType.BIGINT and column == "integer"
    ):
        fix = 'id_type="bigint"'
    else:
        return
    raise ValueError(
        f"id_generator {name!r} makes {makes.value} ids but the PgCheckpointer stores "
        f"{column} ids (its id_type is {str(getattr(raw, 'value', raw))!r}). Pass {fix} to "
        "PgCheckpointer(...), or pick an id_generator whose ids its columns hold."
    )


# Generators apply_id_generator put on a graph; see there.
_applied_id_generators: weakref.WeakSet[BaseIDGenerator] = weakref.WeakSet()


def apply_id_generator(
    graph: CompiledGraph, config: GraphConfig, container: InjectQ
) -> BaseIDGenerator | None:
    """Use ``id_generator`` from 10xgraph.json for the loaded graph; None when it is not set.

    Core binds the graph's generator in the graph's InjectQ container as ``BaseIDGenerator``,
    ``"generated_id_type"`` and the ``"generated_id"`` factory, and reads them back from
    ``InjectQ.get_instance()`` for thread, message and memory ids; the API reads
    ``"generated_id"`` for new thread ids. All three are rebound in the graph's container, the
    API's container and the active one, so every id comes from the configured generator. A
    ``PgCheckpointer`` whose ``id_type`` cannot hold the generator's ids refuses the boot.
    A graph compiled with its own (non-default) generator refuses the setting, so the
    generator lives in one place.
    """
    name = config.id_generator
    if name is None:
        return None
    state_graph = getattr(graph, "_state_graph", None)
    compiled_with = getattr(state_graph, "_id_generator", None)
    if (
        compiled_with is not None
        and type(compiled_with) is not DefaultIDGenerator
        and compiled_with not in _applied_id_generators
    ):
        raise ValueError(
            f"The graph is compiled with {type(compiled_with).__name__} "
            f"(StateGraph(id_generator=...)) and 10xgraph.json sets id_generator {name!r}. "
            "Set the generator in one place: remove one of them."
        )
    generator = load_id_generator(name)

    containers: list[InjectQ] = []
    for candidate in (getattr(state_graph, "_container", None), container, InjectQ.get_instance()):
        if isinstance(candidate, InjectQ) and all(candidate is not c for c in containers):
            containers.append(candidate)
    checkpointer = next(
        (c.try_get(BaseCheckpointer) for c in containers if BaseCheckpointer in c), None
    )
    _check_checkpointer_id_type(generator, name, checkpointer)

    # Factories, not instances: InjectQ keeps a resolved instance binding in its singleton
    # cache, so a rebound instance would not be seen; a factory is looked up first and called
    # on every resolve (core rebinds "get_node" the same way in CompiledGraph.override_node).
    id_type = generator.id_type
    for target in containers:
        target.bind_factory(BaseIDGenerator, lambda: generator)
        target.bind_factory("generated_id_type", lambda: id_type)
        target.bind_factory("generated_id", generator.generate)
    if state_graph is not None:
        # GET /v1/graph reports the generator from here. Remembered, so loading the same graph
        # object again (a second app in one process) is not mistaken for a compiled-in one.
        state_graph._id_generator = generator
        _applied_id_generators.add(generator)
    logger.info("ID generator: %s (id_generator %s)", type(generator).__name__, name)
    return generator


def load_and_bind_auth(container: InjectQ, auth_config: dict) -> None:
    """Bind the authentication backend described by ``GraphConfig.auth_config()``.

    ``jwt`` needs no path; ``custom`` needs a ``path`` to a ``BaseAuth``. Any other method
    is a configuration error and fails at startup rather than silently binding no backend.
    """
    from tenxgraph_api.src.app.core.auth.jwt_auth import JwtAuth, check_jwt_settings
    from tenxgraph_api.src.app.core.config.settings import get_settings

    method = auth_config.get("method")
    if method == "jwt":
        check_jwt_settings(get_settings())
        auth_backend: BaseAuth | None = JwtAuth()
    elif method == "none":
        auth_backend = None
    elif method == "custom":
        auth_backend = _load_custom_auth(auth_config.get("path"))
    else:
        raise ValueError(f"Unsupported auth method: {method!r}. Use 'jwt' or 'custom'.")

    container.bind_instance(BaseAuth, auth_backend, allow_none=True)


def _load_custom_auth(path: str | None) -> BaseAuth:
    if not path:
        raise ValueError("Custom auth requires a 'path' in auth_config.")

    # Extract file path before the ':' for existence check
    module_or_path = path.split(":", 1)[0] if ":" in path else path

    # Simple handling: if it appears to be a filesystem path, use it; otherwise
    # convert dotted module path to a file path like src/auth/custom_auth.py
    if os.path.sep in module_or_path or module_or_path.endswith(".py"):
        file_path = Path(module_or_path)
    elif "." in module_or_path and os.path.sep not in module_or_path:
        file_path = Path(module_or_path.replace(".", os.path.sep) + ".py")
    else:
        file_path = Path(module_or_path)

    if not file_path.exists():
        raise ValueError(f"Custom auth path does not exist: {module_or_path}")

    auth = load_auth(path)
    if auth is None:  # load_auth only returns None for an empty path, checked above
        raise ValueError("Custom auth requires a 'path' in auth_config.")
    return auth


# Built-in authorization backends selectable by name in ``10xgraph.json``.
_BUILTIN_AUTHORIZATION = {
    "ownership": OwnershipAuthorizationBackend,  # object-level: owner-only thread access
    "allow_all": DefaultAuthorizationBackend,  # authenticated == authorized
    "default": DefaultAuthorizationBackend,  # alias for allow_all
    "none": DefaultAuthorizationBackend,  # alias for allow_all
}


def _resolve_authorization_backend(
    authorization: str | dict | None,
    cache: BaseCacheStore | None = None,
    server_thread_ids: bool = False,
    auth_enabled: bool = False,
) -> AuthorizationBackend:
    """Pick the authorization backend from config, defaulting by run mode.

    Resolution order (developer choice always wins):

    1. A dict ``{"backend": "rbac", "roles": {...}, ...}`` -> role-based access control.
    2. ``"module:attr"`` -> the developer's custom :class:`AuthorizationBackend`.
    3. A built-in name (``"ownership"``, ``"allow_all"``/``"default"``/``"none"``) ->
       that backend, regardless of mode.
    4. Not configured (``null``) -> secure by default
       (:class:`OwnershipAuthorizationBackend`) whenever requests carry an identity
       (``auth_enabled``) or ``MODE=production``. Only with auth off in development is it
       :class:`DefaultAuthorizationBackend`, which then changes nothing: without an identity
       no authorization check runs. Either can be overridden via (1)/(2)/(3).

    The default used to depend on the mode alone, so a server shipped with the default
    ``MODE=development`` and auth on ran ``allow_all``: the API stamped isolation scope
    ``none`` and every authenticated user could read every thread and memory.

    ``cache`` (the API's cache store) backs the ownership cache's shared L2 tier; the
    built-in backends use it and never close it.
    ``server_thread_ids`` (``runs.thread_ids == "server"``) makes the built-in ownership
    backends refuse client-named ids for threads that do not exist yet.
    """
    # 1. Config-driven RBAC: {"backend": "rbac", "roles": {...}, "default_scopes": [...]}
    if isinstance(authorization, dict):
        backend_name = (authorization.get("backend") or authorization.get("type") or "").lower()
        if backend_name in ("rbac", "role_based", "roles"):
            logger.info("Using RoleBasedAuthorizationBackend (config-driven roles).")
            return RoleBasedAuthorizationBackend(
                role_scopes=authorization.get("roles") or authorization.get("role_scopes") or {},
                default_scopes=authorization.get("default_scopes") or (),
                isolation=authorization.get("isolation", "owner"),
                cache=cache,
                server_thread_ids=server_thread_ids,
            )
        raise ValueError(
            f"Unknown authorization backend config: {authorization!r}. For RBAC use "
            '{"backend": "rbac", "roles": {...}}.'
        )

    if authorization and ":" in authorization:
        backend = load_authorization(authorization)
        logger.info("Using custom AuthorizationBackend from '%s'.", authorization)
        return backend  # type: ignore[return-value]

    if authorization:
        key = authorization.strip().lower()
        builtin = _BUILTIN_AUTHORIZATION.get(key)
        if builtin is None:
            raise ValueError(
                f"Unknown authorization backend '{authorization}'. Use a 'module:attr' "
                f"path or one of: {', '.join(sorted(_BUILTIN_AUTHORIZATION))}."
            )
        logger.info("Using built-in '%s' authorization backend.", key)
        if builtin is DefaultAuthorizationBackend and auth_enabled and _is_production():
            logger.warning(
                'authorization is "%s" with auth enabled in production: every authenticated '
                'user can read and change every thread and memory. Use "ownership" or '
                '"rbac" unless that is intended.',
                key,
            )
        if builtin is OwnershipAuthorizationBackend:
            return OwnershipAuthorizationBackend(cache=cache, server_thread_ids=server_thread_ids)
        return builtin()

    # Not configured: owner-only whenever there is an identity to own threads.
    if auth_enabled or _is_production():
        logger.info(
            "No authorization configured; defaulting to OwnershipAuthorizationBackend "
            "(owner-only thread access). Set "
            '"authorization": "allow_all" to opt out.'
        )
        return OwnershipAuthorizationBackend(cache=cache, server_thread_ids=server_thread_ids)

    logger.info(
        "No authorization configured and auth is off; using DefaultAuthorizationBackend "
        "(requests carry no identity, so there is nothing to authorize)."
    )
    return DefaultAuthorizationBackend()


def _is_production() -> bool:
    from tenxgraph_api.src.app.core.config.settings import get_settings

    return get_settings().MODE == "production"


def load_and_bind_authorization(
    container: InjectQ,
    authorization_path: str | None,
    cache: BaseCacheStore | None = None,
    server_thread_ids: bool = False,
    auth_enabled: bool = False,
) -> None:
    """Bind the authorization backend; ``cache`` defaults to the bound ``BaseCacheStore``."""
    if cache is None:
        cache = container.try_get(BaseCacheStore)
    backend = _resolve_authorization_backend(
        authorization_path, cache, server_thread_ids, auth_enabled
    )
    if server_thread_ids and not isinstance(backend, OwnershipAuthorizationBackend):
        logger.debug(
            'runs.thread_ids is "server" but %s does not check thread ownership; client '
            "thread ids are only refused by the built-in ownership and rbac backends.",
            type(backend).__name__,
        )
    container.bind_instance(AuthorizationBackend, backend)


def _enable_token_revocation(container: InjectQ, config: GraphConfig, auth_enabled: bool) -> None:
    """Bind the ``token_revocation`` settings from 10xgraph.json, if it is on.

    The checks read the cache store bound by ``load_and_bind_cache_store``, so call this after
    it. With an in-memory cache store a revocation reaches only this process: warned about in
    production or with more than one worker.
    """
    from tenxgraph_api.src.app.core.auth.revocation import TokenRevocationConfig
    from tenxgraph_api.src.app.core.cache_store.loader import is_in_memory

    settings = config.token_revocation
    if settings is None:
        return
    if not auth_enabled:
        logger.warning("token_revocation is on but auth is off; there are no tokens to revoke.")
        return
    container.bind_instance(TokenRevocationConfig, settings)
    store = container.try_get(BaseCacheStore)
    logger.info("Token revocation enabled (cache store: %s).", type(store).__name__)
    workers = _web_concurrency()
    if is_in_memory(store) and (_is_production() or workers > 1):
        logger.warning(
            "token_revocation is on with an in-memory cache store (workers=%d): a revocation "
            "reaches only the process that recorded it, and `10xgraph revoke` cannot reach it "
            'at all. Set "cache_store": {"type": "redis", "url": ...} (or type custom) to '
            "share revocations between workers.",
            workers,
        )


def _web_concurrency() -> int:
    """Worker count from ``WEB_CONCURRENCY`` (Gunicorn's own variable); 1 when unset or bad."""
    try:
        return max(int(os.environ.get("WEB_CONCURRENCY", "1")), 1)
    except ValueError:
        return 1


def warn_per_process_state(container: InjectQ) -> None:
    """Warn when threads or caps live in one process and the deployment may run several.

    The in-memory and SQLite checkpointers keep threads in one process (or one file), and
    with an in-memory cache store the run locks, run and WebSocket caps and idempotency
    records are per process too. That is
    fine for one process; with more than one worker or replica every process has its own view.
    Warns in production or when ``WEB_CONCURRENCY`` is above 1 (the API cannot see replicas,
    so production always gets the line). The choice stays the developer's.
    """
    workers = _web_concurrency()
    if not (_is_production() or workers > 1):
        return
    from tenxgraph_api.src.app.core.cache_store.loader import is_in_memory

    checkpointer = container.try_get(BaseCheckpointer)
    kind = type(checkpointer).__name__
    if kind in {"InMemoryCheckpointer", "SqliteCheckpointer"}:
        logger.warning(
            "The checkpointer is %s: threads live in this process only (workers=%d). With more "
            "than one worker or replica, a thread saved by one is invisible to the others and "
            "is lost on restart. Use PgCheckpointer for a deployment with more than one process.",
            kind,
            workers,
        )
    if is_in_memory(container.try_get(BaseCacheStore)):
        logger.warning(
            "The cache store is in-memory: the per-thread run lock, run and WebSocket caps, "
            "idempotency records and in-memory rate limits are counted per process "
            "(workers=%d). With more than one worker or replica each limit multiplies and two "
            'processes can run the same thread at once. Set "cache_store": {"type": "redis", '
            '"url": ...} (or type custom) for a shared view.',
            workers,
        )


async def attach_all_modules(
    config: GraphConfig,
    container: InjectQ,
) -> CompiledGraph | None:
    remote_tools = config.remote_tools
    graph = await load_graph(config.graph_path)

    if remote_tools:
        if graph is None:
            raise RuntimeError("Cannot attach remote tools because the graph failed to load")
        grouped: dict[str, list[dict | CoreRemoteToolConfig]] = {}
        for tool in remote_tools:
            grouped.setdefault(tool.node_name, []).append(tool.to_tool_schema())
        for node_name, schemas in grouped.items():
            graph.attach_remote_tools(schemas, node_name)
        logger.info("Attached %d configured remote tools", len(remote_tools))

    if graph is not None:
        apply_id_generator(graph, config, container)

    logger.info("All modules attached successfully")

    if config.checkpointer_path:
        logger.warning(
            'The "checkpointer" key in 10xgraph.json is not applied by the server and is '
            "ignored. Pass the checkpointer to graph.compile(checkpointer=...) in your agent "
            "module instead."
        )

    # This binding we have done already in the library
    # # Bind checkpointer instance if configured
    # checkpointer = load_checkpointer(config.checkpointer_path)
    # container.bind_instance(BaseCheckpointer, checkpointer, allow_none=True)

    # Bind the store instance (if configured) so the /v1/store endpoints — which
    # inject BaseStore via StoreService — can serve it. Without this the store
    # router reports "Store is not configured" even when 10xgraph.json sets one.
    store = load_store(config.store_path)
    container.bind_instance(BaseStore, store, allow_none=True)

    # Bind the in-memory telemetry store so the graph service can record run
    # events and the /v1/observability endpoints can reconstruct traces.
    #
    # This is a dev-only convenience: it lets the playground show a trace without
    # any external observability backend. It is NOT durable and NOT meant for
    # production (use OTEL / a publisher there instead). So we only bind it
    # outside production; in production the store stays unbound and the
    # /v1/observability endpoints return a clean, empty "disabled" response.
    from tenxgraph_api.src.app.core.config.settings import get_settings
    from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

    if get_settings().MODE != "production":
        container.bind_instance(TelemetryStore, TelemetryStore())
        logger.info("Local in-memory telemetry store bound (dev mode)")
    else:
        logger.info("Production mode: local telemetry store disabled (use OTEL/publisher)")

    # load auth backend
    auth_config = config.auth_config()
    if auth_config:
        load_and_bind_auth(container, auth_config)
    else:
        # Auth disabled is a legitimate choice (single-user / trusted network), but it
        # must not be silent -- an operator who simply forgot to set `auth` should see
        # it at startup. This warns; it does not force auth on.
        logger.warning(
            "⚠️  Authentication is DISABLED (no `auth` configured). Every request runs "
            "as `anonymous` with no access control. This is fine for single-user or "
            "trusted-network deployments; set `auth` in 10xgraph.json to enable it."
        )
        # bind None
        container.bind_instance(BaseAuth, None, allow_none=True)

    # Thread names: random by default; None (``"none"``) leaves new threads unnamed.
    container.bind_instance(
        ThreadNameGenerator,
        resolve_thread_name_generator(config.thread_name_generator),
        allow_none=True,
    )

    # The cache store first: the ownership cache, token revocation and the rate limiter read
    # it from the container.
    load_and_bind_cache_store(container, config)

    # load authorization backend. The ownership cache's shared L2 tier is the cache store
    # (L1 only when it is in-memory).
    authorization_path = config.authorization_path
    load_and_bind_authorization(
        container,
        authorization_path,
        container.get(BaseCacheStore),
        server_thread_ids=config.runs.server_thread_ids,
        auth_enabled=bool(auth_config),
    )
    # Owner checks only run for authenticated requests. With auth on, an ownership backend
    # over a checkpointer that cannot name owners would deny every thread: fail at boot.
    if auth_config:
        assert_ownership_supported(
            container.get(AuthorizationBackend), container.try_get(BaseCheckpointer)
        )

    _enable_token_revocation(container, config, bool(auth_config))
    warn_per_process_state(container)

    # --- Media service wiring ---
    from tenxgraph_api.src.app.core.config.media_settings import (
        MediaSettings,
        get_media_settings,
    )
    from tenxgraph_api.src.app.routers.media import MediaService

    media_settings = get_media_settings()
    container.bind_instance(MediaSettings, media_settings)
    media_service = MediaService(settings=media_settings)
    container.bind_instance(MediaService, media_service)

    logger.info("Container loaded successfully")
    logger.debug(f"Container dependency graph: {container.get_dependency_graph()}")

    return graph
