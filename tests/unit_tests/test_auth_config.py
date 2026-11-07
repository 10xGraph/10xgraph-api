"""One test per row of the "Config rules" table in AUTH_PLAN.md, against ``check_auth_config``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tenxgraph_api import BaseAuth
from tenxgraph_api.src.app.core.auth.auth_config import (
    AuthConfig,
    check_auth_config,
    check_auth_instance,
    check_tenant_claims,
)


MEMBER = {"permissions": ["graph:*", "threads:*"], "reach": "own"}


def _rules(data: dict, level: str = "error", *, production: bool = False) -> set[str]:
    return {i.rule for i in check_auth_config(data, production=production) if i.level == level}


def _jwt(**keys) -> dict:
    return {"auth": {"method": "jwt", **keys}}


@pytest.mark.parametrize("auth", [None, "jwt", {"method": "none"}, {"method": "jwt"}])
def test_valid_shortcuts_pass(auth):
    assert _rules({"auth": auth}) == set()


@pytest.mark.parametrize(
    "auth",
    [
        {"roles": {"m": MEMBER}},
        {"method": "none", "permissions_claim": "perms"},
        {"method": "none", "roles_claim": "roles"},
        {"method": "none", "tenant_claims": ["org_id"]},
        {"method": "none", "default_role": "m"},
        {"method": "none", "system_identities": {"n": {"roles": []}}},
    ],
)
def test_rbac_keys_with_auth_off(auth):
    assert _rules({"auth": auth}) == {"rbac-without-auth"}


@pytest.mark.parametrize("value", ["ownership", None, {"backend": "rbac"}])
def test_authorization_key_refused_with_any_value(value):
    issues = check_auth_config({"auth": "jwt", "authorization": value}, production=False)
    (issue,) = issues
    assert issue.rule == "authorization-removed"
    assert "custom" in issue.message
    assert "auth.roles" in issue.message


def test_custom_without_path():
    assert _rules({"auth": {"method": "custom"}}) == {"custom-path"}


def test_custom_path_not_module_attr():
    assert _rules({"auth": {"method": "custom", "path": "nocolon"}}) == {"custom-path"}


def test_custom_class_not_a_baseauth_subclass_names_the_path(tmp_path, monkeypatch):
    from tenxgraph_api.src.app.loader import load_auth

    (tmp_path / "notauth.py").write_text("class Thing: ...\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(Exception, match=r"custom-class.*notauth:Thing"):
        load_auth("notauth:Thing")


@pytest.mark.parametrize("method", ["jwt", "none"])
def test_path_with_non_custom_method(method):
    assert _rules({"auth": {"method": method, "path": "a:B"}}) == {"path-not-custom"}


def test_both_permission_sources():
    data = _jwt(roles={"m": MEMBER}, permissions_claim="perms")
    issues = check_auth_config(data, production=False)
    assert [i.rule for i in issues] == ["rbac"]
    assert "pick one permission source" in issues[0].message


def test_roles_without_roles_claim_defaults_to_roles():
    assert _rules(_jwt(roles={"m": MEMBER})) == set()
    assert AuthConfig.parse(_jwt(roles={"m": MEMBER})["auth"]).rbac_config().roles_claim == "roles"


def test_default_role_not_in_roles():
    (issue,) = check_auth_config(_jwt(roles={"m": MEMBER}, default_role="x"), production=False)
    assert issue.rule == "rbac"
    assert "'x'" in issue.message


def test_inherits_not_in_roles():
    role = {**MEMBER, "inherits": ["ghost"]}
    (issue,) = check_auth_config(_jwt(roles={"m": role}), production=False)
    assert "ghost" in issue.message


def test_system_identity_role_not_in_roles():
    data = _jwt(roles={"m": MEMBER}, system_identities={"nightly": {"roles": ["ghost"]}})
    (issue,) = check_auth_config(data, production=False)
    assert issue.rule == "system-identity-role"
    assert "ghost" in issue.message


def test_inheritance_cycle():
    roles = {
        "a": {"inherits": ["b"], "reach": "own"},
        "b": {"inherits": ["a"], "reach": "own"},
    }
    (issue,) = check_auth_config(_jwt(roles=roles), production=False)
    assert "cycle" in issue.message
    assert "a -> b -> a" in issue.message


def test_unknown_permission():
    roles = {"m": {"permissions": ["graph:explode"], "reach": "own"}}
    (issue,) = check_auth_config(_jwt(roles=roles), production=False)
    assert issue.rule == "rbac"
    assert "graph:explode" in issue.message


def test_unknown_reach():
    roles = {"m": {"permissions": ["graph:read"], "reach": "galaxy"}}
    (issue,) = check_auth_config(_jwt(roles=roles), production=False)
    assert issue.field == "auth.roles.m.reach"


def test_missing_reach():
    assert _rules(_jwt(roles={"m": {"permissions": ["graph:read"]}})) == {"auth-shape"}


def test_tenant_reach_without_tenant_claims():
    roles = {"m": {"permissions": ["graph:read"], "reach": "tenant"}}
    assert _rules(_jwt(roles=roles)) == {"rbac"}
    assert _rules(_jwt(roles=roles, tenant_claims=["org_id"])) == set()


class _Backend(SimpleNamespace):
    pass


@pytest.mark.parametrize(("production", "level"), [(True, "error"), (False, "warning")])
def test_tenant_claims_not_declared_as_columns(production, level):
    issues = check_tenant_claims(
        ["org_id"], _Backend(tenant_column_names=()), None, production=production
    )
    assert [(i.rule, i.level) for i in issues] == [("tenant-columns", level)]


@pytest.mark.parametrize(("production", "level"), [(True, "error"), (False, "warning")])
def test_tenant_columns_not_in_tenant_claims(production, level):
    issues = check_tenant_claims(
        [], _Backend(tenant_column_names=("org_id",)), None, production=production
    )
    assert [(i.rule, i.level) for i in issues] == [("tenant-columns", level)]
    assert "['org_id']" in issues[0].message


def test_tenant_claims_declared_pass_and_store_is_compared():
    ok = _Backend(tenant_column_names=("org_id",))
    assert check_tenant_claims(["org_id"], ok, ok, production=True) == []
    bare_store = _Backend(tenant_column_names=())
    (issue,) = check_tenant_claims(["org_id"], ok, bare_store, production=True)
    assert "store" in issue.message
    # store_allow_tenant_mismatch leaves the store out
    assert (
        check_tenant_claims(["org_id"], ok, bare_store, production=True, allow_mismatch=True) == []
    )


def test_custom_class_overriding_authorize_with_roles_warns():
    class Plain(BaseAuth):
        def authenticate(self, request, response, credential):
            return {}

    class Override(Plain):
        async def authorize(self, identity, permission, resource=None):
            return True

    with_roles = AuthConfig(method="custom", path="a:B", roles={"m": MEMBER}, roles_claim="roles")
    without_roles = AuthConfig(method="custom", path="a:B")
    assert check_auth_instance(Plain(), with_roles) == []
    assert check_auth_instance(Override(), without_roles) == []
    (issue,) = check_auth_instance(Override(), with_roles)
    assert (issue.rule, issue.level) == ("authorize-override", "warning")


def test_reach_all_is_listed_as_a_warning():
    roles = {"root": {"permissions": ["*"], "reach": "all"}, "m": MEMBER}
    issues = check_auth_config(_jwt(roles=roles), production=False)
    (issue,) = issues
    assert (issue.rule, issue.level) == ("reach-all", "warning")
    assert "root" in issue.message
    assert "'m'" not in issue.message


def test_production_with_auth_off_warns_but_is_allowed():
    assert _rules({}, "warning", production=True) == {"auth-off-production"}
    assert _rules({}, "error", production=True) == set()
    assert _rules({}, "warning", production=False) == set()
    assert _rules({"auth": "jwt"}, "warning", production=True) == set()


def test_runs_server_with_auth_off_is_not_a_warning():
    # runs.thread_ids "server" is enforced with auth on or off (an unknown id is a 404).
    data = {"runs": {"thread_ids": "server"}}
    assert _rules(data, "warning") == set()
    assert _rules({**data, "auth": "jwt"}, "warning") == set()


def test_unknown_auth_key_or_value_is_a_shape_error():
    assert _rules({"auth": {"method": "jwt", "typo": 1}}) == {"auth-shape"}
    assert _rules({"auth": "oauth"}) == {"auth-shape"}
    assert _rules({"auth": {"method": "oauth"}}) == {"auth-shape"}


def test_graph_introspection_key_is_refused():
    assert _rules({"graph_introspection": "scoped"}, "error") == {"graph-introspection-removed"}
