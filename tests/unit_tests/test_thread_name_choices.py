"""``thread_name_generator``: random (default), first_message, none, or module:attr."""

# ruff: noqa: S101, PLR2004

import asyncio
import json
from types import SimpleNamespace

import pytest
from injectq import InjectQ
from tenxgraph.core.graph import StateGraph
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.constants import END

from tenxgraph_api.src.app import loader
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import run_limits, thread_lock
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.utils.thread_name_generator import (
    FIRST_MESSAGE_NAME_LENGTH,
    DefaultThreadNameGenerator,
    FirstMessageThreadNameGenerator,
    ThreadNameGenerator,
)


THIS = "tests.unit_tests.test_thread_name_choices"


@pytest.fixture(autouse=True)
def _restore_active_container():
    """``StateGraph(container=...)`` activates its container; give the next test the old one."""
    from injectq.core.context import ContainerContext

    previous = ContainerContext.get_current()
    yield
    if previous is None:
        ContainerContext.clear_current()
    else:
        ContainerContext.set_current(previous)


class FixedNamer(ThreadNameGenerator):
    async def generate_name(self, messages: list[str]) -> str:
        return "fixed"


# ---------------------------------------------------------------- config


def test_graph_config_choices(tmp_path):
    path = tmp_path / "10xgraph.json"

    def parsed(value):
        path.write_text(json.dumps({"agent": "g:app", "thread_name_generator": value}))
        return GraphConfig(str(path)).thread_name_generator

    assert parsed(None) == "random"
    assert parsed("") == "random"
    for value in ("random", "first_message", "none", "graph.names:Namer"):
        assert parsed(value) == value
    for bad in ("first", "llm", 3):
        with pytest.raises(ValueError, match="thread_name_generator must be one of random"):
            parsed(bad)


def test_each_choice_resolves_to_its_generator():
    assert type(loader.resolve_thread_name_generator("random")) is DefaultThreadNameGenerator
    first = loader.resolve_thread_name_generator("first_message")
    assert type(first) is FirstMessageThreadNameGenerator
    assert loader.resolve_thread_name_generator("none") is None
    custom = loader.resolve_thread_name_generator(f"{THIS}:FixedNamer")
    assert type(custom) is FixedNamer


# ---------------------------------------------------------------- first_message


def _first(messages: list[str]) -> str:
    return asyncio.run(FirstMessageThreadNameGenerator().generate_name(messages))


def test_first_message_collapses_whitespace_and_keeps_short_text():
    assert _first(["  Plan   a\n trip\tto  Rome  "]) == "Plan a trip to Rome"
    assert _first(["", "   ", "the reply"]) == "the reply"


def test_first_message_cuts_on_a_word_boundary_at_60_characters():
    assert FIRST_MESSAGE_NAME_LENGTH == 60
    text = "Summarise the quarterly revenue report and list the three biggest risks for us"
    name = _first([text])
    assert name == "Summarise the quarterly revenue report and list the three"
    assert len(name) <= 60
    assert text.startswith(name) and text[len(name)] == " "  # whole words, nothing added

    exactly = "word " * 11 + "abcd"
    assert len(exactly) == 59
    assert _first([exactly + " more"]) == exactly
    sixty = "a" * 30 + " " + "b" * 29  # 60 characters: kept whole
    assert _first([sixty + " tail"]) == sixty
    assert _first(["x" * 80]) == "x" * 60  # one long word has no boundary to cut at


def test_first_message_without_text_is_a_random_name():
    name = _first(["", "  "])
    assert name and "-" in name
    assert _first([])


# ---------------------------------------------------------------- live: a real run


@pytest.fixture
def leases(monkeypatch):
    locks, slots = LeaseManager(InMemoryCacheStore()), LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: locks)
    monkeypatch.setattr(run_limits, "lease_manager", lambda: slots)


def _graph():
    def node(state: AgentState, config: dict) -> Message:
        return Message.text_message("Sure, here is the plan.", role="assistant")

    graph = StateGraph(container=InjectQ())
    graph.add_node("A", node)
    graph.add_edge("A", END)
    graph.set_entry_point("A")
    return graph.compile(checkpointer=InMemoryCheckpointer())


def _service(graph, choice: str) -> GraphService:
    service = GraphService.__new__(GraphService)
    service._graph = graph
    service.config = SimpleNamespace(runs=RunsConfig())
    service.checkpointer = graph._checkpointer
    service.thread_name_generator = loader.resolve_thread_name_generator(choice)
    service._media_service = None
    service._telemetry = None
    return service


PROMPT = "  Plan a   week-long trip to Rome with museums, food tours and a day in Florence too "


async def _run(choice: str, *, stream: bool) -> tuple[GraphService, str | None]:
    """Run once on a new thread; the thread's saved name (the thread itself must exist)."""
    graph = _graph()
    service = _service(graph, choice)
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": PROMPT}]}]
    )
    user = {"user_id": "alice"}
    if stream:
        thread_id = None
        async for line in service.stream_graph(graph_input, user):
            chunk = json.loads(line)
            thread_id = thread_id or (chunk.get("metadata") or {}).get("thread_id")
    else:
        thread_id = (await service.invoke_graph(graph_input, user)).meta["thread_id"]
    await service.drain_naming_tasks()
    await graph._state_graph._task_manager.wait_for_all()  # naming runs on the graph's manager
    info = await service.checkpointer.aget_thread({"thread_id": str(thread_id), "user_id": "alice"})
    await graph.aclose()
    assert info is not None  # the thread itself is always saved
    return service, info.thread_name


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_first_message_names_the_thread_after_the_user_text(leases, stream):
    _, name = await _run("first_message", stream=stream)
    assert name == ("Plan a week-long trip to Rome with museums, food tours and a")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_the_default_random_name_is_saved(leases, stream):
    """The bug: the default name was generated and dropped, so threads stayed unnamed."""
    _, name = await _run("random", stream=stream)
    assert name and "-" in name


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_none_leaves_the_thread_unnamed_but_saved(leases, stream):
    service, name = await _run("none", stream=stream)
    assert name is None
    assert not service.__dict__.get("_naming_tasks")


@pytest.mark.asyncio
async def test_a_custom_generator_is_saved_the_same_way(leases):
    _, name = await _run(f"{THIS}:FixedNamer", stream=False)
    assert name == "fixed"
