"""
Authorization backend system for 10xGraph CLI.

This module provides an authorization interface that developers can implement
to add resource-level access control to their 10xGraph applications.
"""

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from tenxgraph.core.authz import Isolation


if TYPE_CHECKING:
    from tenxgraph_api.src.app.core.cache_store import BaseCacheStore


logger = logging.getLogger("tenxgraph_api.authorization")

# The /v1/store routes require these. Core releases up to 0.9.1 named them "memory:*", so a
# "*" role would never reach the memory routes; they are added here until core is bumped.
STORE_SCOPES = frozenset({"store:read", "store:write", "store:delete"})
# Admin-only scope: POST /v1/threads/{id}/messages imports history of any role. Newer cores list
# it in ALL_SCOPES; it is added here so older cores still grant it to a "*" role.
CHECKPOINTER_ADMIN_SCOPES = frozenset({"checkpointer:import"})
# DELETE /v1/files/{file_id}. Core lists only files:read and files:upload, so it is added here.
FILES_DELETE_SCOPES = frozenset({"files:delete"})
LEGACY_MEMORY_SCOPES = frozenset({"memory:read", "memory:write", "memory:delete"})
# Core still lists it, but the setup endpoint was removed: no route checks it, so a role rule
# for it would guard nothing.
REMOVED_SCOPES = frozenset({"graph:setup"})


def all_scopes() -> frozenset[str]:
    """Every scope the API can require, which is what a ``"*"`` role is granted."""
    from tenxgraph.core.authz import ALL_SCOPES

    return (
        (frozenset(ALL_SCOPES) - LEGACY_MEMORY_SCOPES - REMOVED_SCOPES)
        | STORE_SCOPES
        | CHECKPOINTER_ADMIN_SCOPES
        | FILES_DELETE_SCOPES
    )


class ScopeClaimError(ValueError):
    """A scope claim is present but not a string or a collection of strings."""


def _split_scope_claim(name: str, value: Any) -> set[str]:
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, list | tuple | set | frozenset) and all(
        isinstance(item, str) for item in value
    ):
        return {item for item in value if item}
    raise ScopeClaimError(f"Invalid {name!r} claim: expected a string or a list of strings")


def scopes_from_claims(user: Any) -> list[str] | None:
    """Exact scope names an identity carries, or None when it has no scope claim.

    Reads the OAuth ``scope`` claim (a space-separated string) and ``scopes`` (a list, tuple or
    set of strings, or a space-separated string) and merges them. Callers compare with
    ``required in scopes``, so a scope only matches by exact name, never as a substring.

    Raises:
        ScopeClaimError: a claim is present but of the wrong type (number, dict, a list with
            non-strings, ...). ``RequirePermission`` turns it into a 401.
    """
    if not isinstance(user, dict):
        return None
    present = [name for name in ("scope", "scopes") if user.get(name) is not None]
    if not present:
        return None
    granted: set[str] = set()
    for name in present:
        granted |= _split_scope_claim(name, user[name])
    return sorted(granted)


def _accepts_user(fn: Any) -> bool:
    """Whether ``isolation_scope`` takes the user (the 1.0 form) or nothing (older backends)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for p in params
    )


def resolve_isolation_scope(backend: Any, user: dict[str, Any] | None) -> str | list[str]:
    """Call ``backend.isolation_scope`` for this caller.

    ``isolation_scope(user)`` is the current form; backends written before it take no
    argument, so the signature decides how it is called (never a retry on ``TypeError``,
    which could hide a bug inside the method). A backend without the method gets
    ``"owner"``, the :class:`AuthorizationBackend` default (fail closed): sharing data takes
    an explicit ``"none"``.
    """
    fn = getattr(backend, "isolation_scope", None)
    if not callable(fn):
        return "owner"
    return fn(user) if _accepts_user(fn) else fn()


class AuthorizationBackend(ABC):
    """
    Abstract base class for authorization backends.

    Developers should implement this class to define custom authorization logic
    for their 10xGraph applications. The authorize method is called before
    any resource operation to check if the user has permission.

    Storage isolation is separate from ``authorize``: ``isolation_scope(user)`` decides which
    columns threads and memories are scoped by for this caller. It defaults to ``"owner"``
    (``user_id`` plus every declared tenant column), so a backend that implements only
    ``authorize`` is isolated. Return ``"none"`` on purpose to let every authenticated user
    see the same data, or a column list such as ``["org_id"]`` for an org admin.

    Example:
        class MyAuthorizationBackend(AuthorizationBackend):
            async def authorize(self, user, resource, action, resource_id=None, **context):
                # Check if user has permission
                if user.get("role") == "admin":
                    return True
                # Add custom logic here
                return False

            def isolation_scope(self, user):
                # "owner" (the default): each user sees only their own threads and memories.
                # An org admin sees every thread in their org.
                if user.get("role") == "org_admin":
                    return ["org_id"]
                return "owner"
    """

    @abstractmethod
    async def authorize(
        self,
        user: dict[str, Any],
        resource: str,
        action: str,
        resource_id: str | None = None,
        **context: Any,
    ) -> bool:
        """
        Check if user can perform action on resource.

        Args:
            user: User information dictionary containing at least 'user_id'
            resource: Resource type (e.g., 'graph', 'checkpointer', 'store')
            action: Action to perform (e.g., 'invoke', 'stream', 'read', 'write', 'delete')
            resource_id: Optional specific resource identifier (e.g., thread_id, namespace)
            **context: Additional context for authorization decision

        Returns:
            bool: True if authorized, False otherwise

        Raises:
            Exception: Can raise exceptions for auth failures or errors
        """

    def isolation_scope(self, user: dict[str, Any] | None = None) -> str | list[str]:
        """The data-isolation policy for this caller, sent to storage per request.

        Called once per request with the authenticated ``user`` (the dict your auth class
        returned). The API stamps the result (server-side, non-hijackable) into the trusted
        ``authz`` block (``tenxgraph.core.authz.build_authz``) so the checkpointer and store
        scope storage the *same* way this backend decides access, and the built-in ownership
        backends compare the same columns when a request names a thread.

        - ``"owner"`` -> ``["user_id", *tenant_columns]``: only the caller's own rows.
        - ``"none"``  -> ``[]``: storage is not scoped (any authenticated user sees everything).
        - a list of ``"user_id"`` and declared tenant column names (``tenant_columns=`` on
          ``PgCheckpointer`` / ``PgVectorStore``): rows matching the caller's values on those
          columns, e.g. ``["org_id"]`` for an org admin. Naming a column the storage backend
          does not declare fails the request.

        The caller's values come only from ``user`` (``config["user"]``), never from request
        config. A picked tenant column the user lacks fails the request rather than being
        dropped. Backends written before 1.0 define ``isolation_scope(self)`` with no
        argument; they keep working.

        Defaults to ``"owner"`` (fail closed): a custom backend that implements only
        ``authorize`` still isolates users. Override and return ``"none"`` to share data on
        purpose.
        """
        return "owner"

    def scopes_for(self, user: dict[str, Any]) -> list[str] | None:
        """Return the scopes this identity is granted, or None for "unrestricted".

        ``RequirePermission`` checks the required ``"<resource>:<action>"`` scope against
        this list. Returning ``None`` means the identity is not scope-restricted
        (permissive -- the default, so nothing breaks until scopes are issued).

        The default passes through the scopes the identity already carries: the OAuth
        ``scope`` claim (space-separated) and ``scopes`` (list or space-separated string),
        merged into exact names (see :func:`scopes_from_claims`). A role-based backend
        overrides this to map ``user["roles"]`` to scopes.

        Raises:
            ScopeClaimError: a scope claim has the wrong type.
        """
        return scopes_from_claims(user)


class DefaultAuthorizationBackend(AuthorizationBackend):
    """
    Default authorization backend that allows all authenticated users.

    This implementation performs basic authentication check (user has user_id)
    but allows all operations. Use this as a starting point or for development.

    For production use, implement a custom AuthorizationBackend with proper
    access control logic based on your application's requirements.
    """

    async def authorize(
        self,
        user: dict[str, Any],
        resource: str,
        action: str,
        resource_id: str | None = None,
        **context: Any,
    ) -> bool:
        """
        Allow all authenticated users to perform any action.

        Args:
            user: User information dictionary
            resource: Resource type (not used in default implementation)
            action: Action to perform (not used in default implementation)
            resource_id: Optional resource identifier (not used in default implementation)
            **context: Additional context (not used in default implementation)

        Returns:
            bool: True if user has 'user_id', False otherwise
        """
        # Only check if user is authenticated (has user_id)
        return bool(user.get("user_id"))

    def isolation_scope(self, user: dict[str, Any] | None = None) -> str | list[str]:
        # allow_all shares data on purpose: authenticated == authorized, no per-user scoping.
        return "none"


class OwnershipAuthorizationBackend(AuthorizationBackend):
    """Object-level authorization: a user may only touch threads in their isolation scope.

    This is the built-in "secure" backend. It enforces the rule most multi-tenant
    apps want without the developer writing any code: *a thread can be read, mutated,
    stopped or fixed only by the user who owns it.* Ownership is resolved from the
    configured checkpointer, whose thread registry records the owning ``user_id`` and, with
    ``PgCheckpointer(tenant_columns=...)``, the thread's tenant values.

    "Owns" follows :meth:`isolation_scope`: a thread is accessible when its values equal the
    caller's on every column the caller's scope picks (``"owner"`` = ``user_id`` and every
    tenant column; a list such as ``["org_id"]`` lets an org admin reach every thread of the
    org; ``"none"`` picks no column, so any existing thread). The caller's values come from
    the authenticated user only. A picked column the thread or the caller has no value for
    denies.

    Decision table (for the thread-scoped resources ``graph`` and ``checkpointer``):

    - No ``user_id`` on the request -> deny (must be authenticated).
    - No ``resource_id`` (list / create-without-id, e.g. ``GET /v1/threads``, the graph
      schema endpoint) -> allow; those paths are already user-scoped by the service.
    - The thread has no owner yet -> allow only the actions that create it (``graph:invoke``,
      ``graph:stream``, ``checkpointer:write``); the caller becomes its owner. With
      ``server_thread_ids`` (``runs.thread_ids == "server"``) even those are denied: new
      threads must be started without an id, so the server picks it. Every route, AG-UI
      and ``/v1/graph/live`` included, follows this rule. Every other
      action (read, delete, stop, fix) is denied: there is nothing of the caller's to act
      on, and an ownerless thread may still hold data written before owners were recorded.
    - The thread exists and matches the caller on every scope column -> allow.
    - The thread exists and does not match -> **deny, for every action** including
      ``invoke`` and ``stream``. This is the whole point: you cannot read, continue, stop,
      fix, or run another user's (or another tenant's) thread.

    Resources other than ``graph``/``checkpointer`` (store, files) are allowed through:
    they enforce their own per-user scoping (the store keys on ``user_id``; media checks
    file ownership in ``MediaService``).

    Ownership is resolved through a :class:`ThreadOwnershipResolver` in front of
    :meth:`BaseCheckpointer.aget_thread_scope` (``aget_thread_owner`` for a checkpointer
    without it). A thread's values are immutable, so the resolver caches them (in-process,
    plus the shared cache store when one is configured) -- after the first lookup a check is
    an in-memory hit, not a database round-trip per request. Pass the API's
    ``BaseCacheStore`` as ``cache``; the backend uses it but never closes it.

    This backend fails closed: with no checkpointer, or one that cannot resolve ownership
    (``aget_thread_owner`` raises ``NotImplementedError``), every thread-scoped request is
    denied, and so is a scope naming a tenant column the checkpointer does not declare or
    answer. The loader refuses to start without an owner lookup (see
    :func:`assert_ownership_supported`), so this only guards a misconfiguration that slipped
    past it.
    """

    # Resources whose ``resource_id`` is a thread_id and therefore ownership-checkable.
    THREAD_RESOURCES = frozenset({"graph", "checkpointer"})

    # ``(resource, action)`` pairs that may target a thread with no owner yet. Each one
    # records the caller as the owner (the graph service and the checkpointer service both
    # register the thread before writing to it).
    CREATE_ACTIONS = frozenset(
        {
            ("graph", "invoke"),
            ("graph", "stream"),
            ("checkpointer", "write"),
            ("checkpointer", "import"),
        }
    )

    def isolation_scope(self, user: dict[str, Any] | None = None) -> str | list[str]:
        # Owner-only access -> storage must be scoped to the caller too.
        return "owner"

    def __init__(
        self,
        checkpointer: Any | None = None,
        *,
        resolver: Any | None = None,
        cache: "BaseCacheStore | None" = None,
        server_thread_ids: bool = False,
    ) -> None:
        self._checkpointer = checkpointer
        self._resolver = resolver
        self._cache = cache
        self._resolver_built = resolver is not None
        # ``runs.thread_ids == "server"``: a request may only name a thread that exists.
        self.server_thread_ids = server_thread_ids

    @property
    def checkpointer(self) -> Any | None:
        """Resolve the checkpointer lazily from the DI container if not injected."""
        if self._checkpointer is None:
            try:
                from injectq import InjectQ
                from tenxgraph.storage.checkpointer import BaseCheckpointer

                self._checkpointer = InjectQ.get_instance().try_get(BaseCheckpointer)
            except Exception:  # pragma: no cover - defensive
                self._checkpointer = None
        return self._checkpointer

    def _get_resolver(self) -> Any | None:
        """Return the cached ownership resolver, building it once from the checkpointer.

        Returns None when no checkpointer is available (nothing to resolve).
        """
        if self._resolver_built:
            return self._resolver
        self._resolver_built = True
        cp = self.checkpointer
        if cp is None:
            self._resolver = None
        else:
            from tenxgraph_api.src.app.core.auth.ownership_resolver import (
                ThreadOwnershipResolver,
            )

            self._resolver = ThreadOwnershipResolver(
                scope_lookup(cp), cache=self._cache, tenant_columns=self._tenant_columns()
            )
        return self._resolver

    def _tenant_columns(self) -> tuple[str, ...]:
        """The checkpointer's declared tenant column names (empty for most checkpointers)."""
        names = getattr(self.checkpointer, "tenant_column_names", None)
        if not isinstance(names, Sequence) or isinstance(names, str):
            return ()
        return tuple(str(name) for name in names)

    def _scope_columns(self, user: dict[str, Any]) -> tuple[str, ...] | None:
        """The columns this caller's isolation scope compares, or None to deny.

        Resolved exactly as storage resolves the stamped block
        (``tenxgraph.core.authz.isolation_columns``), against the checkpointer's tenant
        columns. A scope storage could not apply (an unknown string, a column the
        checkpointer does not declare) denies instead of guessing.
        """
        from tenxgraph.core.authz import SCOPE_NONE, SCOPE_OWNER, build_authz, isolation_columns

        scope = resolve_isolation_scope(self, user)
        try:
            if isinstance(scope, str) and scope not in (SCOPE_OWNER, SCOPE_NONE):
                raise ValueError(f"unknown isolation scope {scope!r}")
            if not isinstance(scope, str | list | tuple):
                raise ValueError(f"isolation scope must be a string or a list, got {scope!r}")
            block = build_authz(user.get("user_id"), scope=scope)
            return isolation_columns({"authz": block}, self._tenant_columns())
        except ValueError as exc:
            logger.error(
                "%s.isolation_scope returned a scope the checkpointer cannot apply (%s); "
                "denying thread access.",
                type(self).__name__,
                exc,
            )
            return None

    def evict(self, thread_id: str | int, owner: str | int | None = None) -> Any:
        """Invalidate a thread's cached ownership (call on thread deletion).

        ``owner`` is the deleting user; they may recreate the id straight away. Returns the
        resolver's ``evict`` coroutine when a resolver exists, so callers may ``await`` it;
        returns None when there is nothing to evict.
        """
        resolver = self._get_resolver()
        if resolver is None:
            return None
        return resolver.evict(thread_id, owner)

    async def authorize(
        self,
        user: dict[str, Any],
        resource: str,
        action: str,
        resource_id: str | None = None,
        **context: Any,
    ) -> bool:
        user_id = user.get("user_id") if user else None
        if not user_id:
            return False

        # Only thread-scoped resources are ownership-checked here.
        if resource not in self.THREAD_RESOURCES:
            return True

        # Endpoints without a specific thread (list/create) are user-scoped by the
        # service layer; nothing to authorize at the object level.
        if resource_id is None:
            return True

        resolver = self._get_resolver()
        if resolver is None:
            logger.error(
                "Ownership authorization is active but no checkpointer is configured; "
                "denying %s:%s on thread %s.",
                resource,
                action,
                resource_id,
            )
            return False

        columns = self._scope_columns(user)
        if columns is None:
            return False

        return await self._authorize_ownership(
            resolver,
            str(resource_id),
            user,
            columns,
            resource,
            action,
            client_thread_ids=context.get("client_thread_ids") is True,
        )

    async def _authorize_ownership(  # noqa: PLR0911
        self,
        resolver: Any,
        resource_id: str,
        user: dict[str, Any],
        columns: tuple[str, ...],
        resource: str,
        action: str,
        *,
        client_thread_ids: bool = False,
    ) -> bool:
        user_id = str(user.get("user_id"))
        try:
            thread_scope = await _resolve_scope(resolver, resource_id, columns)
        except NotImplementedError:
            logger.error(
                "Checkpointer %s cannot resolve thread ownership; denying %s:%s. Use a "
                "checkpointer that implements aget_thread_owner (e.g. PgCheckpointer) or "
                "configure a custom AuthorizationBackend.",
                type(self.checkpointer).__name__,
                resource,
                action,
            )
            return False
        except Exception as exc:
            # Fail closed: a resolution error must not silently grant access.
            logger.warning(
                "Ownership check failed for thread %s (user %s): %s; denying.",
                resource_id,
                user_id,
                exc,
            )
            return False

        # No owner yet: only an action that creates the thread may proceed, and it makes the
        # caller the owner. Otherwise the caller must match the thread, for EVERY action.
        if thread_scope is None:
            if await self._recently_deleted(resolver, resource_id, user_id):
                # Another worker may still cache the deleted thread's owner; handing the id to
                # a new owner now would let that stale entry reach into the new thread.
                logger.info(
                    "Refusing %s:%s on thread %s: it was just deleted",
                    resource,
                    action,
                    resource_id,
                )
                return False
            if self.server_thread_ids and not client_thread_ids:
                # New threads get server-picked ids; a client may not name one into being,
                # so nobody can claim an id ahead of the user it was meant for. No built-in
                # route passes ``client_thread_ids``; it stays for custom callers.
                logger.info(
                    "Refusing %s:%s on unknown thread %s: runs.thread_ids is 'server'",
                    resource,
                    action,
                    resource_id,
                )
                return False
            return (resource, action) in self.CREATE_ACTIONS
        if self._matches(thread_scope, user, columns):
            return True
        missing = [column for column in columns if column not in thread_scope]
        if missing:
            logger.error(
                "Checkpointer %s did not return %s for thread %s, which the isolation scope "
                "compares; denying %s:%s. Declare the column with tenant_columns= on "
                "PgCheckpointer or return it from aget_thread_scope.",
                type(self.checkpointer).__name__,
                ", ".join(missing),
                resource_id,
                resource,
                action,
            )
        return False

    def _matches(
        self, thread_scope: dict[str, Any], user: dict[str, Any], columns: tuple[str, ...]
    ) -> bool:
        """Whether the thread's values equal the caller's on every scope column.

        The caller's values come from the authenticated user only (``config["user"]`` once a
        service copies it), converted with the column's declared type when the checkpointer
        declares it. A column with no value on either side never matches.
        """
        from tenxgraph.storage.columns import ColumnSet, user_value

        from tenxgraph_api.src.app.core.auth.ownership_resolver import canonical_value

        specs = getattr(self.checkpointer, "columns", None)
        if not isinstance(specs, ColumnSet):
            specs = None  # a custom checkpointer: compare the values as text
        for column in columns:
            expected = thread_scope.get(column)
            if expected is None:
                return False
            value = user_value(user, column)
            if value is None:
                return False
            if column != "user_id":
                spec = specs.get(column) if specs is not None else None
                if spec is not None:
                    try:
                        value = spec.convert(value)
                    except ValueError:
                        return False
                    if value is None:
                        return False
            if canonical_value(value) != expected:
                return False
        return True

    @staticmethod
    async def _recently_deleted(resolver: Any, resource_id: str, user_id: str) -> bool:
        check = getattr(resolver, "recently_deleted", None)
        if not callable(check):
            return False
        try:
            return bool(await check(resource_id, user_id))
        except Exception as exc:  # pragma: no cover - the resolver already swallows errors
            logger.debug("Tombstone check failed for thread %s: %s", resource_id, exc)
            return False


def _has_scope_lookup(checkpointer: Any) -> bool:
    return inspect.iscoroutinefunction(getattr(checkpointer, "aget_thread_scope", None))


def scope_lookup(checkpointer: Any) -> Any:
    """The checkpointer's thread scope lookup: ``aget_thread_scope``, else ``aget_thread_owner``.

    ``BaseCheckpointer.aget_thread_scope`` answers ``{"user_id": owner}`` through
    ``aget_thread_owner`` by default and ``PgCheckpointer`` adds its tenant values. A
    checkpointer (or an older core) without it falls back to ``aget_thread_owner``, which the
    resolver reads as ``{"user_id": owner}``; scopes that pick tenant columns then deny.
    """
    if _has_scope_lookup(checkpointer):
        return checkpointer.aget_thread_scope
    return checkpointer.aget_thread_owner


async def _resolve_scope(
    resolver: Any, thread_id: str, columns: tuple[str, ...]
) -> dict[str, Any] | None:
    """The thread's scope through ``resolver``; a custom resolver with only ``owner_of`` works."""
    scope_of = getattr(resolver, "scope_of", None)
    if callable(scope_of):
        return await scope_of(thread_id, columns)
    from tenxgraph_api.src.app.core.auth.ownership_resolver import to_scope

    return to_scope(await resolver.owner_of(thread_id))


def supports_owner_lookup(checkpointer: Any) -> bool:
    """Whether ``checkpointer`` can answer ``aget_thread_owner`` (or ``aget_thread_scope``).

    A :class:`BaseCheckpointer` subclass that overrides neither inherits base methods that
    always raise ``NotImplementedError``. Other objects are taken at their word if they have
    one of the methods.
    """
    if checkpointer is None:
        return False
    from tenxgraph.storage.checkpointer import BaseCheckpointer

    if isinstance(checkpointer, BaseCheckpointer):
        cls = type(checkpointer)
        if cls.aget_thread_owner is not BaseCheckpointer.aget_thread_owner:
            return True
        base_scope = getattr(BaseCheckpointer, "aget_thread_scope", None)
        return base_scope is not None and getattr(cls, "aget_thread_scope", None) is not base_scope
    return callable(getattr(checkpointer, "aget_thread_owner", None)) or _has_scope_lookup(
        checkpointer
    )


def assert_ownership_supported(backend: AuthorizationBackend, checkpointer: Any) -> None:
    """Refuse to start when owner-only authorization has nothing to check ownership with.

    Without this, every ownership check would be denied at request time (the backend fails
    closed), so the server would start and then reject every thread. Raising at boot names the
    real problem instead.
    """
    if not isinstance(backend, OwnershipAuthorizationBackend):
        return
    if supports_owner_lookup(checkpointer):
        return
    name = type(checkpointer).__name__ if checkpointer is not None else "no checkpointer"
    raise RuntimeError(
        f"Refusing to start: {type(backend).__name__} enforces owner-only thread access, but "
        f"{name} cannot resolve thread owners (aget_thread_owner is not implemented). Use a "
        "checkpointer that implements it (PgCheckpointer, SqliteCheckpointer, "
        "InMemoryCheckpointer), implement aget_thread_owner on your checkpointer, or set "
        '"authorization" to "allow_all" or a custom backend in 10xgraph.json.'
    )


def _isolation_shortcut(isolation: Any) -> str:
    """``"owner"`` or ``"none"`` for an exact string or ``Isolation`` member; else raise.

    An unknown value (``"owners"``, ``None``, ``True``) raises ``ValueError`` at construction,
    so a server with a typo in ``authorization.isolation`` refuses to boot instead of
    quietly picking an isolation level.
    """
    if isinstance(isolation, str):
        try:
            return Isolation(isolation).value
        except ValueError:
            pass
    valid = ", ".join(repr(m.value) for m in Isolation)
    raise ValueError(
        f"Unknown authorization isolation {isolation!r}: use {valid} "
        "(tenxgraph.core.authz.Isolation)"
    )


class RoleBasedAuthorizationBackend(OwnershipAuthorizationBackend):
    """RBAC: map a user's roles to scopes, on top of owner-only object isolation.

    Combines two things in one backend:

    - **Scopes** (what actions are allowed) -- ``scopes_for`` maps ``user["roles"]`` (or
      ``user["role"]``) to a set of scopes via the ``role_scopes`` table. ``RequirePermission``
      then enforces the required ``"<resource>:<action>"`` scope. A role granting ``"*"``
      gets every scope in :func:`all_scopes`.
    - **Isolation** (which rows are visible) -- inherited from
      :class:`OwnershipAuthorizationBackend` (owner-only threads), configurable via
      ``isolation``: ``"owner"`` (default) or ``"none"``, or an ``Isolation`` member. Any
      other value raises ``ValueError`` when the backend is built, so the server refuses
      to boot rather than guess.

    Example::

        RoleBasedAuthorizationBackend(
            role_scopes={
                "admin": ["*"],
                "member": ["graph:invoke", "graph:stream", "checkpointer:read"],
            },
            default_scopes=["graph:read"],  # granted to everyone, even with no role
        )
    """

    def __init__(
        self,
        role_scopes: dict[str, Any] | None = None,
        *,
        default_scopes: Any = (),
        isolation: str | Isolation = "owner",
        checkpointer: Any | None = None,
        resolver: Any | None = None,
        cache: "BaseCacheStore | None" = None,
        server_thread_ids: bool = False,
    ) -> None:
        super().__init__(
            checkpointer=checkpointer,
            resolver=resolver,
            cache=cache,
            server_thread_ids=server_thread_ids,
        )
        self._role_scopes = {str(r): list(s) for r, s in (role_scopes or {}).items()}
        self._default_scopes = list(default_scopes)
        self._isolation = _isolation_shortcut(isolation)

    def isolation_scope(self, user: dict[str, Any] | None = None) -> str | list[str]:
        return self._isolation

    @staticmethod
    def _roles_of(user: dict[str, Any]) -> list[str]:
        if not isinstance(user, dict):
            return []
        roles = user.get("roles")
        if roles is None:
            role = user.get("role")
            roles = [role] if role else []
        elif isinstance(roles, str):
            roles = [roles]
        return [str(r) for r in roles]

    def scopes_for(self, user: dict[str, Any]) -> list[str]:
        granted: set[str] = set(self._default_scopes)
        for role in self._roles_of(user):
            granted.update(self._role_scopes.get(role, []))
        if "*" in granted:
            return sorted(all_scopes())
        return sorted(granted)
