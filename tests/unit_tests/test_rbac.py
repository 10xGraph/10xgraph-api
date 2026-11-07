import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tenxgraph.core.authz import Reach

from tenxgraph_api.src.app.core.auth.permissions_registry import (
    ALL_PERMISSIONS,
    DEFAULT_PERMISSIONS,
)
from tenxgraph_api.src.app.core.auth.rbac import (
    InvalidClaimsError,
    RbacConfig,
    log_reach_all_roles,
    resolve,
    roles_with_reach_all,
)


def cfg(**kw) -> RbacConfig:
    return RbacConfig.model_validate(kw)


ROLES = {
    "member": {"permissions": ["graph:*", "threads:read"], "reach": "own"},
    "org_admin": {"inherits": ["member"], "permissions": ["admin:read"], "reach": "tenant"},
    "super": {"permissions": ["*"], "reach": "all"},
}
BASE = {"user_id": "u1"}


def rbac() -> RbacConfig:
    return cfg(roles=ROLES, tenant_claims=["org_id"], default_role="member")


# ---- config validation


def test_roles_claim_defaults_to_roles():
    assert rbac().roles_claim == "roles"


def test_unowned_keys_ignored():
    assert cfg(method="jwt", path=None, system_identities={}).roles == {}


def test_inheritance_multi_level_and_diamond():
    c = cfg(
        roles={
            "a": {"permissions": ["graph:read"], "reach": "own"},
            "b": {"permissions": ["store:read"], "inherits": ["a"], "reach": "own"},
            "c": {"permissions": ["files:read"], "inherits": ["a"], "reach": "own"},
            "d": {"permissions": ["config:read"], "inherits": ["b", "c"], "reach": "own"},
        }
    )
    assert c.role_permissions("d") == {"graph:read", "store:read", "files:read", "config:read"}
    assert c.role_permissions("a") == {"graph:read"}


def test_inherited_reach_not_changed():
    c = rbac()
    assert c.roles["org_admin"].reach is Reach.TENANT
    assert c.roles["member"].reach is Reach.OWN
    assert "graph:invoke" in c.role_permissions("org_admin")


def test_cycle_refused_and_named():
    roles = {
        "a": {"inherits": ["b"], "reach": "own"},
        "b": {"inherits": ["c"], "reach": "own"},
        "c": {"inherits": ["a"], "reach": "own"},
    }
    with pytest.raises(ValidationError, match=r"a -> b -> c -> a"):
        cfg(roles=roles)


def test_self_cycle_refused():
    with pytest.raises(ValidationError, match="x -> x"):
        cfg(roles={"x": {"inherits": ["x"], "reach": "own"}})


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"roles": {"r": {"permissions": ["nope:read"], "reach": "own"}}}, "nope:read"),
        ({"roles": {"r": {"permissions": ["graph:zap"], "reach": "own"}}}, "graph:zap"),
        ({"roles": {"r": {"permissions": []}}}, "reach"),
        ({"roles": {"r": {"permissions": [], "reach": "galaxy"}}}, "reach"),
        (
            {"roles": {"r": {"reach": "own"}}, "default_role": "ghost"},
            "ghost",
        ),
        ({"roles": {"r": {"inherits": ["ghost"], "reach": "own"}}}, "ghost"),
        (
            {
                "roles": {"r": {"reach": "own"}},
                "roles_claim": "roles",
                "permissions_claim": "perms",
            },
            "pick one",
        ),
        ({"roles": {"r": {"reach": "tenant"}}}, "tenant_claims"),
        ({"roles_claim": "roles"}, "no roles"),
    ],
)
def test_config_refused(kwargs, match):
    with pytest.raises(ValidationError, match=match):
        cfg(**kwargs)


def test_roles_with_reach_all():
    assert roles_with_reach_all(rbac()) == ["super"]
    assert roles_with_reach_all(None) == []
    assert roles_with_reach_all(cfg()) == []


def test_log_reach_all(caplog):
    with caplog.at_level("WARNING", logger="tenxgraph_api.auth"):
        log_reach_all_roles(rbac())
    assert "super" in caplog.text


# ---- resolve: no RBAC config


@pytest.mark.parametrize("config", [None, cfg(), cfg(tenant_claims=[])])
def test_default_mode(config):
    ident = resolve(dict(BASE), config)
    assert ident.permissions == DEFAULT_PERMISSIONS
    assert ident.reach is Reach.OWN
    assert ident.user_id == "u1"
    assert ident.claims == BASE


@pytest.mark.parametrize("claims", [{}, {"user_id": ""}, {"user_id": 5}, {"user_id": None}])
def test_bad_user_id(claims):
    with pytest.raises(InvalidClaimsError):
        resolve(claims, None)


# ---- tenant


def test_tenant_values():
    ident = resolve({**BASE, "org_id": 7, "roles": ["member"]}, rbac())
    assert ident.tenant == {"org_id": "7"}


@pytest.mark.parametrize("value", [None, "", 1.5, ["x"], True])
def test_bad_tenant_claim(value):
    claims = {**BASE, "roles": ["member"]}
    if value is not None:
        claims["org_id"] = value
    with pytest.raises(InvalidClaimsError, match="org_id"):
        resolve(claims, rbac())


# ---- roles mode


@pytest.mark.parametrize("roles", [["member"], "member"])
def test_roles_list_and_space_string(roles):
    ident = resolve({**BASE, "org_id": "a", "roles": roles}, rbac())
    assert ident.roles == {"member"}
    assert ident.permissions == rbac().role_permissions("member")


def test_space_string_multiple():
    ident = resolve({**BASE, "org_id": "a", "roles": "member org_admin"}, rbac())
    assert ident.roles == {"member", "org_admin"}
    assert ident.reach is Reach.TENANT


@pytest.mark.parametrize("roles", [5, {"a": 1}, [1, 2], ["member", None], True])
def test_malformed_roles_claim_not_default(roles):
    with pytest.raises(InvalidClaimsError):
        resolve({**BASE, "org_id": "a", "roles": roles}, rbac())


def test_missing_roles_claim_uses_default_role():
    ident = resolve({**BASE, "org_id": "a"}, rbac())
    assert ident.roles == {"member"}


def test_missing_roles_claim_no_default_means_nothing():
    c = cfg(roles=ROLES, tenant_claims=["org_id"])
    ident = resolve({**BASE, "org_id": "a"}, c)
    assert ident.permissions == frozenset()
    assert ident.reach is Reach.OWN


def test_unknown_token_role_ignored():
    ident = resolve({**BASE, "org_id": "a", "roles": ["ghost", "member"]}, rbac())
    assert ident.roles == {"member"}
    only_ghost = resolve({**BASE, "org_id": "a", "roles": ["ghost"]}, rbac())
    assert only_ghost.permissions == frozenset()
    assert only_ghost.reach is Reach.OWN


def test_custom_roles_claim_name():
    c = cfg(roles_claim="groups", roles=ROLES, tenant_claims=["org_id"])
    ident = resolve({**BASE, "org_id": "a", "groups": ["super"], "roles": ["member"]}, c)
    assert ident.roles == {"super"}


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        (["member"], Reach.OWN),
        (["member", "org_admin"], Reach.TENANT),
        (["org_admin", "super", "member"], Reach.ALL),
        (["super"], Reach.ALL),
    ],
)
def test_widest_reach(held, expected):
    assert resolve({**BASE, "org_id": "a", "roles": held}, rbac()).reach is expected


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"roles": []},
        {"roles": ["member"]},
        {"roles": ["org_admin"]},
        {"roles": ["ghost"]},
        {"roles": "member org_admin"},
        {"reach": "all", "roles": ["member"]},
        {"scope": "*"},
    ],
)
def test_reach_never_defaults_to_all(claims):
    ident = resolve({**BASE, "org_id": "a", **claims}, rbac())
    assert ident.reach is not Reach.ALL


def test_no_config_never_all():
    assert resolve({**BASE, "reach": "all"}, None).reach is Reach.OWN


# ---- permissions_claim mode


def test_permissions_claim_mode():
    c = cfg(permissions_claim="perms")
    ident = resolve({**BASE, "perms": ["graph:*", "files:read", "bogus", "x:y"]}, c)
    assert ident.permissions == {
        "graph:read",
        "graph:invoke",
        "graph:stream",
        "graph:stop",
        "graph:fix",
        "files:read",
    }
    assert ident.reach is Reach.OWN


def test_permissions_claim_space_string_and_missing():
    c = cfg(permissions_claim="perms")
    assert resolve({**BASE, "perms": "config:read store:read"}, c).permissions == {
        "config:read",
        "store:read",
    }
    assert resolve(dict(BASE), c).permissions == frozenset()


@pytest.mark.parametrize("value", [5, {"a": 1}, [1]])
def test_permissions_claim_malformed(value):
    with pytest.raises(InvalidClaimsError):
        resolve({**BASE, "perms": value}, cfg(permissions_claim="perms"))


# ---- narrowing


def test_scope_narrows():
    ident = resolve({**BASE, "scope": "graph:read threads:read files:read"}, None)
    assert ident.permissions == {"graph:read", "threads:read", "files:read"}


def test_scopes_list_and_wildcard():
    ident = resolve({**BASE, "scopes": ["store:*", "admin:read"]}, None)
    assert ident.permissions == {"store:read", "store:write", "store:delete"}


def test_openid_only_does_not_narrow():
    assert resolve({**BASE, "scope": "openid profile email"}, None).permissions == (
        DEFAULT_PERMISSIONS
    )
    assert resolve({**BASE, "scopes": []}, None).permissions == DEFAULT_PERMISSIONS


def test_scope_mixed_with_openid_narrows():
    ident = resolve({**BASE, "scope": "openid graph:read"}, None)
    assert ident.permissions == {"graph:read"}


@pytest.mark.parametrize(
    "claims", [{"scope": 5}, {"scope": {"a": 1}}, {"scopes": 5}, {"scopes": [1]}]
)
def test_malformed_scope(claims):
    with pytest.raises(InvalidClaimsError):
        resolve({**BASE, **claims}, None)


@pytest.mark.parametrize("scope", ["*", "admin:*", "admin:write graph:read", "graph:invoke", "x"])
@pytest.mark.parametrize("roles", [["member"], ["org_admin"], ["super"], []])
def test_narrowing_never_widens(scope, roles):
    base = resolve({**BASE, "org_id": "a", "roles": roles}, rbac())
    narrowed = resolve({**BASE, "org_id": "a", "roles": roles, "scope": scope}, rbac())
    assert narrowed.permissions <= base.permissions
    assert narrowed.permissions <= ALL_PERMISSIONS
    assert narrowed.reach is base.reach


def test_scope_cannot_add_to_empty():
    ident = resolve({**BASE, "org_id": "a", "roles": ["ghost"], "scope": "*"}, rbac())
    assert ident.permissions == frozenset()


# ---- properties

PERM_NAMES = sorted(ALL_PERMISSIONS)
JUNK = st.sampled_from(["openid", "profile", "email", "x", "graph", "a:b", "graph:nope"])
SCOPE_ENTRIES = st.lists(st.one_of(st.sampled_from([*PERM_NAMES, "*", "graph:*", "admin:*"]), JUNK))


@st.composite
def acyclic_roles(draw):
    """Role table where role i can only inherit roles with a lower index."""
    count = draw(st.integers(1, 6))
    roles = {}
    for i in range(count):
        parents = (
            draw(st.lists(st.sampled_from([f"r{j}" for j in range(i)]), max_size=3)) if i else []
        )
        roles[f"r{i}"] = {
            "permissions": draw(st.lists(st.sampled_from([*PERM_NAMES, "*", "tasks:*"]))),
            "inherits": parents,
            "reach": draw(st.sampled_from(["own", "tenant", "all"])),
        }
    return roles


def role_config(roles) -> RbacConfig:
    return cfg(roles=roles, tenant_claims=["org_id"], default_role="r0")


@given(
    roles=acyclic_roles(),
    held=st.lists(st.sampled_from([f"r{i}" for i in range(6)])),
    scope=SCOPE_ENTRIES,
)
def test_property_narrowing_never_widens(roles, held, scope):
    c = role_config(roles)
    claims = {**BASE, "org_id": "a", "roles": held}
    base = resolve(claims, c)
    for key, value in (("scope", " ".join(scope)), ("scopes", scope)):
        narrowed = resolve({**claims, key: value}, c)
        assert narrowed.permissions <= base.permissions
        assert narrowed.reach is base.reach


@given(granted=st.sets(st.sampled_from(PERM_NAMES)), scope=SCOPE_ENTRIES)
def test_property_narrowing_permissions_claim(granted, scope):
    c = cfg(permissions_claim="perms")
    claims = {**BASE, "perms": sorted(granted)}
    assert resolve({**claims, "scopes": scope}, c).permissions <= resolve(claims, c).permissions
    assert resolve(claims, c).permissions == granted


@given(roles=acyclic_roles())
def test_property_inheritance_superset(roles):
    c = role_config(roles)
    for name, role in roles.items():
        for parent in role["inherits"]:
            assert c.role_permissions(name) >= c.role_permissions(parent)


@given(roles=acyclic_roles(), length=st.integers(1, 5))
def test_property_cycle_refused(roles, length):
    cycle = {f"c{i}": {"inherits": [f"c{(i + 1) % length}"], "reach": "own"} for i in range(length)}
    with pytest.raises(ValidationError, match="cycle"):
        role_config({**roles, **cycle})


@given(roles=acyclic_roles(), held=st.lists(st.sampled_from([f"r{i}" for i in range(6)])))
def test_property_reach_all_only_if_held_role_declares_it(roles, held):
    ident = resolve({**BASE, "org_id": "a", "roles": held}, role_config(roles))
    if ident.reach is Reach.ALL:
        assert any(roles[r]["reach"] == "all" for r in ident.roles)


MALFORMED = st.one_of(
    st.integers(),
    st.floats(),
    st.booleans(),
    st.dictionaries(st.text(), st.text()),
    st.lists(st.one_of(st.integers(), st.none(), st.booleans(), st.floats()), min_size=1),
    st.tuples(st.text(), st.integers()).map(lambda t: [t[0], t[1]]),
)


@given(MALFORMED)
def test_property_malformed_roles_claim_raises(value):
    with pytest.raises(InvalidClaimsError):
        resolve({**BASE, "org_id": "a", "roles": value}, rbac())


@pytest.mark.parametrize("scope", ["checkpointer:read", "nope:read graph:bogus", "read:messages"])
def test_unknown_permission_shaped_scope_narrows_to_nothing(scope):
    identity = resolve({"user_id": "u1", "scope": scope}, None)
    assert identity.permissions == frozenset()


def test_unknown_scope_entry_does_not_add_to_known_one():
    identity = resolve({"user_id": "u1", "scope": "openid threads:read checkpointer:write"}, None)
    assert identity.permissions == frozenset({"threads:read"})
