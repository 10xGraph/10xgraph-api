"""GraphConfig.auth_config parses the ``auth`` setting; check_auth_config reports rule errors."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tenxgraph_api.src.app.core.auth.auth_config import AuthConfig, check_auth_config
from tenxgraph_api.src.app.core.auth.rbac import resolve
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(auth) -> GraphConfig:
        path = tmp_path / "10xgraph.json"
        path.write_text(json.dumps({"agent": "path/to/agent.py", "auth": auth}))
        return GraphConfig(path=str(path))

    return _make


def test_no_auth_key_is_none(tmp_path: Path):
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "path/to/agent.py"}))
    assert GraphConfig(path=str(path)).auth_config() is None


def test_null_auth_is_none(make_config):
    assert make_config(None).auth_config() is None


def test_jwt_string_is_the_jwt_method(make_config):
    result = make_config("jwt").auth_config()
    assert isinstance(result, AuthConfig)
    assert result.method == "jwt"


def test_custom_object(make_config):
    result = make_config({"method": "custom", "path": "auth.py:MyAuth"}).auth_config()
    assert result.method == "custom"
    assert result.path == "auth.py:MyAuth"


def test_method_none_object_is_none(make_config):
    assert make_config({"method": "none"}).auth_config() is None


@pytest.mark.parametrize("value", ["", "my-jwt-auth", "JWT", "oauth2", 123, ["jwt", "oauth2"]])
def test_other_values_are_refused(make_config, value):
    with pytest.raises(ValueError, match="auth must be null"):
        make_config(value).auth_config()


def test_unknown_method_in_object_is_refused(make_config):
    with pytest.raises(ValidationError):
        make_config({"method": "oauth2"}).auth_config()


# ---- tool permissions in roles (core ToolGuard)

TOOL_AUTH = {
    "method": "jwt",
    "roles": {
        "support": {"permissions": ["graph:*", "tools:refund"], "reach": "own"},
        "agent": {"permissions": ["tools:*"], "reach": "own"},
        "admin": {"permissions": ["*"], "reach": "own"},
    },
}


def test_tool_permissions_boot_and_reach_identity(make_config):
    issues = check_auth_config({"agent": "a.py:app", "auth": TOOL_AUTH}, production=True)
    assert not [i for i in issues if i.level == "error"]
    rbac = make_config(TOOL_AUTH).auth_config().rbac_config()

    support = resolve({"user_id": "u1", "roles": ["support"]}, rbac)
    assert "tools:refund" in support.permissions
    assert support.can("tools:refund") and not support.can("tools:delete")
    assert resolve({"user_id": "u1", "roles": ["agent"]}, rbac).can("tools:delete")
    assert resolve({"user_id": "u1", "roles": ["admin"]}, rbac).can("tools:delete")


@pytest.mark.parametrize("bad", ["tools:", "tools: refund", "tools:a:b"])
def test_malformed_tool_permission_is_a_boot_error(bad):
    auth = {"method": "jwt", "roles": {"r": {"permissions": [bad], "reach": "own"}}}
    issues = check_auth_config({"agent": "a.py:app", "auth": auth}, production=False)
    assert any(i.level == "error" for i in issues)
