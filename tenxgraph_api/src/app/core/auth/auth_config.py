"""The ``auth`` key of ``10xgraph.json``: one parser and one rule list.

Boot, ``10xgraph audit``, ``10xgraph build`` and the config editor all call
:func:`check_auth_config`, so a setup is refused (or warned about) the same way everywhere.
The functions here read only the config dict; the two that need user code or storage
objects (:func:`check_auth_instance`, :func:`check_tenant_claims`) are called at boot only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .rbac import RbacConfig, RoleConfig, roles_with_reach_all


if TYPE_CHECKING:
    from .base_auth import BaseAuth


# Keys that only make sense when an auth class runs.
RBAC_KEYS = (
    "roles",
    "roles_claim",
    "permissions_claim",
    "tenant_claims",
    "default_role",
    "system_identities",
)


class AuthIssue(BaseModel):
    """One finding. ``rule`` is a short stable id the CLI and tests can name."""

    model_config = ConfigDict(frozen=True)

    level: Literal["error", "warning"]
    rule: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.field}: {self.message}"


class SystemIdentityConfig(BaseModel):
    """One entry of ``auth.system_identities``: a non-user caller such as a scheduled job."""

    model_config = ConfigDict(extra="forbid")

    roles: list[str] = []
    tenant: dict[str, str] = {}


class AuthConfig(BaseModel):
    """The ``auth`` object. ``"jwt"`` and ``null`` are accepted by :meth:`parse` as shortcuts."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["none", "jwt", "custom"] = "none"
    path: str | None = None
    roles_claim: str | None = None
    permissions_claim: str | None = None
    tenant_claims: list[str] = []
    roles: dict[str, RoleConfig] = {}
    default_role: str | None = None
    system_identities: dict[str, SystemIdentityConfig] = {}

    @classmethod
    def parse(cls, raw: Any) -> AuthConfig:
        """Parse the raw ``auth`` value. Raises ``ValidationError`` or ``ValueError``."""
        if raw is None:
            return cls()
        if raw == "jwt":
            return cls(method="jwt")
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        raise ValueError(f'auth must be null, "jwt" or an object, got {raw!r}')

    def rbac_config(self) -> RbacConfig:
        """The RBAC part, validated by :class:`RbacConfig`. Raises ``ValidationError``."""
        return RbacConfig(
            roles_claim=self.roles_claim,
            permissions_claim=self.permissions_claim,
            tenant_claims=self.tenant_claims,
            roles=self.roles,
            default_role=self.default_role,
        )


def check_auth_config(data: dict[str, Any], *, production: bool) -> list[AuthIssue]:
    """Check the auth-related rules of a whole ``10xgraph.json`` dict.

    ``production`` only changes the severity of rules that differ by mode. Nothing here imports
    user code or touches storage. Any issue with level ``"error"`` means the server would not
    boot.
    """
    issues: list[AuthIssue] = []
    if "authorization" in data:
        issues.append(
            _error(
                "authorization-removed",
                "authorization",
                'The "authorization" key is removed. Use "auth": {"method": "custom", "path": '
                '"module:Class"} for your own checks, or "auth.roles" for role-based access.',
            )
        )
    if "graph_introspection" in data:
        issues.append(
            _error(
                "graph-introspection-removed",
                "graph_introspection",
                'The "graph_introspection" key is removed. Reading the graph needs the '
                "graph:read permission; do not grant it to hide graph internals.",
            )
        )
    raw = data.get("auth")
    try:
        auth = AuthConfig.parse(raw)
    except ValidationError as exc:
        issues.extend(_from_validation_error(exc, "auth-shape", "auth"))
        return issues
    except ValueError as exc:
        issues.append(_error("auth-shape", "auth", str(exc)))
        return issues

    issues.extend(_check_method(auth, raw))
    if auth.method != "none":
        issues.extend(_check_rbac(auth))
    elif production:
        issues.append(_warning("auth-off-production", "auth", _AUTH_OFF_PRODUCTION))
    return issues


_AUTH_OFF_PRODUCTION = (
    "MODE=production with auth off: any caller can use every endpoint, and no thread has an "
    'owner. Set "auth" to "jwt" or a custom BaseAuth unless a gateway in front does it.'
)


def _check_method(auth: AuthConfig, raw: Any) -> list[AuthIssue]:
    issues: list[AuthIssue] = []
    if auth.method == "none":
        used = [key for key in RBAC_KEYS if isinstance(raw, dict) and raw.get(key)]
        if used:
            issues.append(
                _error(
                    "rbac-without-auth",
                    "auth",
                    f"RBAC is set ({', '.join(used)}) but auth is off; set auth.method or "
                    "remove the RBAC keys.",
                )
            )
    if auth.method != "custom" and auth.path is not None:
        issues.append(
            _error(
                "path-not-custom",
                "auth.path",
                f'auth.path is only for method "custom", not "{auth.method}".',
            )
        )
    if auth.method == "custom":
        if not auth.path:
            issues.append(
                _error(
                    "custom-path", "auth.path", 'method "custom" needs auth.path "module:Class".'
                )
            )
        elif ":" not in auth.path:
            issues.append(
                _error(
                    "custom-path",
                    "auth.path",
                    f"auth.path {auth.path!r} must look like 'module:Class' "
                    "or 'dir/file.py:Class'.",
                )
            )
    return issues


def _check_rbac(auth: AuthConfig) -> list[AuthIssue]:
    try:
        rbac = auth.rbac_config()
    except ValidationError as exc:
        return _from_validation_error(exc, "rbac", "auth")
    issues = [
        _error(
            "system-identity-role",
            f"auth.system_identities.{name}.roles",
            f"system identity {name!r} uses role {role!r}, which is not in auth.roles.",
        )
        for name, identity in auth.system_identities.items()
        for role in identity.roles
        if role not in auth.roles
    ]
    reach_all = roles_with_reach_all(rbac)
    if reach_all:
        issues.append(
            _warning(
                "reach-all",
                "auth.roles",
                "roles with reach 'all' can read every row of every tenant: "
                + ", ".join(reach_all),
            )
        )
    return issues


def check_tenant_claims(
    tenant_claims: list[str],
    checkpointer: object | None,
    store: object | None,
    *,
    production: bool,
    allow_mismatch: bool = False,
) -> list[AuthIssue]:
    """Boot-time rule: every tenant claim is a declared tenant column of the checkpointer and
    the store. An error in production, a warning otherwise.

    ``allow_mismatch`` (``store_allow_tenant_mismatch``) leaves the store out of the check.
    """
    backends = [("checkpointer", checkpointer)]
    if store is not None and not allow_mismatch:
        backends.append(("store", store))
    level = "error" if production else "warning"
    issues = []
    for label, backend in backends:
        declared = getattr(backend, "tenant_column_names", ())
        missing = [c for c in tenant_claims if c not in declared]
        if missing:
            issues.append(
                AuthIssue(
                    level=level,
                    rule="tenant-columns",
                    field="auth.tenant_claims",
                    message=f"tenant_claims {missing} are not declared as tenant columns on the "
                    f"{label} ({type(backend).__name__}), so that data is not isolated by them. "
                    "Declare the same tenant columns on it.",
                )
            )
    return issues


def check_auth_instance(auth: BaseAuth, auth_config: AuthConfig) -> list[AuthIssue]:
    """Boot-time rule on the loaded class (a non-BaseAuth path already failed in the loader)."""
    from .base_auth import BaseAuth

    if auth_config.roles and type(auth).authorize is not BaseAuth.authorize:
        return [
            _warning(
                "authorize-override",
                "auth.roles",
                f"{type(auth).__name__} overrides authorize and auth.roles is set: the roles "
                "apply only if authorize calls super().authorize.",
            )
        ]
    return []


def _error(rule: str, field: str, message: str) -> AuthIssue:
    return AuthIssue(level="error", rule=rule, field=field, message=message)


def _warning(rule: str, field: str, message: str) -> AuthIssue:
    return AuthIssue(level="warning", rule=rule, field=field, message=message)


def _from_validation_error(exc: ValidationError, rule: str, prefix: str) -> list[AuthIssue]:
    """One error issue per pydantic error, with the field path and no 'Value error,' prefix."""
    issues = []
    for err in exc.errors():
        where = ".".join([prefix, *(str(part) for part in err["loc"])])
        message = err["msg"].removeprefix("Value error, ")
        issues.append(_error(rule, where, message))
    return issues
