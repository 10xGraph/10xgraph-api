"""NC-23: user B cannot read or resolve user A's ``graph://media/<key>`` through any path.

The core resolves ``graph://media/<key>`` for any key (LLM-MED-04); the API's ownership check
is what protects it. This drives every route that reads a stored file or lets a message carry
a media reference, with auth on and the ownership check on, through the real routers and a
real ``MediaService``:

- the media routes (download, info, signed URL, delete, list);
- invoke, stream, the ``/v1/graph/ws`` WebSocket and AG-UI, all of which reach one shared
  ``GraphService._prepare_input`` -> ``preprocess_multimodal_messages``;
- the checkpointer routes that write messages or state (the next run resolves them);
- the preprocessor and ``MediaService`` called directly;
- ``/v1/graph/live``, which accepts no message or media reference at all.

The graph is real: a node resolves every ``graph://media`` reference with the core
``resolve_media_refs`` and echoes the bytes, so "B cannot resolve A's key" is checked on what
the run actually produced, not only on a status code.
"""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from injectq import InjectQ
from tenxgraph.core.graph import CompiledGraph, StateGraph
from tenxgraph.core.state import AgentState, Message, TextBlock
from tenxgraph.core.state.message_block import (
    DocumentBlock,
    ImageBlock,
    MediaRef,
    ToolResultBlock,
)
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.media.resolver import MediaRefResolver
from tenxgraph.utils.constants import END
from tenxgraph.utils.converter import resolve_media_refs

from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    DefaultAuthorizationBackend,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.request_config import ClientInputError
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    check_media_references,
    internal_media_keys,
    preprocess_multimodal_messages,
)
from tenxgraph_api.src.app.routers.media import MediaService
from tenxgraph_api.src.app.routers.media.router import router as media_router
from tests.integration_tests.conftest import HeaderAuth, build_app, make_client, user_headers


SECRET_A = b"secret-of-alice-4f1c"
SECRET_B = b"secret-of-bob-77aa"
ALICE, BOB = "alice", "bob"
LEAK = SECRET_A.decode()
# POST /v1/threads/{id}/messages is admin-only (checkpointer:import).
IMPORTER = {**user_headers(BOB), "X-Test-Scopes": "checkpointer:import"}


# --------------------------------------------------------------------------- the app


class _Auth(HeaderAuth):
    """``X-Test-User`` for HTTP; a bearer token (the user id) also works, which the
    WebSocket pre-run credential check needs."""

    def authenticate(self, request, response, credential):  # type: ignore[override]
        if credential is not None and not request.headers.get("X-Test-User"):
            return {"user_id": credential.credentials}
        return super().authenticate(request, response, credential)


class _Harness:
    """One app with every route that can carry or resolve a media reference."""

    def __init__(self, tmp_path: Path, with_ag_ui: bool) -> None:
        self.resolved: list[tuple[str, str]] = []  # (user_id, key) of every resolution
        settings = MediaSettings(
            MEDIA_STORAGE_TYPE=MediaStorageType.MEMORY, MEDIA_REQUIRE_OWNER=True
        )  # type: ignore[call-arg]
        self.media = MediaService(settings=settings)
        self.checkpointer = InMemoryCheckpointer()

        config_file = tmp_path / "10xgraph.json"
        config_file.write_text(
            json.dumps(
                {
                    "agent": "x:y",
                    "auth": {"method": "custom", "path": "x:y"},
                    "ag_ui": {"enabled": with_ag_ui},
                    "runs": {"thread_ids": "client"},
                }
            )
        )
        self.config = GraphConfig(str(config_file))

        graph = StateGraph(AgentState())
        graph.add_node("MAIN", self._resolve_node)
        graph.set_entry_point("MAIN")
        graph.add_edge("MAIN", END)
        self.graph = graph.compile(checkpointer=self.checkpointer)

        # GraphService, CheckpointerService and the WebSocket handshake look services up in the
        # active container (as in production, where the loader fills it): bind them there too.
        active = InjectQ.get_instance()
        for kind, value in (
            (MediaService, self.media),
            (BaseAuth, _Auth()),
            (AuthorizationBackend, DefaultAuthorizationBackend()),
            (GraphConfig, self.config),
            (BaseCheckpointer, self.checkpointer),
            (CompiledGraph, self.graph),
        ):
            active.bind_instance(kind, value, allow_none=True)

        routers = [media_router, graph_router, checkpointer_router]
        if with_ag_ui:
            from tenxgraph_api.src.app.routers.ag_ui import router as ag_ui_router

            routers.append(ag_ui_router)
        self.app = build_app(
            routers=routers,
            checkpointer=self.checkpointer,
            extra_bindings={
                MediaService: self.media,
                CompiledGraph: self.graph,
                GraphConfig: self.config,
                BaseCheckpointer: self.checkpointer,
            },
        )
        self.client: TestClient = make_client(self.app)

    async def _resolve_node(self, state: AgentState, config: dict[str, Any]) -> Message:
        """Resolve every internal media reference of the context, as the LLM call would."""
        user = config.get("user_id", "?")
        context = [m.model_copy(deep=True) for m in state.context]
        for message in context:
            for key in internal_media_keys(message):
                self.resolved.append((user, key))
        resolver = MediaRefResolver(media_store=self.media.store)
        resolved = await resolve_media_refs(context, resolver)
        found: list[str] = []
        for message in resolved:
            for block in message.content:
                media = getattr(block, "media", None)
                if media is not None and media.kind == "data" and media.data_base64:
                    found.append(base64.b64decode(media.data_base64).decode())
        return Message(role="assistant", content=[TextBlock(text="resolved:" + "|".join(found))])

    def upload(self, user: str, data: bytes) -> str:
        resp = self.client.post(
            "/v1/files/upload",
            files={"file": ("note.txt", data, "text/plain")},
            headers=user_headers(user),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["file_id"]


@pytest.fixture
def h(tmp_path):
    pytest.importorskip("ag_ui")
    with InjectQ.test_mode():
        yield _Harness(tmp_path, with_ag_ui=True)


@pytest.fixture
def keys(h):
    """Alice's file, Bob's own file."""
    return h.upload(ALICE, SECRET_A), h.upload(BOB, SECRET_B)


def _image(url: str | None = None, *, file_id: str | None = None) -> dict:
    media: dict[str, Any] = {"kind": "file_id", "file_id": file_id} if file_id else {"kind": "url"}
    if url:
        media["url"] = url
    return {"role": "user", "content": [{"type": "image", "media": media}]}


def _victim_variants(key: str) -> dict[str, dict]:
    """Every way a client can point a message at Alice's file."""
    return {
        "graph url": _image(f"graph://media/{key}"),
        "legacy url": _image(f"agentflow://media/{key}"),
        "file_id": _image(file_id=key),
        "file_id with a url": _image(f"graph://media/{key}", file_id="not-mine"),
        "document file_id": {
            "role": "user",
            "content": [{"type": "document", "media": {"kind": "file_id", "file_id": key}}],
        },
        "nested in a tool result": {
            "role": "tool",
            "content": [
                {
                    "type": "tool_result",
                    "call_id": "c1",
                    "output": [
                        {"type": "image", "media": {"kind": "url", "url": f"graph://media/{key}"}}
                    ],
                }
            ],
        },
    }


def _never_alice(h: _Harness, key: str, who: str) -> None:
    """The run did not resolve Alice's key for ``who``."""
    assert (who, key) not in h.resolved, f"{who} resolved {key}"


# --------------------------------------------------------------------------- media routes


def test_media_routes_refuse_another_users_file(h, keys):
    alice_key, bob_key = keys
    bob = user_headers(BOB)

    for path in (
        f"/v1/files/{alice_key}",
        f"/v1/files/{alice_key}/info",
        f"/v1/files/{alice_key}/url",
    ):
        resp = h.client.get(path, headers=bob)
        assert resp.status_code == 404, f"{path} -> {resp.status_code}"
        assert LEAK not in resp.text

    # B cannot delete it, and cannot tell it from a missing file.
    assert h.client.delete(f"/v1/files/{alice_key}", headers=bob).status_code == 404
    missing = h.client.get("/v1/files/does-not-exist", headers=bob)
    assert missing.status_code == 404

    # B's listing holds B's file only.
    listed = h.client.get("/v1/files", headers=bob)
    assert listed.status_code == 200
    ids = [f["file_id"] for f in listed.json()["data"]["files"]]
    assert ids == [bob_key]

    # Alice still has her file (the delete did nothing) and can read it.
    own = h.client.get(f"/v1/files/{alice_key}", headers=user_headers(ALICE))
    assert own.status_code == 200 and own.content == SECRET_A


@pytest.mark.asyncio
async def test_media_service_denies_another_user_on_every_resolve_path(h, keys):
    alice_key, _ = keys
    service = h.media
    with pytest.raises(PermissionError):
        await service.ensure_can_access(alice_key, BOB)
    with pytest.raises(PermissionError):
        await service.get_file(alice_key, BOB)
    with pytest.raises(PermissionError):
        await service.get_file_info(alice_key, BOB)
    with pytest.raises(KeyError):
        await service.ensure_can_access("nope", BOB)
    # The owner passes.
    await service.ensure_can_access(alice_key, ALICE)
    assert (await service.get_file(alice_key, ALICE))[0] == SECRET_A


@pytest.mark.asyncio
async def test_a_file_with_no_owner_is_denied_when_owners_are_required(h):
    key = await h.media.store.store(b"legacy", "text/plain", metadata={})
    with pytest.raises(PermissionError):
        await h.media.ensure_can_access(key, BOB)


# --------------------------------------------------------------------------- preprocessor


@pytest.mark.asyncio
async def test_preprocessor_rejects_every_variant(h, keys):
    alice_key, bob_key = keys
    for raw in _victim_variants(alice_key).values():
        message = Message.model_validate(raw)
        with pytest.raises(ClientInputError, match="not found"):
            await preprocess_multimodal_messages([message], h.media, BOB)
        with pytest.raises(ClientInputError, match="not found"):
            await check_media_references([message], h.media, BOB)
    # Missing and foreign give the same error text, so existence is not confirmed.
    for key in (alice_key, "missing-key"):
        with pytest.raises(ClientInputError) as exc:
            await preprocess_multimodal_messages(
                [Message.model_validate(_image(f"graph://media/{key}"))], h.media, BOB
            )
        assert str(exc.value).replace(key, "K") == "File not found: K"
    # Own file is allowed, and the owner can use hers.
    own = Message.model_validate(_image(f"graph://media/{bob_key}"))
    assert await preprocess_multimodal_messages([own], h.media, BOB)
    assert await preprocess_multimodal_messages(
        [Message.model_validate(_image(f"graph://media/{alice_key}"))], h.media, ALICE
    )


# --------------------------------------------------------------------------- invoke / stream


@pytest.mark.parametrize("variant", list(_victim_variants("K")))
def test_invoke_cannot_resolve_another_users_file(h, keys, variant):
    alice_key, _ = keys
    body = {"messages": [_victim_variants(alice_key)[variant]]}
    resp = h.client.post("/v1/graph/invoke", json=body, headers=user_headers(BOB))
    assert resp.status_code in (400, 422), f"{variant}: {resp.status_code} {resp.text[:200]}"
    assert LEAK not in resp.text
    _never_alice(h, alice_key, BOB)


@pytest.mark.parametrize("variant", list(_victim_variants("K")))
def test_stream_cannot_resolve_another_users_file(h, keys, variant):
    alice_key, _ = keys
    body = {"messages": [_victim_variants(alice_key)[variant]]}
    resp = h.client.post("/v1/graph/stream", json=body, headers=user_headers(BOB))
    assert LEAK not in resp.text
    assert "resolved:" not in resp.text
    _never_alice(h, alice_key, BOB)


def test_invoke_and_stream_resolve_the_callers_own_file(h, keys):
    """Control: the same request shape works for the owner, so the denials above are real."""
    alice_key, bob_key = keys
    ref = {"messages": [_image(f"graph://media/{bob_key}")]}
    invoked = h.client.post("/v1/graph/invoke", json=ref, headers=user_headers(BOB))
    assert invoked.status_code == 200, invoked.text
    assert SECRET_B.decode() in invoked.text
    streamed = h.client.post("/v1/graph/stream", json=ref, headers=user_headers(BOB))
    assert streamed.status_code == 200 and SECRET_B.decode() in streamed.text
    assert (BOB, bob_key) in h.resolved

    own = {"messages": [_image(f"graph://media/{alice_key}")]}
    assert LEAK in h.client.post("/v1/graph/invoke", json=own, headers=user_headers(ALICE)).text


def test_resume_tool_result_messages_are_checked_too(h, keys):
    """The tool_result of a resume reaches the graph through the same input path."""
    alice_key, _ = keys
    victim = _victim_variants(alice_key)["nested in a tool result"]
    body = {"messages": [], "resume": None, "tool_result": [victim]}
    resp = h.client.post("/v1/graph/invoke", json=body, headers=user_headers(BOB))
    assert LEAK not in resp.text
    _never_alice(h, alice_key, BOB)


# --------------------------------------------------------------------------- WebSocket


def _ws_run(h: _Harness, user: str, message: dict) -> list[str]:
    frames: list[str] = []
    headers = {**user_headers(user), "Authorization": f"Bearer {user}"}
    with h.client.websocket_connect("/v1/graph/ws", headers=headers) as ws:
        ws.send_json({"invoke_type": "fresh", "messages": [message]})
        for _ in range(60):
            frame = ws.receive_text()  # the run ends with an error frame or status "done"
            frames.append(frame)
            if '"event":"error"' in frame or '"status":"done"' in frame:
                break
    return frames


@pytest.mark.parametrize("variant", ["graph url", "legacy url", "file_id"])
def test_websocket_run_cannot_resolve_another_users_file(h, keys, variant):
    alice_key, _ = keys
    frames = _ws_run(h, BOB, _victim_variants(alice_key)[variant])
    assert LEAK not in "".join(frames)
    _never_alice(h, alice_key, BOB)


def test_websocket_run_resolves_the_callers_own_file(h, keys):
    _, bob_key = keys
    frames = _ws_run(h, BOB, _image(f"graph://media/{bob_key}"))
    assert SECRET_B.decode() in "".join(frames)


def test_live_endpoint_accepts_no_message_or_media_reference():
    """``/v1/graph/live`` takes audio bytes and plain text controls; its init frame cannot
    carry messages, state or a media reference, so nothing there resolves a stored file."""
    from tenxgraph_api.src.app.routers.graph.services.graph_service import REALTIME_INIT_FIELDS

    assert set(REALTIME_INIT_FIELDS) == {
        "model",
        "voice",
        "modalities",
        "vad",
        "system_prompt",
        "tools_tags",
    }
    assert not {"messages", "state", "context", "media", "file_id"} & set(REALTIME_INIT_FIELDS)


# --------------------------------------------------------------------------- AG-UI


def _ag_ui_body(thread_id: str, part: dict) -> dict:
    return {
        "threadId": thread_id,
        "runId": "r1",
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "messages": [{"id": "u1", "role": "user", "content": [part]}],
    }


@pytest.mark.parametrize("source_kind", ["url-graph", "url-legacy", "file"])
def test_ag_ui_cannot_resolve_another_users_file(h, keys, source_kind):
    alice_key, _ = keys
    source = {
        "url-graph": {"type": "url", "value": f"graph://media/{alice_key}"},
        "url-legacy": {"type": "url", "value": f"agentflow://media/{alice_key}"},
        "file": {"type": "file", "value": alice_key},
    }[source_kind]
    resp = h.client.post(
        "/v1/ag-ui",
        json=_ag_ui_body("t-bob-1", {"type": "image", "source": source}),
        headers=user_headers(BOB),
    )
    assert LEAK not in resp.text
    assert "resolved:" not in resp.text
    _never_alice(h, alice_key, BOB)


def test_ag_ui_resolves_the_callers_own_file(h, keys):
    _, bob_key = keys
    resp = h.client.post(
        "/v1/ag-ui",
        json=_ag_ui_body(
            "t-bob-2",
            {"type": "image", "source": {"type": "url", "value": f"graph://media/{bob_key}"}},
        ),
        headers=user_headers(BOB),
    )
    assert resp.status_code == 200
    assert SECRET_B.decode() in resp.text


# --------------------------------------------------------------------------- checkpointer


@pytest.mark.parametrize(
    "variant", ["graph url", "legacy url", "file_id", "document file_id", "nested in a tool result"]
)
def test_writing_messages_into_a_thread_is_checked(h, keys, variant):
    """A message written into a thread is resolved by the next run, so it is checked now."""
    alice_key, _ = keys
    message = {**_victim_variants(alice_key)[variant], "message_id": "m1"}
    resp = h.client.post(
        "/v1/threads/bob-thread/messages",
        json={"messages": [message]},
        headers=IMPORTER,
    )
    assert resp.status_code in (400, 422), f"{variant}: {resp.status_code} {resp.text[:200]}"
    assert LEAK not in resp.text


def test_writing_state_into_a_thread_is_checked(h, keys):
    alice_key, _ = keys
    message = {**_victim_variants(alice_key)["graph url"], "message_id": "m1"}
    resp = h.client.put(
        "/v1/threads/bob-thread/state",
        json={"state": {"context": [message]}, "config": {}},
        headers=user_headers(BOB),
    )
    assert resp.status_code in (400, 422), f"{resp.status_code} {resp.text[:200]}"
    assert LEAK not in resp.text


def test_writing_the_callers_own_file_into_a_thread_is_allowed(h, keys):
    _, bob_key = keys
    message = {**_image(f"graph://media/{bob_key}"), "message_id": "m1"}
    resp = h.client.post(
        "/v1/threads/bob-thread/messages",
        json={"messages": [message]},
        headers=IMPORTER,
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- no media service


@pytest.mark.asyncio
async def test_references_are_refused_when_ownership_cannot_be_checked():
    message = Message(
        role="user", content=[ImageBlock(media=MediaRef(kind="url", url="graph://media/x"))]
    )
    with pytest.raises(ClientInputError, match="cannot be verified"):
        await preprocess_multimodal_messages([message], None, BOB)
    doc = Message(
        role="user", content=[DocumentBlock(media=MediaRef(kind="url", url="agentflow://media/x"))]
    )
    with pytest.raises(ClientInputError, match="cannot be verified"):
        await preprocess_multimodal_messages([doc], None, BOB)
    tool = Message(
        role="tool",
        content=[
            ToolResultBlock(
                call_id="1", output=[{"type": "image", "media": {"url": "graph://media/x"}}]
            )
        ],
    )
    with pytest.raises(ClientInputError, match="cannot be verified"):
        await preprocess_multimodal_messages([tool], None, BOB)


@pytest.mark.parametrize("variant", ["file_id", "document file_id"])
def test_writing_state_with_a_foreign_file_id_is_refused(h, keys, variant):
    alice_key, _ = keys
    message = {**_victim_variants(alice_key)[variant], "message_id": "m1"}
    resp = h.client.put(
        "/v1/threads/bob-thread/state",
        json={"state": {"context": [message]}, "config": {}},
        headers=user_headers(BOB),
    )
    assert resp.status_code in (400, 422), resp.text[:200]


def test_writing_the_callers_own_file_id_is_allowed(h, keys):
    _, bob_key = keys
    message = {**_image(file_id=bob_key), "message_id": "m1"}
    resp = h.client.post(
        "/v1/threads/bob-thread/messages", json={"messages": [message]}, headers=IMPORTER
    )
    assert resp.status_code == 200, resp.text
