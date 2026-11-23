"""Audit events for tool calls in a ``/v1/graph/live`` session, on a real LiveAgent."""

# ruff: noqa: S101

import json
from unittest.mock import MagicMock

import pytest
from tenxgraph.core.authz import Identity
from tenxgraph.core.graph import StateGraph, ToolGuard, ToolNode
from tenxgraph.core.realtime.base import ToolCallEvent, ToolResultEvent
from tenxgraph.core.realtime.live_agent import LiveAgent
from tenxgraph.core.realtime.queue import LiveInputQueue
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils import CallbackManager

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.src.app.core.audit import set_sinks
from tenxgraph_api.src.app.core.audit.tools import ToolAudit
from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


ALICE = Identity(user_id="alice", tenant={"org": "acme"})
SECRET = "4111-1111-1111-1111"


class _Recorder(BaseAuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Client:
    """A realtime provider that asks for the scripted tool calls, then ends."""

    def __init__(self, calls):
        self.calls = calls
        self.responses: list = []

    async def connect(self, config, resume_handle=None):
        pass

    async def send_tool_response(self, call_id, name, result):
        self.responses.append((call_id, result))

    async def receive(self):
        for event in self.calls:
            yield event

    async def close(self):
        pass


@pytest.fixture
def sink():
    recorder = _Recorder()
    set_sinks([recorder])
    yield recorder
    set_sinks([])


def charge(card: str) -> str:
    return f"charged {card}"


def explode(card: str) -> str:
    raise RuntimeError(f"db down for {card}")


def refund(card: str) -> str:
    return f"refunded {card}"


def _service(calls, **guard) -> GraphService:
    client = _Client(
        [ToolCallEvent(id=cid, name=name, args={"card": SECRET}) for cid, name in calls]
    )
    agent = LiveAgent(
        "gemini-2.5-flash-live",
        tool_node=ToolNode([charge, explode, refund]),
        realtime_client_factory=lambda: client,
    )
    manager = CallbackManager()
    if guard:
        ToolGuard(**guard).register(manager)
    graph = StateGraph()
    graph.add_node("LIVE", agent)
    graph.set_entry_point("LIVE")
    compiled = graph.compile(checkpointer=InMemoryCheckpointer(), callback_manager=manager)
    config = MagicMock()
    config.websocket = WebSocketConfig.from_dict({})
    return GraphService(graph=compiled, checkpointer=compiled._checkpointer, config=config)


async def _session(service: GraphService, thread_id: str) -> list:
    queue = LiveInputQueue()
    queue.close()
    return [e async for e in service.realtime_graph(queue, {"thread_id": thread_id}, ALICE)]


@pytest.mark.asyncio
async def test_live_tool_ok_failed_and_denied(sink):
    service = _service(
        [("c1", "charge"), ("c2", "explode"), ("c3", "refund"), ("c4", "charge")],
        allow={"charge", "explode"},
        approval={"charge"},
    )
    events = await _session(service, "live-1")
    assert len([e for e in events if isinstance(e, ToolResultEvent)]) == 4

    summary = sorted(
        (e.tool_call_id, e.action, e.resource, e.result, e.reason) for e in sink.events
    )
    assert summary == [
        # Approval cannot pause a live session: the guard denies the call.
        (
            "c1",
            "tool:call",
            "tool/charge",
            "denied",
            "This call needs approval, but the run cannot pause.",
        ),
        ("c2", "tool:call", "tool/explode", "failed", None),
        ("c3", "tool:call", "tool/refund", "denied", "This tool is not on the allow list."),
        (
            "c4",
            "tool:call",
            "tool/charge",
            "denied",
            "This call needs approval, but the run cannot pause.",
        ),
    ]
    for event in sink.events:
        assert (event.user_id, event.tenant, event.thread_id) == (
            "alice",
            {"org": "acme"},
            "live-1",
        )
    dumped = json.dumps([e.model_dump() for e in sink.events])
    assert SECRET not in dumped and "card" not in dumped and "db down" not in dumped


@pytest.mark.asyncio
async def test_live_tool_success_and_no_result_leak(sink):
    service = _service([("c1", "charge")])
    await _session(service, "live-2")
    assert [(e.tool_call_id, e.result) for e in sink.events] == [("c1", "success")]
    assert "charged" not in json.dumps([e.model_dump() for e in sink.events])


@pytest.mark.asyncio
async def test_live_refused_call_is_failed_and_a_replay_is_recorded_once(sink):
    tools = ToolAudit(ALICE, "t", None)
    await tools.record_live(ToolCallEvent(id="x", name="ghost", args={}))
    await tools.record_live(ToolResultEvent(id="x", result={"error": "not available"}))
    await tools.record_live(ToolCallEvent(id="x", name="ghost", args={}))
    await tools.record_live(ToolResultEvent(id="x", result={"error": "not available"}))
    assert [(e.resource, e.result) for e in sink.events] == [("tool/ghost", "failed")]
