import importlib
import inspect
import logging
import os
from pathlib import Path

from injectq import InjectQ
from tenxgraph.core import CompiledGraph
from tenxgraph.core.graph.remote_tool import RemoteToolConfig as CoreRemoteToolConfig
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.store import BaseStore

from tenxgraph_api import BaseAuth
from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
    OwnershipAuthorizationBackend,
    RoleBasedAuthorizationBackend,
    assert_ownership_supported,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.utils.thread_name_generator import ThreadNameGenerator


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


def _build_ownership_redis(redis_url: str | None) -> object | None:
    """Build an async Redis client for the ownership L2 cache, or None.

    Returns None (L1-only) when no URL is configured or the ``redis`` package is absent;
    the resolver degrades gracefully either way.
    """
    if not redis_url:
        return None
    try:
        from redis.asyncio import Redis as AsyncRedis  # type: ignore[import]
    except ImportError:
        logger.warning(
            "REDIS configured but the 'redis' package is not installed; ownership cache "
            "runs in-process only (L1). Install the redis extra to share it across workers."
        )
        return None
    try:
        client = AsyncRedis.from_url(redis_url, decode_responses=False)
        logger.info("Ownership authorization L2 cache enabled (shared Redis).")
        return client
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to build ownership Redis client: %s; using L1 cache only.", exc)
        return None


def _resolve_authorization_backend(
    authorization: str | dict | None,
    redis_url: str | None = None,
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

    ``redis_url`` (when set) backs the ownership cache's shared L2 tier.
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
                redis=_build_ownership_redis(redis_url),
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
            return OwnershipAuthorizationBackend(
                redis=_build_ownership_redis(redis_url), server_thread_ids=server_thread_ids
            )
        return builtin()

    # Not configured: owner-only whenever there is an identity to own threads.
    if auth_enabled or _is_production():
        logger.info(
            "No authorization configured; defaulting to OwnershipAuthorizationBackend "
            "(owner-only thread access). Set "
            '"authorization": "allow_all" to opt out.'
        )
        return OwnershipAuthorizationBackend(
            redis=_build_ownership_redis(redis_url), server_thread_ids=server_thread_ids
        )

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
    redis_url: str | None = None,
    server_thread_ids: bool = False,
    auth_enabled: bool = False,
) -> None:
    backend = _resolve_authorization_backend(
        authorization_path, redis_url, server_thread_ids, auth_enabled
    )
    if server_thread_ids and not isinstance(backend, OwnershipAuthorizationBackend):
        logger.debug(
            'runs.thread_ids is "server" but %s does not check thread ownership; client '
            "thread ids are only refused by the built-in ownership and rbac backends.",
            type(backend).__name__,
        )
    container.bind_instance(AuthorizationBackend, backend)


def _bind_token_revocation(
    container: InjectQ, config: GraphConfig, auth_enabled: bool, redis_url: str | None
) -> None:
    """Bind the ``token_revocation`` backend from 10xgraph.json, if one is configured.

    A backend the developer already bound in their InjectQ container is left alone.
    """
    from tenxgraph_api.src.app.core.auth.revocation import (
        TokenRevocationBackend,
        build_token_revocation,
    )

    spec = config.token_revocation
    if not isinstance(spec, str) or not spec:
        return
    if not auth_enabled:
        logger.warning("token_revocation is set but auth is off; there are no tokens to revoke.")
        return
    if container.try_get(TokenRevocationBackend) is not None:
        logger.info("Using the TokenRevocationBackend bound in the InjectQ container.")
        return
    container.bind_instance(TokenRevocationBackend, build_token_revocation(spec, redis_url))
    logger.info("Token revocation enabled (%s).", spec)


def _bind_shared_concurrency(container: InjectQ, redis_url: str | None) -> None:
    """Share the run and WebSocket connection caps across workers when Redis is configured.

    Without Redis each process counts its own slots, so a cap becomes ``limit x workers``. A
    ``ConcurrencyLimiter`` the developer already bound is left alone.
    """
    from tenxgraph_api.src.app.core.concurrency import (
        ConcurrencyLimiter,
        RedisConcurrencyLimiter,
    )

    if not redis_url or container.try_get(ConcurrencyLimiter) is not None:
        return
    try:
        limiter = RedisConcurrencyLimiter.from_url(redis_url)
    except ImportError:
        logger.warning(
            "REDIS configured but the 'redis' package is not installed; run and WebSocket "
            "caps are counted per process."
        )
        return
    container.bind_instance(ConcurrencyLimiter, limiter)
    logger.info("Run and WebSocket connection caps are shared through Redis.")


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

    logger.info("All modules attached successfully")

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

    # load thread name generator
    thread_name_generator_path = config.thread_name_generator_path
    if thread_name_generator_path:
        thread_name_generator = load_thread_name_generator(thread_name_generator_path)
        container.bind_instance(ThreadNameGenerator, thread_name_generator)
    else:
        # bind None if not configured
        container.bind_instance(ThreadNameGenerator, None, allow_none=True)

    # load authorization backend. The ownership cache's shared L2 tier reuses the
    # configured Redis URL (config `redis`, else settings.REDIS_URL); absent -> L1 only.
    from tenxgraph_api.src.app.core.config.settings import get_settings

    authorization_path = config.authorization_path
    ownership_redis_url = config.redis_url or get_settings().REDIS_URL
    load_and_bind_authorization(
        container,
        authorization_path,
        ownership_redis_url,
        server_thread_ids=config.runs.server_thread_ids,
        auth_enabled=bool(auth_config),
    )
    # Owner checks only run for authenticated requests. With auth on, an ownership backend
    # over a checkpointer that cannot name owners would deny every thread: fail at boot.
    if auth_config:
        assert_ownership_supported(
            container.get(AuthorizationBackend), container.try_get(BaseCheckpointer)
        )

    _bind_token_revocation(container, config, bool(auth_config), ownership_redis_url)
    _bind_shared_concurrency(container, ownership_redis_url)

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
