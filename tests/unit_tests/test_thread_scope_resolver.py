"""ThreadOwnershipResolver caching thread scopes (user_id plus tenant values)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from tenxgraph.storage.columns import parse_columns

from tenxgraph_api.src.app.core.auth.authorization import (
    OwnershipAuthorizationBackend,
    scope_lookup,
    supports_owner_lookup,
)
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


class _FakeCache:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ttl=None, mode="always"):
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


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
async def test_tenant_scopes_use_their_own_json_l2_key():
    cache = _FakeCache()
    lookup = _ScopeLookup({"t:1": {"user_id": "alice", "org_id": "org-a"}})
    r = ThreadOwnershipResolver(lookup, cache=cache, tenant_columns=("org_id",))
    await r.scope_of("t:1", ("user_id", "org_id"))

    assert list(cache.store) == ["owner:scope:t%3A1"]
    assert json.loads(cache.store["owner:scope:t%3A1"]) == {"org_id": "org-a", "user_id": "alice"}

    other_lookup = _ScopeLookup({})
    other = ThreadOwnershipResolver(other_lookup, cache=cache, tenant_columns=("org_id",))
    assert await other.scope_of("t:1", ("user_id", "org_id")) == {
        "user_id": "alice",
        "org_id": "org-a",
    }
    assert other_lookup.calls == 0


@pytest.mark.asyncio
async def test_without_tenant_columns_l2_keeps_the_plain_owner_layout():
    cache = _FakeCache()
    r = ThreadOwnershipResolver(_ScopeLookup({"t1": {"user_id": "alice"}}), cache=cache)
    await r.scope_of("t1", ("user_id",))
    assert cache.store == {"owner:id:t1": b"alice"}


@pytest.mark.asyncio
async def test_a_plain_owner_entry_is_never_read_as_a_tenant_scope():
    """An entry an older release wrote (owner only) must not stand in for a full scope."""
    cache = _FakeCache()
    cache.store["owner:id:t1"] = b"alice"
    lookup = _ScopeLookup({"t1": {"user_id": "alice", "org_id": "org-a"}})
    r = ThreadOwnershipResolver(lookup, cache=cache, tenant_columns=("org_id",))

    assert (await r.scope_of("t1", ("user_id", "org_id")))["org_id"] == "org-a"
    assert lookup.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1]", b'{"org_id": "a"}', b'{"user_id": "a", "org_id": 5}', b"\xff"],
)
async def test_malformed_l2_scope_entries_are_misses(raw):
    cache = _FakeCache()
    cache.store["owner:scope:t1"] = raw
    lookup = _ScopeLookup({"t1": {"user_id": "alice", "org_id": "org-a"}})
    r = ThreadOwnershipResolver(lookup, cache=cache, tenant_columns=("org_id",))

    assert await r.scope_of("t1", ("user_id", "org_id")) == {
        "user_id": "alice",
        "org_id": "org-a",
    }
    assert lookup.calls == 1


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


@pytest.mark.asyncio
async def test_evict_clears_both_l2_layouts():
    cache = _FakeCache()
    cache.store["owner:id:t1"] = b"alice"
    r = ThreadOwnershipResolver(
        _ScopeLookup({"t1": {"user_id": "alice", "org_id": "a"}}),
        cache=cache,
        tenant_columns=("org_id",),
    )
    await r.scope_of("t1", ("user_id", "org_id"))
    await r.evict("t1", "alice")
    assert list(cache.store) == ["owner:tomb:t1"]


@pytest.mark.asyncio
async def test_a_delete_by_someone_other_than_the_owner_blocks_everyone():
    """An org admin deleting a member's thread gets no head start on recreating the id."""
    r = ThreadOwnershipResolver(_ScopeLookup({"t1": {"user_id": "alice", "org_id": "a"}}))
    await r.scope_of("t1")
    await r.evict("t1", "carol")
    assert await r.recently_deleted("t1", "carol") is True
    assert await r.recently_deleted("t1", "alice") is True


# ---------------------------------------------------------------------------
# Backend: which lookup it uses, and matching
# ---------------------------------------------------------------------------


class _OwnerOnly:
    async def aget_thread_owner(self, thread_id):
        return "alice"


class _WithScope(_OwnerOnly):
    tenant_column_names = ("org_id",)

    def __init__(self):
        self.calls = 0

    async def aget_thread_scope(self, thread_id):
        self.calls += 1
        return {"user_id": "alice", "org_id": ORG}


def test_scope_lookup_prefers_aget_thread_scope():
    with_scope = _WithScope()
    assert scope_lookup(with_scope) == with_scope.aget_thread_scope
    owner_only = _OwnerOnly()
    assert scope_lookup(owner_only) == owner_only.aget_thread_owner


def test_supports_owner_lookup_accepts_either_method():
    class _ScopeOnly:
        async def aget_thread_scope(self, thread_id):
            return None

    assert supports_owner_lookup(_ScopeOnly())
    assert supports_owner_lookup(_OwnerOnly())
    assert not supports_owner_lookup(object())


@pytest.mark.asyncio
async def test_uuid_tenant_values_match_whatever_case_the_identity_uses():
    cp = _WithScope()
    cp.columns = parse_columns({"org_id": "uuid"}, None)
    backend = OwnershipAuthorizationBackend(checkpointer=cp)
    upper = {"user_id": "alice", "org_id": str(ORG).upper()}

    assert await backend.authorize(upper, "checkpointer", "read", resource_id="t1")
    other = {"user_id": "alice", "org_id": str(uuid.uuid4())}
    assert not await backend.authorize(other, "checkpointer", "read", resource_id="t1")
    bad = {"user_id": "alice", "org_id": "not-a-uuid"}
    assert not await backend.authorize(bad, "checkpointer", "read", resource_id="t1")
    assert cp.calls == 1


@pytest.mark.asyncio
async def test_none_scope_reaches_any_existing_thread():
    class _Shared(OwnershipAuthorizationBackend):
        def isolation_scope(self, user=None):
            return "none"

    backend = _Shared(checkpointer=_WithScope())
    bob = {"user_id": "bob", "org_id": "elsewhere"}
    assert await backend.authorize(bob, "checkpointer", "read", resource_id="t1")


@pytest.mark.asyncio
async def test_an_unknown_scope_string_denies():
    class _Typo(OwnershipAuthorizationBackend):
        def isolation_scope(self, user=None):
            return "owners"

    backend = _Typo(checkpointer=_WithScope())
    alice = {"user_id": "alice", "org_id": str(ORG)}
    assert not await backend.authorize(alice, "checkpointer", "read", resource_id="t1")


@pytest.mark.asyncio
async def test_a_custom_resolver_with_only_owner_of_still_works():
    class _Resolver:
        async def owner_of(self, thread_id):
            return "alice"

    backend = OwnershipAuthorizationBackend(checkpointer=_OwnerOnly(), resolver=_Resolver())
    assert await backend.authorize({"user_id": "alice"}, "graph", "invoke", resource_id="t1")
    assert not await backend.authorize({"user_id": "bob"}, "graph", "invoke", resource_id="t1")
