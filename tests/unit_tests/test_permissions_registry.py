import pytest
from hypothesis import given
from hypothesis import strategies as st

from tenxgraph_api.src.app.core.auth.permissions_registry import (
    ALL_PERMISSIONS,
    DEFAULT_PERMISSIONS,
    REGISTRY,
    expand,
)


def test_all_permissions_count():
    assert len(ALL_PERMISSIONS) == 5 + 3 + 3 + 3 + 1 + 4 + 2
    assert "graph:invoke" in ALL_PERMISSIONS


def test_default_excludes_admin_and_pause():
    assert not {p for p in DEFAULT_PERMISSIONS if p.startswith("admin:")}
    assert "tasks:pause" not in DEFAULT_PERMISSIONS
    assert ALL_PERMISSIONS - {"admin:read", "admin:write", "tasks:pause"} == DEFAULT_PERMISSIONS


def test_expand_star_resource_and_concrete():
    assert expand(["*"]) == ALL_PERMISSIONS
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
    assert expand(a) <= ALL_PERMISSIONS


@given(PATTERNS)
def test_star_is_all(a):
    assert expand(["*", *a]) == ALL_PERMISSIONS
