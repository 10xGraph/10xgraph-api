"""Unit tests for the /v1/graph/live realtime WebSocket endpoint.

Mirrors test_websocket_graph.py: the GraphService is mocked (so no live provider),
and a mock WebSocket drives the binary/JSON frame protocol.
"""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from tenxgraph_api.src.app.routers.graph.router import realtime_graph_ws


def _router_module():
    # ``routers.graph.router`` the attribute is the APIRouter; patch the module itself.
    return sys.modules[realtime_graph_ws.__module__]


@pytest.fixture(autouse=True)
def _credential_valid(monkeypatch):
    """The mock sockets carry no credential: treat it as valid unless a test says otherwise."""
    graph_router = _router_module()

    monkeypatch.setattr(graph_router, "ws_identity_still_valid", lambda ws, user: True)
    monkeypatch.setattr(graph_router, "_ws_token_revoked", AsyncMock(return_value=False))


def _audio_event(data: bytes):
    return SimpleNamespace(type="audio_delta", data=data, model_dump=lambda mode=None: {})


def _json_event(type_: str, **fields):
    payload = {"type": type_, **fields}
    return SimpleNamespace(type=type_, model_dump=lambda mode=None: payload, **fields)


def _make_websocket(receive_side_effects: list, init: dict):
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock(return_value=json.dumps(init))
    ws.receive = AsyncMock(side_effect=receive_side_effects)
    ws.send_bytes = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _make_service(events: list):
    service = MagicMock()
    service.is_live_agent = True  # live graph: allowed on /v1/graph/live
    captured = {}

    async def _gen(input_queue, init, user):
        captured["queue"] = input_queue
        captured["init"] = init
        captured["user"] = user
        for e in events:
            yield e

    service.realtime_graph = _gen
    service._captured = captured
    return service


class TestRealtimeWebSocket:
    @pytest.mark.asyncio
    async def test_audio_events_sent_as_binary_others_as_json(self):
        events = [_audio_event(b"\x01\x02"), _json_event("turn_complete")]
        ws = _make_websocket([WebSocketDisconnect()], init={"model": "gemini-2.5-flash-live"})
        service = _make_service(events)

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        ws.accept.assert_awaited()
        ws.send_bytes.assert_awaited_once_with(b"\x01\x02")
        sent_text = [c.args[0] for c in ws.send_text.call_args_list]
        assert any(json.loads(t)["type"] == "turn_complete" for t in sent_text)

    @pytest.mark.asyncio
    async def test_upstream_binary_frame_becomes_audio_input(self):
        ws = _make_websocket(
            [
                {"type": "websocket.receive", "bytes": b"\xaa\xbb"},
                {"type": "websocket.receive", "text": json.dumps({"type": "close"})},
            ],
            init={"model": "gemini-2.5-flash-live"},
        )
        service = _make_service([])  # downstream finishes immediately

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        q = service._captured["queue"]
        item = q.get_nowait()
        assert item.kind == "audio"
        assert item.data == b"\xaa\xbb"

    @pytest.mark.asyncio
    async def test_oversized_binary_frame_dropped(self):
        from tenxgraph_api.src.app.routers.graph.router import REALTIME_MAX_FRAME_BYTES

        big = b"\x00" * (REALTIME_MAX_FRAME_BYTES + 1)
        ws = _make_websocket(
            [
                {"type": "websocket.receive", "bytes": big},
                {"type": "websocket.receive", "bytes": b"\x01\x02"},
                {"type": "websocket.receive", "text": json.dumps({"type": "close"})},
            ],
            init={"model": "gemini-2.5-flash-live"},
        )
        service = _make_service([])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        q = service._captured["queue"]
        kinds = []
        try:
            while True:
                kinds.append(q.get_nowait())
        except Exception:
            pass
        # Only the small frame is enqueued; the oversized one is dropped.
        audio = [i for i in kinds if i.kind == "audio"]
        assert len(audio) == 1
        assert audio[0].data == b"\x01\x02"

    @pytest.mark.asyncio
    async def test_upstream_text_control_frames_dispatch(self):
        ws = _make_websocket(
            [
                {"type": "websocket.receive", "text": json.dumps({"type": "activity_start"})},
                {"type": "websocket.receive", "text": json.dumps({"type": "text", "text": "hi"})},
                {"type": "websocket.receive", "text": json.dumps({"type": "close"})},
            ],
            init={"model": "gemini-2.5-flash-live"},
        )
        service = _make_service([])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        q = service._captured["queue"]
        kinds = []
        try:
            while True:
                kinds.append(q.get_nowait().kind)
        except Exception:
            pass
        assert "activity_start" in kinds
        assert "text" in kinds

    @pytest.mark.asyncio
    async def test_downstream_error_closes_with_1011(self):
        ws = _make_websocket([WebSocketDisconnect()], init={"model": "gemini-2.5-flash-live"})
        service = MagicMock()

        async def _boom(input_queue, init, user):
            raise RuntimeError("graph has no LiveAgent")
            yield  # make it an async generator

        service.realtime_graph = _boom

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        close_codes = [c.kwargs.get("code") for c in ws.close.call_args_list]
        assert 1011 in close_codes

    @pytest.mark.asyncio
    async def test_non_dict_init_frame_rejected(self):
        ws = _make_websocket([WebSocketDisconnect()], init=["not", "a", "dict"])
        service = _make_service([])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        close_codes = [c.kwargs.get("code") for c in ws.close.call_args_list]
        assert 1003 in close_codes
        # service must never be reached with a malformed init
        assert "init" not in service._captured

    @pytest.mark.asyncio
    async def test_non_live_agent_rejected_with_1008(self):
        """A turn-based graph must be sent to /v1/graph/ws, not /v1/graph/live."""
        ws = _make_websocket([WebSocketDisconnect()], init={"model": "gemini-2.5-flash-live"})
        service = _make_service([])
        service.is_live_agent = False

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        # Rejected before the init frame is read; closed 1008 with a fatal error event.
        close_codes = [c.kwargs.get("code") for c in ws.close.call_args_list]
        assert 1008 in close_codes
        ws.receive_text.assert_not_called()
        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        assert any(
            e.get("type") == "error" and e.get("code") == "not_live" and e.get("fatal") is True
            for e in sent
        )
        assert "/v1/graph/ws" in sent[0]["message"]

    @pytest.mark.asyncio
    async def test_init_frame_passed_to_service(self):
        init = {"model": "gemini-2.5-flash-live", "thread_id": "t-99", "voice": "Puck"}
        ws = _make_websocket([WebSocketDisconnect()], init=init)
        service = _make_service([])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        assert service._captured["init"]["thread_id"] == "t-99"
        assert service._captured["user"] == {"user_id": "u1"}

    @pytest.mark.asyncio
    async def test_close_frame_does_not_truncate_final_events(self):
        """A `close` control frame ends input but must not cut off the model's final
        response: downstream is drained, not cancelled, when the client side finishes first.
        """
        events = [
            _audio_event(b"a"),
            _json_event("output_transcript", text="bye", finished=True),
            _json_event("turn_complete"),
        ]

        async def _slow_gen(input_queue, init, user):
            # Still producing after the client closed input (model finishing its turn).
            for e in events:
                await asyncio.sleep(0.01)
                yield e

        service = MagicMock()
        service.realtime_graph = _slow_gen

        ws = _make_websocket(
            [{"type": "websocket.receive", "text": json.dumps({"type": "close"})}],
            init={"model": "gemini-2.5-flash-live"},
        )

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        # All three trailing events must still reach the client.
        assert ws.send_bytes.await_count == 1
        sent_types = [json.loads(c.args[0])["type"] for c in ws.send_text.call_args_list]
        assert "output_transcript" in sent_types
        assert "turn_complete" in sent_types

    @pytest.mark.asyncio
    async def test_invalid_modalities_sends_error_event_not_opaque_close(self):
        """A bad session config surfaces as a normalized fatal error frame, not a bare 1011."""

        async def _boom(input_queue, init, user):
            raise ValueError("response_modalities must contain exactly one modality")
            yield  # make it an async generator

        service = MagicMock()
        service.realtime_graph = _boom

        ws = _make_websocket([WebSocketDisconnect()], init={"model": "gemini-x"})

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        errors = [m for m in sent if m.get("type") == "error"]
        assert errors and errors[0]["fatal"] is True
        assert errors[0]["code"] == "invalid_config"


class TestIsLiveAgent:
    """GraphService.is_live_agent resolves live detection across core versions."""

    def _service(self, graph):
        from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

        return GraphService(graph=graph, checkpointer=AsyncMock(), config=MagicMock())

    def test_prefers_public_is_realtime(self):
        graph = MagicMock()
        graph.is_realtime = MagicMock(return_value=True)
        assert self._service(graph).is_live_agent is True
        graph.is_realtime.return_value = False
        assert self._service(graph).is_live_agent is False

    def test_falls_back_to_find_live_nodes(self):
        graph = MagicMock(spec=["_find_live_nodes"])
        graph._find_live_nodes = MagicMock(return_value=[("audio", object())])
        assert self._service(graph).is_live_agent is True
        graph._find_live_nodes.return_value = []
        assert self._service(graph).is_live_agent is False

    def test_defaults_false_when_no_detection_api(self):
        graph = MagicMock(spec=[])
        assert self._service(graph).is_live_agent is False


def _checkpointer():
    checkpointer = AsyncMock()
    checkpointer.aget_thread_owner = AsyncMock(return_value=None)
    return checkpointer


def _config(**websocket):
    from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig

    config = MagicMock()
    config.websocket = WebSocketConfig.from_dict({"realtime_models": ["gemini-x"], **websocket})
    return config


async def _realtime_config(init, **websocket):
    """Run a realtime session and return the ``realtime`` overrides the graph received."""
    from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

    captured = {}

    async def _arealtime(input_queue, config):
        captured["config"] = config
        for _ in ():
            yield

    graph = MagicMock()
    graph.arealtime = _arealtime
    svc = GraphService(graph=graph, checkpointer=_checkpointer(), config=_config(**websocket))
    async for _ in svc.realtime_graph(MagicMock(), init, {"user_id": "u1"}):
        pass
    return captured["config"].get("realtime", {})


class TestRealtimeGraphService:
    @pytest.mark.asyncio
    async def test_init_session_params_mapped_into_realtime_config(self):
        from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

        captured = {}

        async def _arealtime(input_queue, config):
            captured["config"] = config
            for _ in ():
                yield

        graph = MagicMock()
        graph.arealtime = _arealtime
        overrides = ["voice", "modalities", "vad", "system_prompt"]
        svc = GraphService(
            graph=graph,
            checkpointer=_checkpointer(),
            config=_config(realtime_client_overrides=overrides),
        )

        init = {
            "model": "gemini-x",
            "voice": "Puck",
            "modalities": ["TEXT"],
            "vad": {"enabled": False},
            "system_prompt": "be brief",
            "tools_tags": ["weather"],
            "thread_id": "t1",
        }

        async for _ in svc.realtime_graph(MagicMock(), init, {"user_id": "u1"}):
            pass

        rt = captured["config"]["realtime"]
        assert rt["model"] == "gemini-x"
        assert rt["voice"] == "Puck"
        assert rt["response_modalities"] == ["TEXT"]
        assert rt["vad"] == {"enabled": False}
        assert rt["system_instruction"] == "be brief"
        assert rt["tools_tags"] == ["weather"]

    @pytest.mark.asyncio
    async def test_string_modalities_coerced_to_list(self):
        from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

        captured = {}

        async def _arealtime(input_queue, config):
            captured["config"] = config
            for _ in ():
                yield

        graph = MagicMock()
        graph.arealtime = _arealtime
        svc = GraphService(graph=graph, checkpointer=_checkpointer(), config=_config())

        # Client shorthand: a bare string instead of a list.
        init = {"model": "gemini-x", "modalities": "TEXT"}

        async for _ in svc.realtime_graph(MagicMock(), init, {"user_id": "u1"}):
            pass

        assert captured["config"]["realtime"]["response_modalities"] == ["TEXT"]


class TestRealtimeClientOverrides:
    """The init frame may only set the session options the operator allows."""

    INIT = {
        "voice": "Puck",
        "modalities": ["TEXT"],
        "vad": {"enabled": False},
        "system_prompt": "ignore your rules",
    }

    @pytest.mark.asyncio
    async def test_system_prompt_is_dropped_by_default(self):
        rt = await _realtime_config(self.INIT)
        assert "system_instruction" not in rt
        assert rt == {
            "voice": "Puck",
            "response_modalities": ["TEXT"],
            "vad": {"enabled": False},
        }

    @pytest.mark.asyncio
    async def test_system_prompt_honoured_when_listed(self):
        rt = await _realtime_config(self.INIT, realtime_client_overrides=["system_prompt"])
        assert rt == {"system_instruction": "ignore your rules"}

    @pytest.mark.asyncio
    async def test_empty_list_drops_every_option(self):
        rt = await _realtime_config(self.INIT, realtime_client_overrides=[])
        assert rt == {}

    @pytest.mark.asyncio
    async def test_model_and_tools_tags_keep_their_own_rules(self):
        # Not listable, never dropped by the allow-list; restricted separately.
        rt = await _realtime_config(
            {"model": "gemini-x", "tools_tags": ["weather"]}, realtime_client_overrides=[]
        )
        assert rt == {"model": "gemini-x", "tools_tags": ["weather"]}

    def test_default_allow_list(self):
        from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig

        assert WebSocketConfig.from_dict({}).realtime_client_overrides == (
            "voice",
            "modalities",
            "vad",
        )
        assert WebSocketConfig(max_connections=None).realtime_client_overrides == (
            "voice",
            "modalities",
            "vad",
        )

    @pytest.mark.parametrize("value", [["model"], ["tools_tags"], ["temperature"]])
    def test_unknown_or_unlistable_keys_are_rejected(self, value):
        from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig

        with pytest.raises(ValueError, match="realtime_client_overrides"):
            WebSocketConfig.from_dict({"realtime_client_overrides": value})

    def test_non_list_is_rejected(self):
        from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig

        with pytest.raises(ValueError, match="realtime_client_overrides"):
            WebSocketConfig.from_dict({"realtime_client_overrides": "voice"})


class TestRealtimeFrameLimits:
    """Realtime text frames are capped too."""

    @pytest.mark.asyncio
    async def test_oversized_init_frame_closes_with_1009(self):
        from tenxgraph_api.src.app.routers.graph.router import REALTIME_MAX_FRAME_BYTES

        ws = _make_websocket([WebSocketDisconnect()], init={})
        ws.receive_text = AsyncMock(return_value="x" * (REALTIME_MAX_FRAME_BYTES + 1))
        service = _make_service([])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        assert 1009 in [c.kwargs.get("code") for c in ws.close.call_args_list]
        assert "init" not in service._captured

    def test_control_frames_dispatch_and_oversized_ones_are_dropped(self):
        from tenxgraph_api.src.app.routers.graph.router import (
            REALTIME_MAX_FRAME_BYTES,
            _dispatch_realtime_control,
        )

        queue = MagicMock()
        big = json.dumps({"type": "text", "text": "x" * REALTIME_MAX_FRAME_BYTES})
        assert _dispatch_realtime_control(queue, big) is None
        queue.send_text.assert_not_called()

        assert _dispatch_realtime_control(queue, json.dumps({"type": "text", "text": "hi"})) is None
        queue.send_text.assert_called_once_with("hi")
        assert _dispatch_realtime_control(queue, '{"type": "close"}') == "close"
        assert _dispatch_realtime_control(queue, "not json") is None
        assert _dispatch_realtime_control(queue, '["a list"]') is None


class TestRealtimeCredentialRecheck:
    """A live session ends when its token expires or is revoked, not only at the handshake."""

    @staticmethod
    def _slow_service():
        service = _make_service([])

        async def _gen(input_queue, init, user):
            await asyncio.sleep(30)  # a session that would run on
            yield

        service.realtime_graph = _gen
        return service

    @staticmethod
    def _closes(ws):
        return [c for c in ws.close.call_args_list if c.kwargs.get("code") == 1008]

    @pytest.mark.asyncio
    async def test_revoked_mid_session_closes_with_1008(self, monkeypatch):
        graph_router = _router_module()

        monkeypatch.setattr(graph_router, "REALTIME_RECHECK_INTERVAL", 0.01)
        revoked = iter([False, True])  # valid at start, revoked at the first re-check
        monkeypatch.setattr(
            graph_router, "_ws_token_revoked", AsyncMock(side_effect=lambda u: next(revoked, True))
        )

        async def _hang(*a, **k):
            await asyncio.sleep(30)

        ws = _make_websocket([], init={"model": "m"})
        ws.receive = AsyncMock(side_effect=_hang)
        await asyncio.wait_for(
            realtime_graph_ws(websocket=ws, service=self._slow_service(), user={"user_id": "u1"}),
            timeout=5,
        )

        assert self._closes(ws)
        sent = [json.loads(c.args[0]) for c in ws.send_text.call_args_list]
        assert any(e.get("code") == "session_expired" for e in sent)

    @pytest.mark.asyncio
    async def test_expired_token_mid_session_closes_with_1008(self, monkeypatch):
        graph_router = _router_module()

        monkeypatch.setattr(graph_router, "REALTIME_RECHECK_INTERVAL", 0.01)
        valid = iter([True])  # the token verifies at start, then stops verifying
        monkeypatch.setattr(
            graph_router, "ws_identity_still_valid", lambda ws, user: next(valid, False)
        )

        async def _hang(*a, **k):
            await asyncio.sleep(30)

        ws = _make_websocket([], init={"model": "m"})
        ws.receive = AsyncMock(side_effect=_hang)
        await asyncio.wait_for(
            realtime_graph_ws(websocket=ws, service=self._slow_service(), user={"user_id": "u1"}),
            timeout=5,
        )

        assert self._closes(ws)

    @pytest.mark.asyncio
    async def test_revoked_before_session_start_is_refused(self, monkeypatch):
        graph_router = _router_module()

        monkeypatch.setattr(graph_router, "_ws_token_revoked", AsyncMock(return_value=True))
        ws = _make_websocket([WebSocketDisconnect()], init={"model": "m"})
        service = _make_service([])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        assert self._closes(ws)
        assert "queue" not in service._captured  # the model session never started

    @pytest.mark.asyncio
    async def test_normal_session_is_unaffected(self, monkeypatch):
        graph_router = _router_module()

        monkeypatch.setattr(graph_router, "REALTIME_RECHECK_INTERVAL", 0.01)
        ws = _make_websocket([WebSocketDisconnect()], init={"model": "m"})
        service = _make_service([_audio_event(b"\x01")])

        await realtime_graph_ws(websocket=ws, service=service, user={"user_id": "u1"})

        ws.send_bytes.assert_awaited_once_with(b"\x01")
        assert not self._closes(ws)


class TestRealtimeSessionLimits:
    def test_live_route_is_behind_the_per_user_connection_guard(self):
        """Live sessions share ``websocket.max_connections_per_user`` with ``/v1/graph/ws``."""
        from tenxgraph_api.src.app.routers.graph.realtime_guard import realtime_connection_guard
        from tenxgraph_api.src.app.routers.graph.router import router

        route = next(r for r in router.routes if getattr(r, "path", "") == "/v1/graph/live")
        calls = [d.call for d in route.dependant.dependencies]
        assert realtime_connection_guard in calls

    def test_byte_budget_drops_frames_over_it(self):
        from tenxgraph_api.src.app.routers.graph.router import _ByteBoundedLiveInputQueue

        q = _ByteBoundedLiveInputQueue(maxsize=1000, max_bytes=100)
        q.send_audio(b"a" * 60)
        q.send_audio(b"b" * 60)  # 120 > 100: dropped
        q.send_audio(b"c" * 40)  # exactly 100: kept
        first, second = q.get_nowait(), q.get_nowait()
        assert (first.data[:1], second.data[:1]) == (b"a", b"c")
        assert q._queue.empty()

    @pytest.mark.asyncio
    async def test_consumed_bytes_free_the_budget(self):
        from tenxgraph_api.src.app.routers.graph.router import _ByteBoundedLiveInputQueue

        q = _ByteBoundedLiveInputQueue(maxsize=1000, max_bytes=100)
        q.send_audio(b"a" * 100)
        assert (await q.get()).data == b"a" * 100
        q.send_audio(b"b" * 100)  # room again
        q.send_text("hi")  # 100 buffered: dropped
        q.close()
        got = [i async for i in q]
        assert [i.kind for i in got] == ["audio"]
        q.send_audio(b"c")  # closed queue drops and does not leak budget
        assert q._buffered == 0
