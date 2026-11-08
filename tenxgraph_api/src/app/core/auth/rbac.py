"""RBAC config and the pure ``resolve(claims, config) -> Identity`` function."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator
from tenxgraph.core.authz import Identity, Reach

from .permissions_registry import DEFAULT_PERMISSIONS, expand


logger = logging.getLogger("tenxgraph_api.auth")

_REACH_ORDER = {Reach.OWN: 0, Reach.TENANT: 1, Reach.ALL: 2}


class InvalidClaimsError(Exception):
    """The token claims are malformed for the configured auth; the API answers 401."""


class RoleConfig(BaseModel):
    """One entry of ``auth.roles``. ``reach`` is required: it never has a default."""

    model_config = ConfigDict(extra="forbid")

    permissions: list[str] = []
    inherits: list[str] = []
    reach: Reach


class RbacConfig(BaseModel):
    """The RBAC keys of the ``auth`` object. Keys it does not own (``method``, ``path``,
    ``system_identities``) are ignored here; the full ``auth`` loader owns them."""

    model_config = ConfigDict(extra="ignore")

    roles_claim: str | None = None
    permissions_claim: str | None = None
    tenant_claims: list[str] = []
    roles: dict[str, RoleConfig] = {}
    default_role: str | None = None

    _effective: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> RbacConfig:
        if self.permissions_claim and (self.roles_claim or self.roles):
            raise ValueError(
                "roles_claim/roles and permissions_claim are both set; pick one permission source"
            )
        if self.roles_claim and not self.roles:
            raise ValueError("roles_claim is set but no roles are defined")
        if self.roles and not self.roles_claim:
            self.roles_claim = "roles"
        if self.default_role is not None and self.default_role not in self.roles:
            raise ValueError(f"default_role {self.default_role!r} is not in roles")
        own: dict[str, frozenset[str]] = {}
        for name, role in self.roles.items():
            try:
                own[name] = expand(role.permissions)
            except ValueError as exc:
                raise ValueError(f"role {name!r}: {exc}") from exc
            for parent in role.inherits:
                if parent not in self.roles:
                    raise ValueError(f"role {name!r} inherits {parent!r}, which is not in roles")
            if role.reach is Reach.TENANT and not self.tenant_claims:
                raise ValueError(f"role {name!r} has reach 'tenant' but tenant_claims is empty")
        self._effective = self._inherit(own)
        return self

    def _inherit(self, own: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
        done: dict[str, frozenset[str]] = {}

        def visit(name: str, path: list[str]) -> frozenset[str]:
            if name in done:
                return done[name]
            if name in path:
                cycle = " -> ".join([*path[path.index(name) :], name])
                raise ValueError(f"role inheritance cycle: {cycle}")
            perms = own[name]
            for parent in self.roles[name].inherits:
                perms = perms | visit(parent, [*path, name])
            done[name] = perms
            return perms

        for name in self.roles:
            visit(name, [])
        return done

    def role_permissions(self, name: str) -> frozenset[str]:
        """Effective permissions of a role: its own plus inherited, computed at parse."""
        return self._effective[name]


def roles_with_reach_all(config: RbacConfig | None) -> list[str]:
    """Names of roles whose own declared reach is ``all``."""
    if config is None:
        return []
    return [name for name, role in config.roles.items() if role.reach is Reach.ALL]


def log_reach_all_roles(config: RbacConfig | None) -> None:
    """Warn once per role that can see every row."""
    for name in roles_with_reach_all(config):
        logger.warning("Role %r has reach 'all': it can read every row of every tenant", name)


def resolve(claims: dict[str, Any], config: RbacConfig | None) -> Identity:
    """Build the caller's :class:`Identity` from verified token claims. No I/O.

    Raises:
        InvalidClaimsError: a claim the config relies on is missing or malformed.
    """
    user_id = claims.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise InvalidClaimsError("token has no usable user_id claim")
    tenant = _tenant(claims, config.tenant_claims if config else [])

    roles: frozenset[str] = frozenset()
    reach = Reach.OWN
    if config is not None and config.roles:
        roles, permissions, reach = _from_roles(claims, config)
    elif config is not None and config.permissions_claim:
        entries = _string_list(claims.get(config.permissions_claim), config.permissions_claim)
        permissions = _expand_known(entries or [])
    else:
        permissions = DEFAULT_PERMISSIONS

    permissions = _narrow(claims, permissions)
    return Identity(
        user_id=user_id,
        kind="user",
        tenant=tenant,
        roles=roles,
        permissions=permissions,
        reach=reach,
        claims=dict(claims),
    )


def _tenant(claims: dict[str, Any], names: list[str]) -> dict[str, str]:
    tenant: dict[str, str] = {}
    for name in names:
        value = claims.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not value:
            raise InvalidClaimsError(f"token has no usable tenant claim {name!r}")
        tenant[name] = value
    return tenant


def _string_list(value: Any, claim: str) -> list[str] | None:
    """A list of str or a space-separated str; None when the claim is missing."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise InvalidClaimsError(
        f"claim {claim!r} must be a list of strings or a space-separated string"
    )


def _from_roles(
    claims: dict[str, Any], config: RbacConfig
) -> tuple[frozenset[str], frozenset[str], Reach]:
    claim = config.roles_claim or "roles"
    names = _string_list(claims.get(claim), claim)
    if names is None:
        names = [config.default_role] if config.default_role else []
    held = []
    for name in names:
        if name in config.roles:
            held.append(name)
        else:
            logger.debug("Ignoring unknown role %r in token", name)
    permissions: frozenset[str] = frozenset().union(*(config.role_permissions(n) for n in held))
    reaches = (config.roles[n].reach for n in held)
    reach = max(reaches, key=_REACH_ORDER.__getitem__, default=Reach.OWN)
    return frozenset(held), permissions, reach


def _expand_known(entries: list[str]) -> frozenset[str]:
    """Expand entries, skipping (and logging) the ones the registry does not know."""
    known: set[str] = set()
    for entry in entries:
        try:
            known |= expand([entry])
        except ValueError:
            logger.debug("Ignoring unknown permission %r in token", entry)
    return frozenset(known)


def _narrow(claims: dict[str, Any], granted: frozenset[str]) -> frozenset[str]:
    """Token ``scope``/``scopes`` only narrow: granted intersected with permission-shaped scopes.

    An entry is permission-shaped when it is ``*`` or contains ``:``. Unknown permission-shaped
    entries still turn narrowing on and add nothing (a stale ``checkpointer:read`` never means
    "no narrowing"). Plain OIDC scopes such as ``openid profile`` are ignored.
    """
    entries = (_string_list(claims.get("scope"), "scope") or []) + (
        _string_list(claims.get("scopes"), "scopes") or []
    )
    shaped = [e for e in entries if e == "*" or ":" in e]
    if not shaped:
        return granted
    return granted & _expand_known(shaped)
