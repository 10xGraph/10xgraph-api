"""Memory provenance in the store API: the server sets origin, clients cannot."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tenxgraph.core.authz import Identity
from tenxgraph.qa.testing.in_memory_store import InMemoryStore
from tenxgraph.storage.store.store_schema import MemoryOrigin

from tenxgraph_api.src.app.routers.store.schemas.store_schemas import (
    ListMemoriesSchema,
    SearchMemorySchema,
    StoreMemorySchema,
    UpdateMemorySchema,
)
from tenxgraph_api.src.app.routers.store.services.store_service import (
    StoreService,
    _client_metadata,
    _client_options,
)


@pytest.fixture
def user():
    return Identity(user_id="u1")


@pytest.fixture
def real_service():
    store = InMemoryStore()
    return StoreService(store=store), store


@pytest.mark.asyncio
async def test_origin_is_user_and_client_values_are_ignored(mock_store, user):
    service = StoreService(store=mock_store)
    mock_store.astore.return_value = "m1"
    payload = StoreMemorySchema(
        content="hello",
        metadata={"origin": "system", "expires_at": "2000-01-01T00:00:00+00:00", "k": 1},
        options={"origin": "system", "expires_at": "x", "infer": False},
    )
    await service.store_memory(payload, user)
    kwargs = mock_store.astore.call_args.kwargs
    assert kwargs["origin"] is MemoryOrigin.USER
    assert kwargs["metadata"] == {"k": 1}
    assert "expires_at" not in kwargs
    assert kwargs["infer"] is False


@pytest.mark.asyncio
async def test_update_forces_user_origin(mock_store, user):
    service = StoreService(store=mock_store)
    mock_store.aupdate.return_value = {"ok": True}
    payload = UpdateMemorySchema(
        content="new", metadata={"origin": "system"}, options={"origin": "system"}
    )
    await service.update_memory("m1", payload, user)
    kwargs = mock_store.aupdate.call_args.kwargs
    assert kwargs["origin"] is MemoryOrigin.USER
    assert kwargs["metadata"] == {}


def test_origin_is_not_a_client_option_or_metadata_key():
    assert "origin" not in _client_options({"origin": "system"})
    assert _client_metadata({"origin": "system", "expires_at": "x", "a": 1}) == {"a": 1}


def test_request_schemas_have_no_origin_field():
    assert "origin" not in StoreMemorySchema.model_fields
    assert "origin" not in UpdateMemorySchema.model_fields


def test_expires_at_needs_a_timezone():
    with pytest.raises(ValidationError):
        StoreMemorySchema(content="x", expires_at="2030-01-01T00:00:00")
    ok = StoreMemorySchema(content="x", expires_at="2030-01-01T00:00:00+00:00")
    assert ok.expires_at is not None


def test_origins_filter_values():
    schema = SearchMemorySchema(query="q", origins=["user", None])
    assert schema.origins == [MemoryOrigin.USER, None]
    with pytest.raises(ValidationError):
        ListMemoriesSchema(origins=["root"])


@pytest.mark.asyncio
async def test_response_carries_origin_and_expires_at(real_service, user):
    service, _ = real_service
    later = datetime.now(UTC) + timedelta(days=1)
    created = await service.store_memory(
        StoreMemorySchema(content="likes tea", expires_at=later), user
    )
    got = await service.get_memory(created.memory_id, {}, user)
    assert got.memory.origin == MemoryOrigin.USER
    assert got.memory.expires_at == later
    dumped = got.model_dump(mode="json")["memory"]
    assert dumped["origin"] == "user"
    assert dumped["expires_at"] is not None


@pytest.mark.asyncio
async def test_unknown_origin_and_no_expiry_are_null(real_service, user):
    service, store = real_service
    # A record written outside the API: no origin, no expiry.
    memory_id = await store.astore({"user": user.model_dump()}, "old fact")
    got = await service.get_memory(memory_id, {}, user)
    dumped = got.model_dump(mode="json")["memory"]
    assert dumped["origin"] is None
    assert dumped["expires_at"] is None


@pytest.mark.asyncio
async def test_list_origins_filter(real_service, user):
    service, store = real_service
    await service.store_memory(StoreMemorySchema(content="from user"), user)
    await store.astore({"user": user.model_dump()}, "from agent", origin=MemoryOrigin.AGENT)
    page = await service.list_memories({}, user, origins=[MemoryOrigin.USER])
    assert [m.content for m in page.memories] == ["from user"]
