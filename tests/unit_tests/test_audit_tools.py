"""Audit events for tool calls, ToolGuard denials and approvals, on real graph runs."""

# ruff: noqa: S101

import json
from types import SimpleNamespace

import pytest
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ToolDenied
from tenxgraph.core.graph import StateGraph, ToolGuard, ToolNode
from tenxgraph.core.state import AgentState, Message, StreamEvent, ToolCallBlock
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils import CallbackManager
from tenxgraph.utils.constants import END

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.src.app.core.audit import set_sinks
from tenxgraph_api.src.app.core.audit.tools import ToolAudit
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import run_limits, thread_lock
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService


ALICE = Identity(user_id="alice", tenant={"org": "acme"}, roles={"agent"})
SECRET = "4111-1111-1111-1111"
MSG = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]


class _Recorder(BaseAuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)

    def tools(self) -> list[AuditEvent]:
        return [e for e in self.events if e.event == "tool"]


class _Broken(BaseAuditSink):
    async def emit(self, event: AuditEvent) -> None:
        raise RuntimeError("sink down")


@pytest.fixture(autouse=True)
def limiters(monkeypatch):
    locks, slots = LeaseManager(InMemoryCacheStore()), LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: locks)
    monkeypatch.setattr(run_limits, "lease_manager", lambda: slots)


@pytest.fixture
def sink():
    recorder = _Recorder()
    set_sinks([recorder])
    yield recorder
    set_sinks([])


def _service(tools, calls, **guard) -> GraphService:
    """MODEL asks for ``calls`` (with a secret argument) once, TOOL runs them, MODEL ends."""

    async def model(st: AgentState):
        if st.context[-1].role == "tool":
            return Message.text_message("done", role="assistant")
        args = {"card": SECRET}
        return Message(
            role="assistant",
            content=[ToolCallBlock(id=cid, name=name, args=args) for cid, name in calls],
            tools_calls=[
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for cid, name in calls
            ],
        )

    def route(st: AgentState) -> str:
        last = st.context[-1]
        return "TOOL" if last.role == "assistant" and last.tools_calls else END

    manager = CallbackManager()
    if guard:
        ToolGuard(**guard).register(manager)
    graph = StateGraph()
    graph.add_node("MODEL", model)
    graph.add_node("TOOL", ToolNode(tools))
    graph.add_conditional_edges("MODEL", route, {"TOOL": "TOOL", END: END})
    graph.add_edge("TOOL", "MODEL")
    graph.set_entry_point("MODEL")
    compiled = graph.compile(checkpointer=InMemoryCheckpointer(), callback_manager=manager)

    service = GraphService.__new__(GraphService)
    service._graph = compiled
    service.config = SimpleNamespace(runs=RunsConfig(thread_ids="client"))
    service.checkpointer = compiled._checkpointer
    service.thread_name_generator = None
    service._media_service = None
    service._telemetry = None
    return service


def charge(card: str) -> str:
    return f"charged {card}"


def explode(card: str) -> str:
    raise RuntimeError(f"db down for {card}")


def refund(card: str) -> str:
    return f"refunded {card}"


def wire(card: str) -> str:
    return f"wired {card}"


async def _run(service: GraphService, mode: str, graph_input: GraphInputSchema) -> None:
    if mode == "invoke":
        await service.invoke_graph(graph_input, ALICE)
    else:
        async for _ in service.stream_graph(graph_input, ALICE):
            pass


def _input(thread_id: str, **extra) -> GraphInputSchema:
    if "resume" in extra:
        return GraphInputSchema(config={"thread_id": thread_id}, **extra)
    return GraphInputSchema(messages=MSG, config={"thread_id": thread_id})


def _summary(events: list[AuditEvent]) -> list[tuple]:
    return [(e.action, e.resource, e.result, e.reason, e.tool_call_id) for e in events]


MODES = ["invoke", "stream"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_tool_ok_and_error_are_recorded_once_with_identity(sink, mode):
    service = _service([charge, explode], [("c1", "charge"), ("c2", "explode")])
    await _run(service, mode, _input(f"ok-{mode}"))

    events = sink.tools()
    assert sorted(_summary(events)) == [
        ("tool:call", "tool/charge", "success", None, "c1"),
        ("tool:call", "tool/explode", "failed", None, "c2"),
    ]
    for event in events:
        assert event.user_id == "alice"
        assert event.tenant == {"org": "acme"}
        assert event.roles == ["agent"]
        assert event.thread_id == f"ok-{mode}"
        assert event.run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_guard_denial_records_reason(sink, mode):
    service = _service([charge, refund], [("c1", "charge"), ("c2", "refund")], allow={"charge"})
    await _run(service, mode, _input(f"deny-{mode}"))

    assert sorted(_summary(sink.tools())) == [
        ("tool:call", "tool/charge", "success", None, "c1"),
        ("tool:call", "tool/refund", "denied", "This tool is not on the allow list.", "c2"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_check_denial_reason_is_the_policy_sentence(sink, mode):
    def check(name, args, identity):
        raise ToolDenied("Refunds need a manager.")

    service = _service([refund], [("c1", "refund")], check=check)
    await _run(service, mode, _input(f"check-{mode}"))

    assert _summary(sink.tools()) == [
        ("tool:call", "tool/refund", "denied", "Refunds need a manager.", "c1")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("answer", "result", "reason", "call_result"),
    [
        (True, "allowed", None, "success"),
        ({"approved": True}, "allowed", None, "success"),
        (False, "denied", "rejected", "denied"),
        ({"approved": False}, "denied", "rejected", "denied"),
        (None, "denied", "cancelled", "denied"),
    ],
)
async def test_approval_decision_then_tool_outcome(sink, mode, answer, result, reason, call_result):
    service = _service([refund], [("c1", "refund")], approval={"refund"})
    thread_id = f"appr-{mode}-{answer!s}"
    await _run(service, mode, _input(thread_id))
    assert sink.tools() == []  # paused before the tool ran

    await _run(service, mode, _input(thread_id, resume=answer))
    approve, call = sink.tools()
    assert (approve.action, approve.resource, approve.result, approve.reason) == (
        "tool:approve",
        "tool/refund",
        result,
        reason,
    )
    assert approve.tool_call_id == "c1"
    assert approve.user_id == "alice"
    assert approve.thread_id == thread_id
    assert (call.action, call.result, call.tool_call_id) == ("tool:call", call_result, "c1")


@pytest.mark.asyncio
async def test_resume_of_a_plain_interrupt_records_no_approval(sink):
    from tenxgraph.utils import interrupt

    def ask(card: str) -> str:
        return str(interrupt({"question": "which?"}))

    service = _service([ask], [("c1", "ask")])
    await _run(service, "invoke", _input("plain"))
    await _run(service, "invoke", _input("plain", resume="this one"))
    assert [e.action for e in sink.tools()] == ["tool:call"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_no_arguments_or_results_in_any_event(sink, mode):
    service = _service(
        [charge, explode, refund],
        [("c1", "charge"), ("c2", "explode"), ("c3", "refund")],
        approval={"refund"},
    )
    await _run(service, mode, _input(f"leak-{mode}"))
    await _run(service, mode, _input(f"leak-{mode}", resume=True))

    assert sorted((e.action, e.tool_call_id) for e in sink.tools()) == [
        ("tool:approve", "c3"),
        ("tool:call", "c1"),
        ("tool:call", "c2"),
        ("tool:call", "c3"),
    ]
    dumped = json.dumps([e.model_dump() for e in sink.events])
    assert SECRET not in dumped
    assert "charged" not in dumped and "refunded" not in dumped and "db down" not in dumped
    assert "card" not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_broken_sink_does_not_fail_the_run(mode, caplog):
    set_sinks([_Broken()])
    try:
        service = _service([charge], [("c1", "charge")], approval={"charge"})
        await _run(service, mode, _input(f"broken-{mode}"))
        await _run(service, mode, _input(f"broken-{mode}", resume=True))
    finally:
        set_sinks([])
    state = await service.checkpointer.aget_state({"thread_id": f"broken-{mode}", "user": ALICE})
    assert state.context[-1].text() == "done"
    assert any("Audit sink _Broken failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_stream_chunks_skip_the_clients_own_tool_results(sink):
    remote_result = Message.tool_message(
        content=[{"type": "tool_result", "call_id": "client-1", "output": "ok"}]
    )
    tools = ToolAudit(ALICE, "t", "r")
    tools.skip.add("client-1")
    await tools.record(remote_result)
    assert sink.tools() == []
    # And a real stream records the server tool through stream_chunks (WebSocket and AG-UI).
    service = _service([charge], [("c1", "charge")])
    errors = [
        c
        async for c in service.stream_chunks(_input("chunks"), ALICE)
        if c.event == StreamEvent.ERROR
    ]
    assert errors == []
    assert _summary(sink.tools()) == [("tool:call", "tool/charge", "success", None, "c1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", MODES)
@pytest.mark.parametrize("then", MODES)
async def test_each_call_is_recorded_once_across_pauses_and_resumes(sink, first, then):
    """Two approvals in one turn: three runs, the finished siblings replayed twice."""
    service = _service(
        [charge, explode, refund, wire],
        [("c1", "charge"), ("c2", "explode"), ("c3", "refund"), ("c4", "wire")],
        approval={"refund", "wire"},
    )
    thread_id = f"once-{first}-{then}"
    await _run(service, first, _input(thread_id))
    assert sorted(e.tool_call_id for e in sink.tools()) == ["c1", "c2"]
    await _run(service, then, _input(thread_id, resume=True))
    await _run(service, then, _input(thread_id, resume=False))

    calls = [e for e in sink.tools() if e.action == "tool:call"]
    assert sorted((e.tool_call_id, e.result) for e in calls) == [
        ("c1", "success"),
        ("c2", "failed"),
        ("c3", "success"),
        ("c4", "denied"),
    ]
    approvals = [(e.result, e.reason) for e in sink.tools() if e.action == "tool:approve"]
    assert approvals == [("allowed", None), ("denied", "rejected")]


@pytest.mark.asyncio
async def test_audit_off_records_nothing():
    set_sinks([])
    service = _service([charge], [("c1", "charge")])
    await _run(service, "invoke", _input("off"))  # no error, nothing to assert beyond that
