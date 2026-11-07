"""ThreadOwnershipResolver caching thread scopes (user_id plus tenant values)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tenxgraph_api.src.app.core.auth.ownership_resolver import (
    ThreadOwnershipResolver,
    canonical_value,
    to_scope,
)


class _ScopeLookup:
    def __init__(self, scopes: dict[str, dict]):
        self.scopes = scopes
        self.calls = 0

    async def __call__(self, thread_id: str):
        self.calls += 1
        return self.scopes.get(str(thread_id))


ORG = uuid.UUID("6f1c0d1e-8a43-4c5e-9b0e-2f1d3c4b5a69")


def test_canonical_value_normalises_types():
    assert canonical_value(None) is None
    assert canonical_value(True) == "true"
    assert canonical_value(7) == "7"
    assert canonical_value(ORG) == str(ORG)
    later = datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert canonical_value(later) == canonical_value(datetime(2026, 1, 1, tzinfo=UTC))


def test_to_scope_reads_owner_and_dict_results():
    assert to_scope(None) is None
    assert to_scope("alice") == {"user_id": "alice"}
    assert to_scope(42) == {"user_id": "42"}
    assert to_scope({"user_id": "alice", "org_id": ORG, "team": None}) == {
        "user_id": "alice",
        "org_id": str(ORG),
        "team": None,
    }
    # A scope with no owner is not a thread.
    assert to_scope({"org_id": "x"}) is None


@pytest.mark.asyncio
async def test_scope_is_cached_in_l1_with_tenant_values():
    lookup = _ScopeLookup({"t1": {"user_id": "alice", "org_id": ORG}})
    r = ThreadOwnershipResolver(lookup, tenant_columns=("org_id",))

    for _ in range(3):
        assert await r.scope_of("t1", ("user_id", "org_id")) == {
            "user_id": "alice",
            "org_id": str(ORG),
        }
    assert lookup.calls == 1
    assert await r.owner_of("t1") == "alice"


@pytest.mark.asyncio
async def test_returned_scope_is_a_copy():
    r = ThreadOwnershipResolver(_ScopeLookup({"t1": {"user_id": "alice", "org_id": "a"}}))
    scope = await r.scope_of("t1")
    scope["org_id"] = "b"
    assert (await r.scope_of("t1"))["org_id"] == "a"


@pytest.mark.asyncio
async def test_a_cached_scope_missing_a_compared_column_is_looked_up_again():
    lookup = _ScopeLookup({"t1": {"user_id": "alice"}})
    r = ThreadOwnershipResolver(lookup, tenant_columns=("org_id",))
    await r.scope_of("t1", ("user_id",))
    # The column was declared later: the cached entry cannot answer it.
    lookup.scopes["t1"] = {"user_id": "alice", "org_id": "org-a"}

    assert (await r.scope_of("t1", ("user_id", "org_id")))["org_id"] == "org-a"
    assert lookup.calls == 2
    await r.scope_of("t1", ("user_id", "org_id"))
    assert lookup.calls == 2


@pytest.mark.asyncio
async def test_a_null_tenant_value_is_cached_as_known():
    lookup = _ScopeLookup({"t1": {"user_id": "alice", "org_id": None}})
    r = ThreadOwnershipResolver(lookup, tenant_columns=("org_id",))
    for _ in range(2):
        assert (await r.scope_of("t1", ("org_id",)))["org_id"] is None
    assert lookup.calls == 1
