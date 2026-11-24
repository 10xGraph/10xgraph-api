"""Tests for store router input validation."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from tenxgraph_api.src.app.routers.store.router import (
    delete_memory,
    get_memory,
    search_memories,
    update_memory,
)
from tenxgraph_api.src.app.routers.store.schemas.store_schemas import (
    SearchMemorySchema,
    UpdateMemorySchema,
)

USER = {"id": "user-123", "name": "Test User"}


def _search(value):
    return search_memories(
        request=MagicMock(),
        payload=SearchMemorySchema(query=value, metadata_filters=None),
        service=AsyncMock(),
        identity=USER,
    )


def _by_id(func, extra=None):
    def call(value):
        return func(
            request=MagicMock(),
            memory_id=value,
            payload=extra,
            service=AsyncMock(),
            identity=USER,
        )

    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize(
    "call",
    [
        _search,
        _by_id(get_memory),
        _by_id(update_memory, UpdateMemorySchema(content="new content")),
        _by_id(delete_memory),
    ],
    ids=["search", "get", "update", "delete"],
)
async def test_blank_query_or_memory_id_is_422(call, value):
    with pytest.raises(HTTPException) as exc_info:
        await call(value)
    assert exc_info.value.status_code == 422
