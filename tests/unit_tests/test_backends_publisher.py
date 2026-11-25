"""BackendsPublisher: one span carries the keys of every enabled backend (real OTEL SDK spans)."""

import json
import sys

import pytest


pytest.importorskip("opentelemetry.sdk")

import opentelemetry
import opentelemetry.trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tenxgraph.runtime.publisher.events import ContentType, Event, EventModel, EventType
from tenxgraph.runtime.publisher.identity import transform_user_id
from tenxgraph.runtime.publisher.otel_publisher import ObservabilityLevel

from tenxgraph_api.src.app.core.config.backends_publisher import (
    BackendsPublisher,
    enabled_backends,
)


OI_KEYS = ("openinference.", "input.", "output.", "llm.", "session.id", "user.id", "metadata")
LANGSMITH_KEYS = ("langsmith.",)
LOGFIRE_KEYS = ("logfire.",)


@pytest.fixture(autouse=True)
def _real_otel(monkeypatch):
    """Other tests may put a fake ``opentelemetry`` in sys.modules; use the real one."""
    monkeypatch.setitem(sys.modules, "opentelemetry", opentelemetry)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", opentelemetry.trace)


def _evt(event, etype, node="MAIN", data=None):
    return EventModel(
        event=event,
        event_type=etype,
        node_name=node,
        run_id="r1",
        thread_id="t1",
        user_id="alice42",
        timestamp=10.0,
        data=data or {},
        content_type=[ContentType.MESSAGE],
    )


_STATE = {"context": [{"role": "user", "content": "hi"}]}
_TOOL = {"function_name": "search", "tool_call_id": "c1", "args": {"q": 1}}
_LLM_END = {"output_response": "yo", "input_tokens": 3, "output_tokens": 2}


def _backends(names, level=ObservabilityLevel.FULL, user_id_mode="raw") -> dict[str, dict]:
    """One run with a graph, node, LLM and tool span; returns span attributes by name."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    pub = BackendsPublisher(
        names, tracer=provider.get_tracer("t"), level=level, user_id_mode=user_id_mode
    )
    for e in (
        _evt(Event.GRAPH_EXECUTION, EventType.START, node="", data={"state": _STATE}),
        _evt(Event.NODE_EXECUTION, EventType.START, data={"state": _STATE}),
        _evt(
            Event.LLM_CALL,
            EventType.START,
            data={"model": "m1", "provider": "openai", "input_messages": _STATE["context"]},
        ),
        _evt(Event.LLM_CALL, EventType.END, data=_LLM_END),
        _evt(Event.NODE_EXECUTION, EventType.END, data={"state": _STATE, "messages": []}),
        _evt(Event.TOOL_EXECUTION, EventType.START, node="search", data=_TOOL),
        _evt(Event.TOOL_EXECUTION, EventType.END, node="search", data={**_TOOL, "message": "ok"}),
        _evt(Event.GRAPH_EXECUTION, EventType.END, node="", data={"messages": []}),
    ):
        pub._dispatch(e)
    return {s.name: dict(s.attributes) for s in exporter.get_finished_spans()}


def _has(spans, prefixes) -> bool:
    return any(k.startswith(prefixes) for attrs in spans.values() for k in attrs)


def test_enabled_backends_reads_the_config_block():
    cfg = {"phoenix": {"enabled": True}, "langsmith": {"enabled": False}, "otlp": {"enabled": 1}}
    assert enabled_backends(cfg) == ["phoenix", "otlp"]
    assert enabled_backends({}) == []
    assert enabled_backends({"logfire": None}) == []


def test_otlp_backend_is_basic_only():
    spans = _backends(["otlp"])
    assert set(spans) == {"invoke_agent 10xgraph", "MAIN", "chat m1", "execute_tool search"}
    assert spans["chat m1"]["gen_ai.request.model"] == "m1"
    assert not _has(spans, OI_KEYS + LANGSMITH_KEYS + LOGFIRE_KEYS)


def test_backends_are_kept_in_a_fixed_order():
    assert BackendsPublisher(["otlp", "langsmith", "phoenix"]).backends == [
        "phoenix",
        "langsmith",
        "otlp",
    ]


def test_several_backends_write_each_dialect_on_one_span():
    spans = _backends(["langsmith", "phoenix"])
    assert not _has(spans, LOGFIRE_KEYS)
    llm = spans["chat m1"]
    assert llm["openinference.span.kind"] == "LLM" and llm["langsmith.span.kind"] == "llm"
    assert llm["session.id"] == llm["langsmith.metadata.thread_id"] == "t1"


def test_all_three_dialects_on_one_span():
    llm = _backends(["phoenix", "langsmith", "logfire"])["chat m1"]
    assert llm["logfire.msg"] == llm["langsmith.trace.name"] == "chat m1"
    assert llm["openinference.span.kind"] == "LLM"


@pytest.mark.parametrize("mode", ["hash", "mask"])
def test_user_id_mode_applies_to_every_backend_key(mode):
    spans = _backends(["phoenix", "langsmith"], user_id_mode=mode)
    expected = transform_user_id("alice42", mode)
    assert expected != "alice42"
    for attrs in spans.values():
        assert attrs["user.id"] == attrs["langsmith.metadata.user_id"] == expected
        assert "alice42" not in json.dumps(attrs)


def test_levels_hide_content_in_every_backend():
    spans_level = _backends(["phoenix", "langsmith", "logfire"], level=ObservabilityLevel.SPANS)
    assert not _has(spans_level, ("input.", "output.", "llm."))
    assert spans_level["chat m1"]["openinference.span.kind"] == "LLM"
    standard = _backends(["phoenix", "langsmith", "logfire"], level=ObservabilityLevel.STANDARD)
    llm = standard["chat m1"]
    assert llm["llm.token_count.total"] == 5
    assert not any(k.startswith(("input.", "output.", "llm.input_", "llm.output_")) for k in llm)
    assert "input.value" not in standard["invoke_agent 10xgraph"]
    assert "input.value" in standard["MAIN"]  # node state is STANDARD


def test_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="nope"):
        BackendsPublisher(["phoenix", "nope"])
