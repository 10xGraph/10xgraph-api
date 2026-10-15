"""Unit tests for the WebSocket graph endpoint."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from tenxgraph.core.state import StreamChunk, StreamEvent

from tenxgraph_api.src.app.routers.graph.router import websocket_graph
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import WsGraphInputSchema


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _chunk_json(event: StreamEvent, data: dict | None = None) -> str:
    """Build a StreamChunk JSON string the way stream_graph yields (with trailing \\n)."""
    return StreamChunk(event=event, data=data).model_dump_json() + "\n"


def _as_frame(item):
    """Exceptions (e.g. WebSocketDisconnect) pass through; payloads become JSON text frames."""
    if isinstance(item, BaseException) or (
        isinstance(item, type) and issubclass(item, BaseException)
    ):
        return item
    return json.dumps(item)


def _make_websocket(receive_side_effects: list) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=[_as_frame(x) for x in receive_side_effects])
    return ws


def _make_service(stream_chunks_per_call: list[list[str]]) -> MagicMock:
    """
    Mock GraphService whose stream_graph yields canned chunks per call.
    Call count is tracked via service._stream_calls.
    """
    service = MagicMock()
    service.is_live_agent = False  # turn-based graph: allowed on /v1/graph/ws
    calls: list = []

    async def _stream_generator(chunks):
        for chunk in chunks:
            yield chunk

    def _side_effect(graph_input, user):
        calls.append(graph_input)
        idx = len(calls) - 1
        chunks = stream_chunks_per_call[idx] if idx < len(stream_chunks_per_call) else []
        return _stream_generator(chunks)

    service.stream_graph = _side_effect
    service._stream_calls = calls
    return service


# ── Canned payloads ───────────────────────────────────────────────────────────

_FRESH_REQUEST = {
    "invoke_type": "fresh",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
}

_RESUME_REQUEST = {
    "invoke_type": "resume",
    "tool_result": [{"role": "tool", "content": [{"type": "text", "text": "42"}]}],
    "config": {"thread_id": "thread-abc"},
}

_DONE_JSON = StreamChunk(event=StreamEvent.UPDATES, data={"status": "done"}).model_dump_json()


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation tests
# ─────────────────────────────────────────────────────────────────────────────


class TestWsGraphInputSchema:
    def test_fresh_valid(self):
        ws = WsGraphInputSchema(**_FRESH_REQUEST)
        assert ws.invoke_type == "fresh"
        gi = ws.to_graph_input()
        assert len(gi.messages) == 1

    def test_fresh_empty_messages_raises(self):
        with pytest.raises(Exception, match="messages must not be empty"):
            WsGraphInputSchema(invoke_type="fresh", messages=[])

    def test_resume_valid(self):
        ws = WsGraphInputSchema(**_RESUME_REQUEST)
        assert ws.invoke_type == "resume"
        gi = ws.to_graph_input()
        # to_graph_input uses tool_result as messages for stream_graph
        assert gi.messages[0].role == "tool"

    def test_resume_missing_tool_result_raises(self):
        with pytest.raises(Exception, match="tool_result must not be empty"):
            WsGraphInputSchema(
                invoke_type="resume",
                tool_result=None,
                config={"thread_id": "t1"},
            )

    def test_resume_missing_thread_id_raises(self):
        with pytest.raises(Exception, match="config.thread_id is required"):
            WsGraphInputSchema(
                invoke_type="resume",
                tool_result=[{"role": "tool", "content": [{"type": "text", "text": "x"}]}],
                config=None,
            )


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestWebSocketGraphEndpoint:
    # ── 1. Fresh run ─────────────────────────────────────────────────────

    async def test_fresh_run_streams_chunks_and_done(self):
        chunk1 = _chunk_json(StreamEvent.MESSAGE, {"text": "hi"})
        chunk2 = _chunk_json(StreamEvent.UPDATES, {"status": "completed"})

        ws = _make_websocket([_FRESH_REQUEST, WebSocketDisconnect()])
        service = _make_service([[chunk1, chunk2]])

        await websocket_graph(websocket=ws, service=service, user={})

        sent = [c.args[0] for c in ws.send_text.call_args_list]
        assert chunk1 in sent
        assert chunk2 in sent
        assert _DONE_JSON in sent

    # ── 2. Resume run ────────────────────────────────────────────────────

    async def test_resume_run_passes_tool_result_to_stream_graph(self):
        resume_chunk = _chunk_json(StreamEvent.MESSAGE, {"text": "tool used"})

        ws = _make_websocket([_RESUME_REQUEST, WebSocketDisconnect()])
        service = _make_service([[resume_chunk]])

        await websocket_graph(websocket=ws, service=service, user={})

        # stream_graph was called once with tool_result as its messages
        assert len(service._stream_calls) == 1
        gi = service._stream_calls[0]
        assert gi.messages[0].role == "tool"

        sent = [c.args[0] for c in ws.send_text.call_args_list]
        assert resume_chunk in sent
        assert _DONE_JSON in sent

    # ── 3. Sequential fresh → resume ─────────────────────────────────────

    async def test_fresh_then_resume_two_stream_calls(self):
        chunk_fresh = _chunk_json(StreamEvent.MESSAGE, {"text": "fresh"})
        chunk_resume = _chunk_json(StreamEvent.MESSAGE, {"text": "resumed"})

        ws = _make_websocket([_FRESH_REQUEST, _RESUME_REQUEST, WebSocketDisconnect()])
        service = _make_service([[chunk_fresh], [chunk_resume]])

        await websocket_graph(websocket=ws, service=service, user={})

        assert len(service._stream_calls) == 2
        sent = [c.args[0] for c in ws.send_text.call_args_list]
        assert chunk_fresh in sent
        assert chunk_resume in sent
        assert sent.count(_DONE_JSON) == 2

    # ── 4. Invalid input sends ERROR chunk, connection survives ───────────

    async def test_invalid_input_sends_error_and_continues(self):
        bad_request = {"invoke_type": "fresh", "messages": []}  # empty messages
        good_chunk = _chunk_json(StreamEvent.MESSAGE, {"text": "ok"})

        ws = _make_websocket([bad_request, _FRESH_REQUEST, WebSocketDisconnect()])
        service = _make_service([[good_chunk]])

        await websocket_graph(websocket=ws, service=service, user={})

        sent = [c.args[0] for c in ws.send_text.call_args_list]
        first = json.loads(sent[0])
        assert first["event"] == StreamEvent.ERROR
        assert "reason" in first.get("data", {})
        # Connection survived; served the valid second request
        assert good_chunk in sent

    # ── 5. Invalid resume (missing thread_id) sends ERROR ─────────────────

    async def test_resume_without_thread_id_sends_error(self):
        bad_resume = {
            "invoke_type": "resume",
            "tool_result": [{"role": "tool", "content": [{"type": "text", "text": "x"}]}],
            # config omitted → no thread_id
        }
        ws = _make_websocket([bad_resume, WebSocketDisconnect()])
        service = _make_service([])

        await websocket_graph(websocket=ws, service=service, user={})

        sent = [c.args[0] for c in ws.send_text.call_args_list]
        assert any(json.loads(t).get("event") == StreamEvent.ERROR for t in sent)
        # stream_graph never called
        assert len(service._stream_calls) == 0

    # ── 6. Immediate disconnect ───────────────────────────────────────────

    async def test_immediate_disconnect_no_error(self):
        ws = _make_websocket([WebSocketDisconnect()])
        service = _make_service([])

        await websocket_graph(websocket=ws, service=service, user={})

        ws.accept.assert_called_once()
        ws.send_text.assert_not_called()

    # ── 7. Unexpected server error closes with 1011 ───────────────────────

    async def test_unexpected_error_closes_1011(self):
        ws = _make_websocket([Exception("boom")])
        service = _make_service([])

        await websocket_graph(websocket=ws, service=service, user={})

        ws.close.assert_called_once_with(code=1011)

    # ── 8. Empty stream still sends done ─────────────────────────────────

    async def test_empty_stream_sends_done(self):
        ws = _make_websocket([_FRESH_REQUEST, WebSocketDisconnect()])
        service = _make_service([[]])

        await websocket_graph(websocket=ws, service=service, user={})

        sent = [c.args[0] for c in ws.send_text.call_args_list]
        assert sent == [_DONE_JSON]

    # ── 9. Done signal format ─────────────────────────────────────────────

    async def test_done_signal_correct_format(self):
        ws = _make_websocket([_FRESH_REQUEST, WebSocketDisconnect()])
        service = _make_service([[]])

        await websocket_graph(websocket=ws, service=service, user={})

        sent = [c.args[0] for c in ws.send_text.call_args_list]
        done = json.loads(_DONE_JSON)
        assert any(
            json.loads(t).get("event") == done["event"]
            and json.loads(t).get("data", {}).get("status") == "done"
            for t in sent
        )

    # ── 10. Live agent rejected on the turn-based socket ──────────────────

    async def test_live_agent_rejected_with_1008(self):
        """A live (realtime) graph must be sent to /v1/graph/live, not /v1/graph/ws."""
        ws = _make_websocket([_FRESH_REQUEST, WebSocketDisconnect()])
        service = _make_service([[]])
        service.is_live_agent = True

        await websocket_graph(websocket=ws, service=service, user={})

        # Rejected up front: error sent, socket closed 1008, stream never invoked.
        ws.close.assert_called_once_with(code=1008)
        assert len(service._stream_calls) == 0
        sent = [c.args[0] for c in ws.send_text.call_args_list]
        assert len(sent) == 1
        payload = json.loads(sent[0])
        assert payload["event"] == StreamEvent.ERROR
        assert "/v1/graph/live" in payload["data"]["reason"]

    # ── 11. invoke_type logged ────────────────────────────────────────────

    async def test_fresh_and_resume_both_reach_stream_graph(self):
        """Both fresh and resume go through stream_graph identically."""
        ws = _make_websocket([_FRESH_REQUEST, _RESUME_REQUEST, WebSocketDisconnect()])
        service = _make_service([[], []])

        await websocket_graph(websocket=ws, service=service, user={})

        assert len(service._stream_calls) == 2
        # fresh run uses messages, resume run uses tool_result
        assert service._stream_calls[0].messages[0].role == "user"
        assert service._stream_calls[1].messages[0].role == "tool"


@pytest.mark.asyncio
class TestWebSocketFrameLimits:
    """Frames over MAX_REQUEST_SIZE are refused before they are parsed."""

    async def test_oversized_frame_closes_with_1009(self, monkeypatch):
        import importlib

        mod = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")
        monkeypatch.setattr(mod, "_ws_max_frame_chars", lambda: 100)
        ws = _make_websocket([])
        ws.receive_text = AsyncMock(side_effect=["x" * 101, WebSocketDisconnect()])
        loads = MagicMock(side_effect=AssertionError("must not parse"))
        monkeypatch.setattr(mod.json, "loads", loads)
        service = _make_service([])

        await websocket_graph(websocket=ws, service=service, user={})

        ws.close.assert_awaited_with(code=1009)
        assert service._stream_calls == []
        assert "exceeds the 100 limit" in ws.send_text.call_args_list[0].args[0]

    async def test_frame_at_the_limit_is_accepted(self, monkeypatch):
        import importlib

        mod = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")
        frame = json.dumps(_FRESH_REQUEST)
        monkeypatch.setattr(mod, "_ws_max_frame_chars", lambda: len(frame))
        ws = _make_websocket([])
        ws.receive_text = AsyncMock(side_effect=[frame, WebSocketDisconnect()])
        service = _make_service([[]])

        await websocket_graph(websocket=ws, service=service, user={})

        assert len(service._stream_calls) == 1

    async def test_non_json_frame_sends_error_and_keeps_the_socket(self):
        ws = _make_websocket([])
        ws.receive_text = AsyncMock(
            side_effect=["{not json", json.dumps(_FRESH_REQUEST), WebSocketDisconnect()]
        )
        service = _make_service([[]])

        await websocket_graph(websocket=ws, service=service, user={})

        first = ws.send_text.call_args_list[0].args[0]
        assert "not valid JSON" in first
        assert len(service._stream_calls) == 1

    async def test_default_cap_is_max_request_size(self):
        import importlib

        from tenxgraph_api.src.app.core.config.settings import get_settings

        mod = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")
        assert mod._ws_max_frame_chars() == get_settings().MAX_REQUEST_SIZE
