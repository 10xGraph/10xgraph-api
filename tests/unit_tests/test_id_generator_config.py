"""``id_generator`` in 10xgraph.json: thread, message and memory ids from one generator."""

# ruff: noqa: S101

import json
from types import SimpleNamespace

import pytest
from injectq import InjectQ
from tenxgraph.core.graph import StateGraph
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.checkpointer.pg_checkpointer import PgCheckpointer
from tenxgraph.utils.constants import END
from tenxgraph.utils.id_generator import (
    BaseIDGenerator,
    BigIntIDGenerator,
    HexIDGenerator,
    IDType,
    IntIDGenerator,
    ShortIDGenerator,
    TimestampIDGenerator,
    UUIDGenerator,
)

from tenxgraph_api.src.app import loader
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import (
    ID_GENERATOR_NAMES,
    GraphConfig,
    RunsConfig,
)
from tenxgraph_api.src.app.core.leases import LeaseManager
from tenxgraph_api.src.app.routers.graph import run_limits, thread_lock
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.utils import snowflake_id_generator
from tenxgraph_api.src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator


THIS = "tests.unit_tests.test_id_generator_config"


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


class CountingGenerator(BaseIDGenerator):
    """String ids ``cfg-1``, ``cfg-2``, ... so a test can tell where an id came from."""

    def __init__(self) -> None:
        self.count = 0

    @property
    def id_type(self) -> IDType:
        return IDType.STRING

    def generate(self) -> str:
        self.count += 1
        return f"cfg-{self.count}"


counting_instance = CountingGenerator()


def counting_factory() -> CountingGenerator:
    return CountingGenerator()


not_a_generator = object()


def _config(value) -> SimpleNamespace:
    return SimpleNamespace(id_generator=value)


def _graph(*, container: InjectQ | None = None, id_generator=None, checkpointer=None):
    def node(state: AgentState, config: dict) -> Message:
        return Message.text_message("ok", role="assistant")

    graph = StateGraph(container=container or InjectQ(), id_generator=id_generator)
    graph.add_node("A", node)
    graph.add_edge("A", END)
    graph.set_entry_point("A")
    return graph.compile(checkpointer=checkpointer or InMemoryCheckpointer())


# ---------------------------------------------------------------- names and paths


BUILTINS = {
    "uuid": (UUIDGenerator, IDType.STRING),
    "hex": (HexIDGenerator, IDType.STRING),
    "short": (ShortIDGenerator, IDType.STRING),
    "int": (IntIDGenerator, IDType.INTEGER),
    "bigint": (BigIntIDGenerator, IDType.BIGINT),
    "timestamp": (TimestampIDGenerator, IDType.INTEGER),
    "snowflake": (SnowFlakeIdGenerator, IDType.BIGINT),
}


def test_every_config_name_has_a_generator():
    assert set(ID_GENERATOR_NAMES) == set(loader.BUILTIN_ID_GENERATORS) == set(BUILTINS)


@pytest.mark.parametrize("name", sorted(BUILTINS))
@pytest.mark.asyncio
async def test_each_builtin_name(name, monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_NODE_ID", "1")
    monkeypatch.setenv("SNOWFLAKE_WORKER_ID", "1")
    generator = loader.load_id_generator(name)
    cls, id_type = BUILTINS[name]
    assert type(generator) is cls
    assert generator.id_type == id_type
    value = generator.generate()
    if hasattr(value, "__await__"):
        value = await value
    assert isinstance(value, str if id_type == IDType.STRING else int)
    assert value not in ("", 0)


def test_snowflake_without_its_package_names_the_extra(monkeypatch):
    monkeypatch.setattr(snowflake_id_generator, "HAS_SNOWFLAKE", False)
    with pytest.raises(ValueError) as info:
        loader.load_id_generator("snowflake")
    assert str(info.value) == (
        'id_generator "snowflake" needs the snowflakekit package: '
        'pip install "10xgraph-api[snowflakekit]"'
    )


def test_custom_paths():
    assert loader.load_id_generator(f"{THIS}:counting_instance") is counting_instance
    assert type(loader.load_id_generator(f"{THIS}:CountingGenerator")) is CountingGenerator
    made = loader.load_id_generator(f"{THIS}:counting_factory")
    assert type(made) is CountingGenerator and made is not counting_instance
    with pytest.raises(TypeError, match="not a BaseIDGenerator"):
        loader.load_id_generator(f"{THIS}:not_a_generator")
    with pytest.raises(ValueError, match="cannot be loaded"):
        loader.load_id_generator(f"{THIS}:missing")
    with pytest.raises(ValueError, match="cannot be loaded"):
        loader.load_id_generator("no_such_module_xyz:gen")


def test_graph_config_property(tmp_path):
    path = tmp_path / "10xgraph.json"

    def parsed(value):
        path.write_text(json.dumps({"agent": "g:app", "id_generator": value}))
        return GraphConfig(str(path)).id_generator

    assert parsed(None) is None
    assert parsed("") is None
    assert parsed("bigint") == "bigint"
    assert parsed("my_app.ids:gen") == "my_app.ids:gen"
    for bad in ("ulid", 5, ["uuid"]):
        with pytest.raises(ValueError) as info:
            parsed(bad)
        assert str(info.value).startswith(
            "id_generator must be one of uuid, hex, short, int, bigint, timestamp, snowflake "
            'or "module:attr"'
        )


# ---------------------------------------------------------------- applying it


def test_absent_leaves_the_graph_alone():
    graph = _graph()
    before = graph._state_graph._id_generator
    assert loader.apply_id_generator(graph, _config(None), InjectQ()) is None
    assert graph._state_graph._id_generator is before


def test_a_generator_in_code_and_in_config_refuses_boot():
    graph = _graph(id_generator=HexIDGenerator())
    with pytest.raises(ValueError) as info:
        loader.apply_id_generator(graph, _config("bigint"), InjectQ())
    message = str(info.value)
    assert "HexIDGenerator (StateGraph(id_generator=...))" in message
    assert "id_generator 'bigint'" in message
    assert "one place" in message


def test_applying_twice_to_the_same_graph_is_not_a_conflict():
    graph = _graph()
    first = loader.apply_id_generator(graph, _config("hex"), InjectQ())
    second = loader.apply_id_generator(graph, _config("hex"), InjectQ())
    assert type(first) is type(second) is HexIDGenerator


def test_every_container_gets_the_generator():
    graph_container, api_container = InjectQ(), InjectQ()
    graph = _graph(container=graph_container)
    generator = loader.apply_id_generator(graph, _config(f"{THIS}:counting_factory"), api_container)
    assert isinstance(generator, CountingGenerator)
    for container in (graph_container, api_container, InjectQ.get_instance()):
        assert container.get(BaseIDGenerator) is generator
        assert container.get("generated_id_type") == IDType.STRING
        assert container.get("generated_id").startswith("cfg-")
    assert graph._state_graph._id_generator is generator  # GET /v1/graph names it


def _pg(id_type) -> PgCheckpointer:
    checkpointer = PgCheckpointer.__new__(PgCheckpointer)  # no database needed
    checkpointer.id_type = id_type
    return checkpointer


@pytest.mark.parametrize(
    ("name", "column", "fix"),
    [
        ("bigint", "string", 'id_type="bigint"'),
        ("timestamp", "string", 'id_type="bigint"'),
        ("uuid", "bigint", 'id_type="string"'),
        ("hex", "int", 'id_type="string"'),
        ("bigint", "int", 'id_type="bigint"'),
        ("bigint", IDType.STRING, 'id_type="bigint"'),
    ],
)
def test_a_pg_checkpointer_that_cannot_hold_the_ids_refuses_boot(name, column, fix):
    container = InjectQ()
    graph = _graph(container=container)
    container.bind_instance(BaseCheckpointer, _pg(column))
    with pytest.raises(ValueError) as info:
        loader.apply_id_generator(graph, _config(name), container)
    message = str(info.value)
    assert f"id_generator {name!r} makes" in message
    assert f"Pass {fix} to PgCheckpointer(...)" in message


@pytest.mark.parametrize(
    ("name", "column"),
    [("uuid", "string"), ("bigint", "bigint"), ("int", "bigint"), ("int", "int")],
)
def test_a_pg_checkpointer_that_holds_the_ids_boots(name, column):
    container = InjectQ()
    graph = _graph(container=container)
    container.bind_instance(BaseCheckpointer, _pg(column))
    assert loader.apply_id_generator(graph, _config(name), container) is not None


# ---------------------------------------------------------------- live: a real run


@pytest.fixture
def leases(monkeypatch):
    locks, slots = LeaseManager(InMemoryCacheStore()), LeaseManager(InMemoryCacheStore())
    monkeypatch.setattr(thread_lock, "lease_manager", lambda: locks)
    monkeypatch.setattr(run_limits, "lease_manager", lambda: slots)


def _service(graph) -> GraphService:
    service = GraphService.__new__(GraphService)
    service._graph = graph
    service.config = SimpleNamespace(runs=RunsConfig(thread_ids="server"))
    service.checkpointer = graph._checkpointer
    service.thread_name_generator = None
    service._media_service = None
    service._telemetry = None
    return service


@pytest.mark.asyncio
async def test_new_thread_and_message_ids_come_from_the_configured_generator(leases):
    container = InjectQ()
    graph = _graph(container=container)
    generator = loader.apply_id_generator(graph, _config(f"{THIS}:counting_factory"), container)
    assert isinstance(generator, CountingGenerator)
    service = _service(graph)

    # The user's message id is made by the schema (the server replaces client ids).
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    result = await service.invoke_graph(graph_input, {"user_id": "alice"})

    thread_id = result.meta["thread_id"]
    assert thread_id.startswith("cfg-")
    state = await graph._checkpointer.aget_state({"thread_id": thread_id, "user_id": "alice"})
    ids = [m.message_id for m in state.context]
    assert [m.role for m in state.context] == ["user", "assistant"]
    assert all(str(i).startswith("cfg-") for i in ids), ids
    assert len({thread_id, *ids}) == 3
    await graph.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["bigint", "snowflake"])
async def test_integer_ids_on_a_real_run(leases, monkeypatch, name):
    """Snowflake's generate() is async: core resolves it on the message path too."""
    monkeypatch.setenv("SNOWFLAKE_NODE_ID", "1")
    monkeypatch.setenv("SNOWFLAKE_WORKER_ID", "1")
    container = InjectQ()
    graph = _graph(container=container)
    loader.apply_id_generator(graph, _config(name), container)
    service = _service(graph)
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    result = await service.invoke_graph(graph_input, {"user_id": "alice"})
    assert result.meta["thread_id"].isdigit()
    assert all(isinstance(m.message_id, int) for m in result.messages)
    await graph.aclose()
