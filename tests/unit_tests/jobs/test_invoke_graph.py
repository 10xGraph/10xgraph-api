"""``ctx.invoke_graph`` and ``ctx.thread_id()`` with a tiny graph and the memory checkpointer."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from tenxgraph.core.graph import StateGraph
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.constants import END

from tenxgraph_api.jobs import TaskContext, task
from tenxgraph_api.jobs.context import derived_thread_id
from tenxgraph_api.jobs.testing import jobs_harness


pytestmark = pytest.mark.asyncio

U1 = {"user_id": "u1", "org_id": "acme"}
SEEN: list[dict] = []


def _node(state: AgentState, config: dict) -> Message:
    SEEN.append(
        {
            "thread_id": config.get("thread_id"),
            "run_id": config.get("run_id"),
            "user_id": config.get("user_id"),
            "user": config.get("user"),
            "metadata": config.get("metadata"),
            "users": sum(1 for m in state.context if m.role == "user"),
        }
    )
    return Message.text_message("ok", role="assistant")


def make_graph():
    g = StateGraph()
    g.add_node("A", _node)
    g.add_edge("A", END)
    g.set_entry_point("A")
    return g.compile(checkpointer=InMemoryCheckpointer())


def hi() -> dict:
    return {"messages": [Message.text_message("hi", role="user")]}


@task(name="ig_flaky", retries=1, backoff=0)
async def ig_flaky(ctx: TaskContext) -> int:
    result = await ctx.invoke_graph(hi())
    if ctx.attempt == 1:
        raise RuntimeError("crash after the graph ran")
    return len(result["messages"])


def ctx_for(graph, run_id: str, attempt: int) -> TaskContext:
    return TaskContext(
        run_id=run_id, attempt=attempt, task="t", queue="default", identity=dict(U1), emit=None, graph=graph
    )


async def test_config_shape_and_input_rule():
    SEEN.clear()
    graph = make_graph()
    run_id = uuid.uuid4().hex
    first = ctx_for(graph, run_id, 1)
    await first.invoke_graph(hi(), config={"thread_id": "evil", "user_id": "x", "recursion_limit": 7})
    second = ctx_for(graph, run_id, 2)
    await second.invoke_graph(hi())
    a, b = SEEN
    assert a["thread_id"] == b["thread_id"] == first.thread_id()  # task config cannot change it
    assert a["run_id"] == f"{run_id}:1" and b["run_id"] == f"{run_id}:2"
    assert a["user_id"] == "u1" and a["user"]["org_id"] == "acme"
    assert a["metadata"] == {"task_run_id": run_id, "task": "t", "attempt": 1}
    # The input goes in on attempt 1 only: the second attempt does not add the user message.
    assert a["users"] == 1 and b["users"] == 1
    # The thread is saved with the run's user as owner.
    owner = await graph._checkpointer.aget_thread_owner(first.thread_id())
    assert owner == "u1"
    await graph.aclose()


async def test_retry_continues_the_thread_without_resending_input():
    SEEN.clear()
    graph = make_graph()
    async with jobs_harness() as jobs:
        jobs.worker.graph = graph
        run = await ig_flaky.options(identity=U1).enqueue()
        await jobs.drain(follow_delays=True)
        stored = await run.status()
        assert stored.state.value == "succeeded", stored.error
        assert stored.attempt == 2
    assert [s["run_id"].rsplit(":", 1)[1] for s in SEEN] == ["1", "2"]
    assert len({s["thread_id"] for s in SEEN}) == 1
    assert SEEN[-1]["users"] == 1
    await graph.aclose()


async def test_thread_id_for_numeric_checkpointers():
    run_id = "run-42"
    plain = ctx_for(None, run_id, 3)
    assert plain.thread_id() == str(uuid.uuid5(uuid.UUID("5b0f8e8e-6d44-4b8e-9f0e-10a9a7a0c0de"), run_id))
    for id_type, top in (("int", 2**31 - 1), ("bigint", 2**63 - 1)):
        graph = SimpleNamespace(_checkpointer=SimpleNamespace(id_type=id_type))
        tid = ctx_for(graph, run_id, 1).thread_id()
        assert isinstance(tid, int) and 1 <= tid <= top
        assert tid == ctx_for(graph, run_id, 9).thread_id() == derived_thread_id(run_id, id_type)
        assert tid != derived_thread_id("run-43", id_type)
    string_graph = SimpleNamespace(_checkpointer=SimpleNamespace(id_type="string"))
    assert isinstance(ctx_for(string_graph, run_id, 1).thread_id(), str)
