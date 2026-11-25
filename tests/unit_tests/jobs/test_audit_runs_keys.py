"""Worker audit (W4), runs of HTTP schedules (S5), REDIS_KEY_PREFIX on jobs keys (S6) and the
task name as the authorize resource (L11)."""

# ruff: noqa: S101

from __future__ import annotations

import json
import uuid

import pytest
from tenxgraph.core.authz import Identity
from tenxgraph.core.graph import StateGraph, ToolGuard, ToolNode
from tenxgraph.core.state import AgentState, Message, ToolCallBlock
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils import CallbackManager
from tenxgraph.utils.constants import END

from tenxgraph_api import AuditEvent, BaseAuditSink
from tenxgraph_api.jobs import state
from tenxgraph_api.jobs.config import WorkerConfig, build_broker, build_schedule_source
from tenxgraph_api.jobs.scheduler import LEASE_KEY, LeasedScheduler
from tenxgraph_api.jobs.tool_audit import TaskToolAudit, setup_worker_audit
from tenxgraph_api.src.app.core.audit import audit_enabled, set_sinks
from tenxgraph_api.src.app.core.audit.config import AuditConfig
from tenxgraph_api.src.app.core.config.settings import get_settings

from .test_auth import ROLES, Jobs, _kiq, auth_config
from .test_routes import ADD, Env, TokenAuth


class Sink(BaseAuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> None:
        self.events.append(event)

    def of(self, action: str) -> list[AuditEvent]:
        return [e for e in self.events if e.action == action]


@pytest.fixture
def sink():
    recorder = Sink()
    set_sinks([recorder])
    yield recorder
    set_sinks([])


# ---- W4: the worker's tasks:trigger decision


@pytest.mark.asyncio
async def test_worker_trigger_decision_is_audited_allowed_and_denied(monkeypatch, sink):
    jobs = Jobs(monkeypatch)
    allowed = await _kiq(jobs.nightly)
    assert allowed.is_err is False
    (event,) = sink.of("tasks:trigger")
    assert (event.event, event.result, event.user_id) == ("access", "allowed", "__system__:nightly")
    assert event.resource.startswith("task/tests.nightly/")

    sink.events.clear()
    jobs.auth.rbac = auth_config(roles={**ROLES, "member": ROLES["reader"]}).rbac_config()
    assert isinstance((await _kiq(jobs.nightly)).error, PermissionError)
    (event,) = sink.of("tasks:trigger")
    assert (event.result, event.reason) == ("denied", None)

    sink.events.clear()
    task_id = uuid.uuid4().hex
    assert isinstance((await _kiq(jobs.plain, {"user": {"user_id": ""}}, task_id)).error, Exception)
    (event,) = sink.of("tasks:trigger")
    assert (event.result, event.reason) == ("denied", "invalid_identity")
    assert event.resource == f"task/tests.plain/{task_id}"


def test_setup_worker_audit_configures_sinks_and_hook():
    from injectq import InjectQ

    container = InjectQ()
    callbacks = CallbackManager()
    container.bind_instance(CallbackManager, callbacks)

    class Config:
        audit = AuditConfig(enabled=True, sinks=["log"])

    try:
        setup_worker_audit(container, Config())
        assert audit_enabled()
        assert any(isinstance(h, TaskToolAudit) for h in callbacks._lifecycle_hooks)
    finally:
        set_sinks([])

    Config.audit = AuditConfig()
    setup_worker_audit(container, Config())
    assert not audit_enabled()


# ---- W4: tool calls of a graph run inside a task, on a real graph


ALICE = Identity(user_id="alice", tenant={"org": "acme"}, roles={"agent"})


def charge(card: str) -> str:
    return f"charged {card}"


def explode(card: str) -> str:
    raise RuntimeError("down")


def _graph(tools, calls, **guard):
    async def model(st: AgentState):
        if st.context[-1].role == "tool":
            return Message.text_message("done", role="assistant")
        args = {"card": "x"}
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
    checkpointer = InMemoryCheckpointer()
    manager.register_lifecycle_hook(TaskToolAudit(checkpointer))
    graph = StateGraph()
    graph.add_node("MODEL", model)
    graph.add_node("TOOL", ToolNode(tools))
    graph.add_conditional_edges("MODEL", route, {"TOOL": "TOOL", END: END})
    graph.add_edge("TOOL", "MODEL")
    graph.set_entry_point("MODEL")
    return graph.compile(checkpointer=checkpointer, callback_manager=manager)


def _config(thread_id: str) -> dict:
    return {"thread_id": thread_id, "user": ALICE.model_dump(mode="json"), "user_id": "alice"}


@pytest.mark.asyncio
async def test_tool_calls_in_a_task_graph_run_are_audited(sink):
    graph = _graph([charge, explode], [("c1", "charge"), ("c2", "explode")])
    await graph.ainvoke({"messages": [Message.text_message("go")]}, config=_config("t-run"))
    calls = sorted((e.resource, e.result, e.tool_call_id) for e in sink.of("tool:call"))
    assert calls == [("tool/charge", "success", "c1"), ("tool/explode", "failed", "c2")]
    event = sink.of("tool:call")[0]
    assert (event.user_id, event.thread_id) == ("alice", "t-run")
    assert event.run_id


@pytest.mark.asyncio
async def test_approval_in_a_task_graph_run_is_audited_once(sink):
    graph = _graph([charge], [("c1", "charge")], approval={"charge"})
    await graph.ainvoke({"messages": [Message.text_message("go")]}, config=_config("t-appr"))
    assert sink.of("tool:call") == []
    await graph.ainvoke({"resume": True}, config=_config("t-appr"))
    (approve,) = sink.of("tool:approve")
    assert (approve.result, approve.resource, approve.tool_call_id) == (
        "allowed",
        "tool/charge",
        "c1",
    )
    assert [(e.result, e.tool_call_id) for e in sink.of("tool:call")] == [("success", "c1")]


# ---- S5: runs of an HTTP schedule


def _scheduler(env: Env) -> LeasedScheduler:
    scheduler = LeasedScheduler(env.broker, [env.source], None)
    scheduler.lease_until = float("inf")  # leader without startup
    scheduler.cache_store = env.store
    return scheduler


@pytest.mark.asyncio
async def test_scheduled_runs_are_listed_and_readable_by_task_id(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"every": 30}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    scheduler = _scheduler(env)
    for _ in range(2):
        await scheduler.on_ready(env.source, env.source.schedules[schedule_id])

    runs = env.call("GET", f"/v1/tasks/{schedule_id}").json()["data"]["runs"]
    assert len(runs) == 2
    data = env.call("GET", f"/v1/tasks/{runs[0]}").json()["data"]
    assert (data["status"], data["result"], data["owner"]) == ("success", 3, "u1")
    assert env.call("GET", f"/v1/tasks/{runs[0]}", user="u2").status_code == 404
    assert env.call("POST", f"/v1/tasks/{runs[0]}/cancel", user="u2").status_code == 404
    assert env.call("POST", f"/v1/tasks/{runs[0]}/cancel").status_code == 200

    assert env.call("POST", f"/v1/tasks/{schedule_id}/cancel").status_code == 200
    assert await state.read_schedule_runs(env.store, schedule_id) == []


@pytest.mark.asyncio
async def test_scheduled_run_is_queued_with_owner_before_the_send(monkeypatch):
    env = Env(monkeypatch)
    body = {"args": [1, 2], "schedule": {"every": 30}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    scheduler = _scheduler(env)
    seen: list[state.TaskStatus | None] = []

    async def kiq_spy(self, *args, **kwargs):
        seen.append(await state.read_status(env.store, self.custom_task_id))

    from taskiq.kicker import AsyncKicker

    monkeypatch.setattr(AsyncKicker, "kiq", kiq_spy)
    await scheduler.on_ready(env.source, env.source.schedules[schedule_id])
    (record,) = seen
    assert (record.status, record.owner) == ("queued", "u1")


@pytest.mark.asyncio
async def test_schedule_runs_are_bounded(monkeypatch):
    env = Env(monkeypatch)
    for n in range(state.MAX_SCHEDULE_RUNS + 5):
        await state.add_schedule_run(env.store, "s1", f"t{n}")
    runs = await state.read_schedule_runs(env.store, "s1")
    assert len(runs) == state.MAX_SCHEDULE_RUNS
    assert runs[0] == f"t{state.MAX_SCHEDULE_RUNS + 4}"


# ---- S6: REDIS_KEY_PREFIX on every jobs key


REDIS = WorkerConfig(broker={"type": "redis_stream", "url": "redis://localhost:1/0"})


def test_no_prefix_keeps_todays_key_names(monkeypatch):
    monkeypatch.setattr(get_settings(), "REDIS_KEY_PREFIX", "")
    broker = build_broker(REDIS)
    assert (broker.queue_name, broker.consumer_group_name) == ("taskiq", "taskiq")
    assert broker.result_backend.prefix_str is None
    assert build_schedule_source(REDIS)._get_data_key("s1") == "schedule:data:s1"
    assert LeasedScheduler(broker, [], None).lease_key == LEASE_KEY


def test_prefix_applies_to_every_jobs_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "REDIS_KEY_PREFIX", "jca")
    broker = build_broker(REDIS)
    assert (broker.queue_name, broker.consumer_group_name) == ("jca:taskiq", "jca:taskiq")
    assert broker.result_backend.prefix_str == "jca"
    assert build_schedule_source(REDIS)._get_data_key("s1") == "jca:schedule:data:s1"
    assert LeasedScheduler(broker, [], None).lease_key == f"jca:{LEASE_KEY}"


def test_explicit_broker_options_win_over_the_prefix(monkeypatch):
    monkeypatch.setattr(get_settings(), "REDIS_KEY_PREFIX", "jca")
    cfg = WorkerConfig(
        broker={
            "type": "redis_stream",
            "url": "redis://localhost:1/0",
            "options": {"queue_name": "q"},
        }
    )
    assert build_broker(cfg).queue_name == "q"


# ---- L11: the task name is the authorize resource


class AskedAuth(TokenAuth):
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def authorize(self, identity, permission, resource=None):
        self.asked.append((permission, resource))
        return await super().authorize(identity, permission, resource)


def test_authorize_gets_task_name_on_every_tasks_route(monkeypatch, sink):
    auth = AskedAuth()
    env = Env(monkeypatch, auth_impl=auth)
    task_id = env.call("POST", f"/v1/tasks/{ADD}", json={"args": [1, 2]}).json()["data"]["task_id"]
    body = {"args": [1, 2], "schedule": {"every": 30}}
    schedule_id = env.call("POST", f"/v1/tasks/{ADD}", json=body).json()["data"]["schedule_id"]
    assert env.call("GET", f"/v1/tasks/{task_id}").status_code == 200
    patched = env.call("PATCH", f"/v1/tasks/{schedule_id}", json={"schedule": {"every": 60}})
    assert patched.status_code == 200
    assert env.call("POST", f"/v1/tasks/{schedule_id}/cancel").status_code == 200
    assert env.call("GET", "/v1/tasks/nope").status_code == 404
    assert env.call("GET", f"/v1/tasks/{task_id}", user="u2").status_code == 404
    assert auth.asked == [
        ("tasks:trigger", ADD),
        ("tasks:trigger", ADD),  # the worker's check of the in-place run
        ("tasks:trigger", ADD),
        ("tasks:read", ADD),
        ("tasks:trigger", ADD),
        ("tasks:cancel", ADD),
        ("tasks:read", None),  # unknown id: no name, then 404
        ("tasks:read", None),  # another user's task: no name either
    ]
    reads = [e.resource for e in sink.of("tasks:read") if e.event == "access"]
    assert reads[0] == f"task/{ADD}/{task_id}"
    assert sink.of("tasks:trigger")[0].resource == f"task/{ADD}"
