import pytest
from hypothesis import given
from hypothesis import strategies as st

from tenxgraph_api.src.app.core.auth.permissions_registry import (
    ALL_PERMISSIONS,
    DEFAULT_PERMISSIONS,
    REGISTRY,
    expand,
    intersect,
)


def test_all_permissions_count():
    assert len(ALL_PERMISSIONS) == 5 + 3 + 3 + 3 + 1 + 3 + 2
    assert "graph:invoke" in ALL_PERMISSIONS


def test_default_excludes_admin_only():
    assert ALL_PERMISSIONS - {"admin:read", "admin:write"} == DEFAULT_PERMISSIONS




def test_expand_star_resource_and_concrete():
    assert expand(["*"]) == ALL_PERMISSIONS | {"tools:*"}
    assert expand(["config:*"]) == {"config:read"}
    assert expand(["threads:*", "graph:read"]) == {
        "threads:read",
        "threads:write",
        "threads:delete",
        "graph:read",
    }
    assert expand([]) == frozenset()


@pytest.mark.parametrize(
    "bad", ["nope:read", "graph:nope", "graph", ":read", "graph:", "", "graph:read:x", 5]
)
def test_expand_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        expand([bad])


def test_expand_error_names_entry():
    with pytest.raises(ValueError, match="graph:nope"):
        expand(["graph:read", "graph:nope"])


# ---- properties

PATTERNS = st.lists(
    st.sampled_from(
        ["*", *sorted(ALL_PERMISSIONS), *(f"{r}:*" for r in REGISTRY)],
    )
)


@given(PATTERNS, PATTERNS)
def test_expand_monotone(a, b):
    assert expand(a) <= expand(a + b)


@given(PATTERNS)
def test_expand_idempotent(a):
    assert expand(sorted(expand(a))) == expand(a)
    assert expand(a) <= ALL_PERMISSIONS | {"tools:*"}


@given(PATTERNS)
def test_star_is_all(a):
    assert expand(["*", *a]) == ALL_PERMISSIONS | {"tools:*"}


# ---- tools: open actions (tool names), for core's ToolGuard


def test_tools_not_in_route_permissions():
    assert not any(p.startswith("tools:") for p in ALL_PERMISSIONS)
    assert "tools" not in REGISTRY
    assert not any(p.startswith("tools:") for p in DEFAULT_PERMISSIONS)


@pytest.mark.parametrize(
    "name", ["refund", "browser_click", "mcp.search", "get-weather", "Tool_2", "a" * 128, "*"]
)
def test_expand_tool_names_kept(name):
    assert expand([f"tools:{name}"]) == {f"tools:{name}"}


def test_tools_star_kept_literally():
    assert expand(["tools:*", "tools:refund"]) == {"tools:*", "tools:refund"}


@pytest.mark.parametrize(
    "bad",
    [
        "tools",
        "tools:",
        "tools: refund",
        "tools:re fund",
        "tools:a\tb",
        "tools:a:b",
        "tools:a/b",
        "tools:*x",
        "tools:" + "a" * 129,
        "tools:caf\u00e9",
    ],
)
def test_expand_rejects_bad_tool_names(bad):
    with pytest.raises(ValueError):
        expand([bad])


def test_intersect_tools():
    assert intersect(frozenset({"tools:*"}), frozenset({"tools:refund"})) == {"tools:refund"}
    assert intersect(frozenset({"tools:refund"}), frozenset({"tools:*"})) == {"tools:refund"}
    assert intersect(frozenset({"tools:*"}), frozenset({"tools:*"})) == {"tools:*"}
    assert intersect(frozenset({"tools:a"}), frozenset({"tools:b"})) == frozenset()
    assert intersect(frozenset({"tools:*", "graph:read"}), frozenset({"graph:*"})) == frozenset()


@given(PATTERNS, PATTERNS)
def test_intersect_matches_set_intersection_for_routes(a, b):
    ea, eb = expand(a) - {"tools:*"}, expand(b) - {"tools:*"}
    assert intersect(ea, eb) == ea & eb
