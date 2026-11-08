"""Shared fixtures for store unit tests."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tenxgraph.core.authz import Identity
from tenxgraph.core.state import Message
from tenxgraph.storage.store import BaseStore, MemorySearchResult, MemoryType

from tenxgraph_api.src.app.routers.store.services.store_service import StoreService


@pytest.fixture
def mock_store():
    """Mock BaseStore for testing."""
    mock = AsyncMock(spec=BaseStore)
    return mock


@pytest.fixture
def store_service(mock_store):
    """StoreService instance with mocked store."""
    return StoreService(store=mock_store)


@pytest.fixture
def mock_user():
    """Caller identity."""
    return Identity(user_id="test-user-123", claims={"email": "test@example.com"})


@pytest.fixture
def sample_memory_id():
    """Sample memory ID."""
    return str(uuid4())


@pytest.fixture
def sample_message():
    """Sample Message object."""
    return Message.text_message(
        role="user",
        content="This is a test memory",
    )


@pytest.fixture
def sample_memory_result(sample_memory_id):
    """Sample MemorySearchResult."""
    return MemorySearchResult(
        id=sample_memory_id,
        content="This is a test memory",
        memory_type=MemoryType.EPISODIC,
        metadata={"key": "value"},
        score=0.95,
    )


@pytest.fixture
def sample_memory_results(sample_memory_id):
    """Sample list of MemorySearchResult."""
    return [
        MemorySearchResult(
            id=sample_memory_id,
            content="First memory",
            memory_type=MemoryType.EPISODIC,
            metadata={"index": 1},
            score=0.95,
        ),
        MemorySearchResult(
            id=str(uuid4()),
            content="Second memory",
            memory_type=MemoryType.SEMANTIC,
            metadata={"index": 2},
            score=0.85,
        ),
    ]
