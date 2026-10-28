"""Run-trace capture and reconstruction (observability)."""

from tenxgraph.core.state import Message
from tenxgraph.core.state.message import TokenUsages
from tenxgraph.core.state.message_block import TextBlock, ToolCallBlock, ToolResultBlock
from tenxgraph.core.state.stream_chunks import StreamChunk, StreamEvent

from tenxgraph_api.src.app.utils.run_trace import (
    CLIP_CHARS,
    clip,
    reconstruct_run,
    telemetry_record,
)
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore


def _updates(status, node, step=None, **extra):
    return StreamChunk(
        event=StreamEvent.UPDATES, data={"status": status, "node": node, "step": step, **extra}
    )


def _msg(role, content, node, usage=None, model=None):
    m = Message(
        role=role,
        content=content,
        usages=usage,
        metadata={"model": model} if model else {},
    )
    return StreamChunk(event=StreamEvent.MESSAGE, message=m, metadata={"node": node})


def _build():
    store = TelemetryStore()
    trace = store.start_run("t", "r", 100.0)
    usage = TokenUsages(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    usage2 = TokenUsages(prompt_tokens=20, completion_tokens=6, total_tokens=26)
    seq = [
        (100.0, _msg("user", [TextBlock(text="weather?")], "")),
        (100.1, _updates("node_invoked", "agent", 0)),
        (
            100.6,
            _msg(
                "assistant",
                [ToolCallBlock(id="c1", name="get_weather", args={"city": "Paris"})],
                "agent",
                usage,
                "gpt-x",
            ),
        ),
        (100.7, _updates("node_invoked", "tools", 1)),
        (
            100.7,
            _updates(
                "invoking_tool",
                "tools",
                None,
                tool_name="get_weather",
                tool_call_id="c1",
                args={"city": "Paris"},
            ),
        ),
        (101.2, _msg("tool", [ToolResultBlock(call_id="c1", output="sunny")], "tools")),
        (101.3, _updates("node_invoked", "agent", 2)),
        (101.9, _msg("assistant", [TextBlock(text="Sunny.")], "agent", usage2, "gpt-x")),
    ]
    for ts, chunk in seq:
        trace.add(telemetry_record(chunk, ts))
    store.finish_run("t", "r", 102.0, "done")
    return trace


def test_visits_llm_tool_spans_and_timing():
    run = reconstruct_run(_build())
    nodes = [s for s in run.spans if s.kind == "node"]
    assert [(s.name, s.visit) for s in nodes] == [
        ("node: agent", 1),
        ("node: tools", 1),
        ("node: agent", 2),
    ]
    assert run.iterations == 3
    llms = [s for s in run.spans if s.kind == "llm"]
    assert len(llms) == 2 and llms[0].duration_ms > 0
    assert llms[0].model == "gpt-x" and run.model == "gpt-x"
    assert llms[0].outputs["tool_calls"][0]["name"] == "get_weather"
    assert llms[1].outputs["text"] == "Sunny."
    tool = next(s for s in run.spans if s.kind == "tool")
    assert abs(tool.duration_ms - 500.0) < 1
    assert tool.inputs == {"args": {"city": "Paris"}}
    assert tool.outputs == {"result": "sunny"}
    assert tool.status == "ok"
    tools_node = nodes[1]
    assert tool.parent == tools_node.id
    root = run.spans[0]
    assert root.inputs == {"user_message": "weather?"}
    assert root.outputs == {"assistant_message": "Sunny."}
    assert run.usage.total_tokens == 41
    assert run.tool_calls == 1 and run.llm_calls == 2


def test_error_chunk_marks_span_and_run():
    store = TelemetryStore()
    trace = store.start_run("t", "r", 10.0)
    for ts, chunk in [
        (10.0, _updates("node_invoked", "tools", 0)),
        (10.1, _updates("invoking_tool", "tools", None, tool_name="x", tool_call_id="c1", args={})),
        (
            10.4,
            StreamChunk(
                event=StreamEvent.ERROR,
                data={
                    "status": "tool_failed",
                    "node": "tools",
                    "tool_call_id": "c1",
                    "error": "boom",
                },
            ),
        ),
    ]:
        trace.add(telemetry_record(chunk, ts))
    store.finish_run("t", "r", 10.5, "error")
    run = reconstruct_run(trace)
    tool = next(s for s in run.spans if s.kind == "tool")
    assert tool.status == "error" and tool.error == "boom"
    assert run.error == "boom" and run.spans[0].status == "error"


def test_invoke_style_records_without_nodes_get_note():
    store = TelemetryStore()
    trace = store.start_run("t", "r", 1.0)
    trace.add(telemetry_record(_msg("assistant", [TextBlock(text="hi")], ""), 1.5))
    run = reconstruct_run(trace)
    assert run.iterations == 0
    assert any("non-streaming" in n for n in run.notes)


def test_clip_truncates_and_keeps_json():
    assert clip({"a": 1}) == {"a": 1}
    long = clip("x" * (CLIP_CHARS + 10))
    assert long.endswith("...[truncated 10 chars]")


def test_observability_flags(monkeypatch):
    import asyncio

    from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

    svc = GraphService.__new__(GraphService)
    svc._telemetry = None
    monkeypatch.setattr(GraphService, "telemetry", property(lambda self: None))
    out = asyncio.run(svc.get_observability("t"))
    assert out.enabled is False and out.reason in {"disabled_in_production", "store_unavailable"}

    store = TelemetryStore()
    monkeypatch.setattr(GraphService, "telemetry", property(lambda self: store))
    out = asyncio.run(svc.get_observability("t"))
    assert out.enabled is True and out.reason == "no_runs" and out.run is None
