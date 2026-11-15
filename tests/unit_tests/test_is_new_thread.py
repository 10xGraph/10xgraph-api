"""``meta.is_new_thread`` is true only for the call that creates the thread.

Runs the real ``GraphService`` over a real graph and ``InMemoryCheckpointer``. Needs a core
whose ``aput_thread`` returns True only when it created the thread (10xgraph >= 1.0.0);
older releases returned True on every call.
"""

import json

import pytest
from tenxgraph import StateGraph
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils import END

from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


def _reply(state: AgentState):
    return [Message.text_message("hi", role="assistant")]


@pytest.fixture
def service() -> GraphService:
    checkpointer = InMemoryCheckpointer()
    graph = StateGraph()
    graph.add_node("N", _reply)
    graph.set_entry_point("N")
    graph.add_edge("N", END)
    return GraphService(graph.compile(checkpointer=checkpointer), checkpointer, GraphConfig())


def _input(thread_id: str | None = None) -> GraphInputSchema:
    config = {"thread_id": thread_id} if thread_id else {}
    return GraphInputSchema(messages=[Message.text_message("hello")], config=config)


@pytest.mark.asyncio
async def test_invoke_second_call_is_not_new(service):
    first = await service.invoke_graph(_input(), None)
    assert first.meta["is_new_thread"] is True

    second = await service.invoke_graph(_input(first.meta["thread_id"]), None)
    assert second.meta["thread_id"] == first.meta["thread_id"]
    assert second.meta["is_new_thread"] is False


@pytest.mark.asyncio
async def test_stream_second_call_is_not_new(service):
    async def meta_of(graph_input):
        async for chunk in service.stream_graph(graph_input, None):
            metadata = json.loads(chunk).get("metadata") or {}
            if "is_new_thread" in metadata:
                return metadata
        return {}

    first = await meta_of(_input())
    assert first["is_new_thread"] is True
    second = await meta_of(_input(first["thread_id"]))
    assert second["is_new_thread"] is False
