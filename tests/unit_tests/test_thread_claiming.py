"""A fresh run always gets a thread nobody else holds (M8).

- A generated id that another user already created is skipped.
- ``"new"`` over either WebSocket starts a fresh thread instead of one shared thread.
- The WebSocket owner check runs when the config cannot be read.
"""

# ruff: noqa: S101, PLR2004

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from injectq import InjectQ

from tenxgraph_api.src.app.routers.graph.services import graph_service as graph_service_module
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


graph_router = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")


def _service(owners: dict[str, str]) -> GraphService:
    service = GraphService.__new__(GraphService)
    service.checkpointer = MagicMock()
    service.checkpointer.aget_thread_owner = AsyncMock(side_effect=lambda tid: owners.get(tid))
    return service


@pytest.fixture
def generated_ids(monkeypatch):
    """Make the id generator hand out the given ids in order."""

    def _set(*ids):
        sequence = iter(ids)
        container = MagicMock()
        container.atry_get = AsyncMock(side_effect=lambda key: next(sequence))
        monkeypatch.setattr(InjectQ, "get_instance", staticmethod(lambda: container))

    return _set


@pytest.mark.asyncio
async def test_an_unused_generated_id_is_used(generated_ids):
    generated_ids("101")
    assert await _service({})._new_thread_id() == "101"


@pytest.mark.asyncio
async def test_a_claimed_generated_id_is_skipped(generated_ids):
    generated_ids("101", "102")
    assert await _service({"101": "mallory"})._new_thread_id() == "102"


@pytest.mark.asyncio
async def test_gives_up_when_every_id_is_taken(generated_ids, monkeypatch):
    monkeypatch.setattr(graph_service_module, "MAX_THREAD_ID_ATTEMPTS", 2)
    generated_ids("101", "102")
    with pytest.raises(RuntimeError, match="unused thread id"):
        await _service({"101": "m", "102": "m"})._new_thread_id()


@pytest.mark.asyncio
async def test_checkpointer_without_owner_lookup_keeps_working(generated_ids):
    generated_ids("101")
    service = _service({})
    service.checkpointer.aget_thread_owner = AsyncMock(side_effect=NotImplementedError)
    assert await service._new_thread_id() == "101"


@pytest.mark.parametrize("raw", ["new", " new ", "", None])
def test_new_placeholder_starts_a_fresh_thread(raw):
    frame = {"thread_id": raw, "voice": "Puck"}
    assert graph_router._resolve_ws_thread(frame) is None
    assert frame == {"voice": "Puck"}


def test_real_thread_id_is_normalised():
    frame = {"thread_id": "  t1 "}
    assert graph_router._resolve_ws_thread(frame) == "t1"
    assert frame == {"thread_id": "t1"}
