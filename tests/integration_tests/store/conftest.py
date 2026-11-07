"""Shared fixtures for store integration tests."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.storage.store import BaseStore, MemorySearchResult, MemoryType

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.setup_middleware import setup_middleware


class _NoAuthConfig:
    def auth_config(self):
        return None


@pytest.fixture
def mock_store():
    """Mock BaseStore for testing."""
    return AsyncMock(spec=BaseStore)


@pytest.fixture
def app(mock_store):
    """FastAPI test app with the store router and auth disabled."""
    app = FastAPI()
    setup_middleware(app)

    container = InjectQ()
    container.bind_instance(BaseStore, mock_store)
    container.bind_instance(GraphConfig, _NoAuthConfig())
    container.bind_instance(BaseAuth, None, allow_none=True)  # auth off, as the loader does
    setup_fastapi(container, app)

    from tenxgraph_api.src.app.routers.store.router import router as store_router

    app.include_router(store_router)
    return app


@pytest.fixture
def client(app):
    """Test client for making requests."""
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Authentication headers."""
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def sample_memory_id():
    """Sample memory ID."""
    return str(uuid4())


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
