"""Cross-user (IDOR) negative tests for the fixes in the production-readiness audit.

Each test drives the REAL request path -- real ``RequirePermission``, real auth
backend, real service -- and asserts that user B cannot reach user A's data.
"""

from __future__ import annotations

import pytest
from tenxgraph.storage.checkpointer import InMemoryCheckpointer

from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.media import MediaService
from tenxgraph_api.src.app.routers.media.router import router as media_router

from .conftest import OwnershipAuthz, build_app, make_client, user_headers


HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_UNSUPPORTED_MEDIA = 415


# ---------------------------------------------------------------------------
# Media ownership (B1: files had no owner; any authenticated caller could read one)
# ---------------------------------------------------------------------------


@pytest.fixture
def media_app(memory_media_settings):
    service = MediaService(settings=memory_media_settings)
    app = build_app(
        routers=[media_router],
        extra_bindings={MediaService: service},
    )
    return app


def test_owner_can_read_own_file_but_other_user_cannot(media_app):
    client = make_client(media_app)

    upload = client.post(
        "/v1/files/upload",
        files={"file": ("a.txt", b"secret-A", "text/plain")},
        headers=user_headers("alice"),
    )
    assert upload.status_code == HTTP_OK, upload.text
    file_id = upload.json()["data"]["file_id"]

    # Owner reads it back.
    owner_get = client.get(f"/v1/files/{file_id}", headers=user_headers("alice"))
    assert owner_get.status_code == HTTP_OK
    assert owner_get.content == b"secret-A"

    # A different authenticated user must NOT be able to read, inspect, or get a URL
    # for it -- and must get 404 (not 403), so the id's existence is not confirmed.
    for path in (f"/v1/files/{file_id}", f"/v1/files/{file_id}/info", f"/v1/files/{file_id}/url"):
        resp = client.get(path, headers=user_headers("mallory"))
        assert resp.status_code == HTTP_NOT_FOUND, f"{path} -> {resp.status_code}"


def test_upload_rejects_disallowed_content_type(monkeypatch, memory_media_settings):
    # Restrict the allowlist to images; a text upload must be refused (H1 hardening).
    memory_media_settings.MEDIA_ALLOWED_CONTENT_TYPES = "image/*"
    monkeypatch.setattr(
        "tenxgraph_api.src.app.routers.media.router.get_media_settings",
        lambda: memory_media_settings,
    )
    service = MediaService(settings=memory_media_settings)
    app = build_app(routers=[media_router], extra_bindings={MediaService: service})
    client = make_client(app)

    resp = client.post(
        "/v1/files/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=user_headers("alice"),
    )
    assert resp.status_code == HTTP_UNSUPPORTED_MEDIA


def test_upload_over_size_cap_is_rejected(monkeypatch, memory_media_settings):
    # Tiny cap so the streaming size-guard trips without allocating anything large.
    memory_media_settings.MEDIA_MAX_SIZE_MB = 0.001  # ~1 KB
    monkeypatch.setattr(
        "tenxgraph_api.src.app.routers.media.router.get_media_settings",
        lambda: memory_media_settings,
    )
    service = MediaService(settings=memory_media_settings)
    app = build_app(routers=[media_router], extra_bindings={MediaService: service})
    client = make_client(app)

    resp = client.post(
        "/v1/files/upload",
        files={"file": ("big.txt", b"x" * 5000, "text/plain")},
        headers=user_headers("alice"),
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Authorization backend gets the resource_id (B1 #2) -- path AND body
# ---------------------------------------------------------------------------


def test_authz_blocks_other_user_on_owned_thread_path():
    authz = OwnershipAuthz()
    authz.own("thread-A", "alice")
    app = build_app(
        routers=[checkpointer_router],
        authz=authz,
        checkpointer=InMemoryCheckpointer(),
    )
    client = make_client(app)

    # Owner is allowed through (empty state is fine -- 200 proves authz passed).
    owner = client.get("/v1/threads/thread-A/state", headers=user_headers("alice"))
    assert owner.status_code == HTTP_OK

    # Another user is refused at the authorization layer.
    other = client.get("/v1/threads/thread-A/messages", headers=user_headers("mallory"))
    assert other.status_code == HTTP_FORBIDDEN
    # The authz backend actually received the thread id it needed to make the call.
    assert "thread-A" in [str(r) for r in authz.seen_resource_ids]


def test_authz_receives_thread_id_from_request_body():
    """Regression for B1 #2: invoke/stream/stop/fix carry thread_id in the BODY.

    Before the fix, ``resource_id`` came only from path params, so a body-carried
    thread_id never reached the authz backend for exactly the endpoints that execute
    and mutate. This drives a minimal body-carrying route through the real
    ``RequirePermission`` and asserts the backend now sees it.
    """
    from typing import Any

    from fastapi import APIRouter, Depends

    from tenxgraph_api.src.app.core.auth.permissions import RequirePermission

    probe = APIRouter()

    @probe.post("/v1/graph/stop")
    async def _stop(
        payload: dict,
        user: dict[str, Any] = Depends(RequirePermission("graph", "stop")),
    ):
        return {"ok": True}

    authz = OwnershipAuthz()
    authz.own("thread-A", "alice")
    app = build_app(routers=[probe], authz=authz)
    client = make_client(app)

    # Owner allowed; body thread_id must reach authz.
    owner = client.post(
        "/v1/graph/stop", json={"thread_id": "thread-A"}, headers=user_headers("alice")
    )
    assert owner.status_code == HTTP_OK
    assert "thread-A" in [str(r) for r in authz.seen_resource_ids]

    # Different user is blocked -- proving body-derived resource_id is enforced.
    other = client.post(
        "/v1/graph/stop", json={"thread_id": "thread-A"}, headers=user_headers("mallory")
    )
    assert other.status_code == HTTP_FORBIDDEN


# ---------------------------------------------------------------------------
# Built-in ownership backend wired end-to-end (the secure production default)
# ---------------------------------------------------------------------------


class _OwnerRegistryCheckpointer:
    """Minimal checkpointer exposing global thread-owner resolution."""

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.owner_lookups = 0

    def own(self, thread_id: str, user_id: str) -> None:
        self.owners[thread_id] = user_id

    async def aget_thread_owner(self, thread_id):
        self.owner_lookups += 1
        return self.owners.get(str(thread_id))


def test_ownership_backend_enforces_owner_only_reads():
    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    registry = _OwnerRegistryCheckpointer()
    registry.own("thread-A", "alice")
    authz = OwnershipAuthorizationBackend(checkpointer=registry)
    # The service reads through a real in-memory checkpointer; authz resolves ownership
    # through the registry above.
    app = build_app(routers=[checkpointer_router], authz=authz, checkpointer=InMemoryCheckpointer())
    client = make_client(app)

    # Owner reads their thread.
    assert (
        client.get("/v1/threads/thread-A/state", headers=user_headers("alice")).status_code
        == HTTP_OK
    )
    # Non-owner is refused object-level access with no custom code -- built-in backend.
    assert (
        client.get("/v1/threads/thread-A/messages", headers=user_headers("mallory")).status_code
        == HTTP_FORBIDDEN
    )


def test_ownership_backend_blocks_invoke_and_stream_on_foreign_thread():
    """Regression for the reported gap: running (invoke/stream) another user's thread
    must be rejected *before* the graph executes, not just at write time."""
    from typing import Any

    from fastapi import APIRouter, Depends

    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend
    from tenxgraph_api.src.app.core.auth.permissions import RequirePermission

    graph_probe = APIRouter()

    @graph_probe.post("/v1/graph/invoke")
    async def _invoke(
        payload: dict,
        user: dict[str, Any] = Depends(RequirePermission("graph", "invoke")),
    ):
        return {"ran": True}

    @graph_probe.post("/v1/graph/stream")
    async def _stream(
        payload: dict,
        user: dict[str, Any] = Depends(RequirePermission("graph", "stream")),
    ):
        return {"ran": True}

    registry = _OwnerRegistryCheckpointer()
    registry.own("thread-A", "alice")
    authz = OwnershipAuthorizationBackend(checkpointer=registry)
    app = build_app(routers=[graph_probe], authz=authz, checkpointer=InMemoryCheckpointer())
    client = make_client(app)

    # Owner can run their own thread.
    assert (
        client.post(
            "/v1/graph/invoke", json={"thread_id": "thread-A"}, headers=user_headers("alice")
        ).status_code
        == HTTP_OK
    )
    # Attacker cannot invoke or stream someone else's thread -- blocked up front.
    assert (
        client.post(
            "/v1/graph/invoke", json={"thread_id": "thread-A"}, headers=user_headers("mallory")
        ).status_code
        == HTTP_FORBIDDEN
    )
    assert (
        client.post(
            "/v1/graph/stream", json={"thread_id": "thread-A"}, headers=user_headers("mallory")
        ).status_code
        == HTTP_FORBIDDEN
    )
    # A brand-new thread for the attacker is fine (their own new session).
    assert (
        client.post(
            "/v1/graph/invoke", json={"thread_id": "new-one"}, headers=user_headers("mallory")
        ).status_code
        == HTTP_OK
    )


def test_ownership_is_cached_across_requests():
    """End-to-end scalability guarantee: repeated requests for the same thread cost a
    single owner lookup, not one per request."""
    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    registry = _OwnerRegistryCheckpointer()
    registry.own("thread-A", "alice")
    authz = OwnershipAuthorizationBackend(checkpointer=registry)
    app = build_app(routers=[checkpointer_router], authz=authz, checkpointer=InMemoryCheckpointer())
    client = make_client(app)

    for _ in range(6):
        client.get("/v1/threads/thread-A/state", headers=user_headers("alice"))
        client.get("/v1/threads/thread-A/messages", headers=user_headers("mallory"))

    # 12 HTTP requests touched thread-A; ownership was resolved from the backing
    # store exactly once (everything else served from the in-process cache).
    assert registry.owner_lookups == 1


# ---------------------------------------------------------------------------
# Ownership with the REAL in-memory checkpointer, no owner-registry stub
# ---------------------------------------------------------------------------


def _real_ownership_app():
    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    checkpointer = InMemoryCheckpointer()
    authz = OwnershipAuthorizationBackend(checkpointer=checkpointer)
    app = build_app(routers=[checkpointer_router], authz=authz, checkpointer=checkpointer)
    return make_client(app), checkpointer


def test_a_state_write_claims_the_thread_for_the_writer():
    """The writer of a new thread becomes its owner; nobody else can then read or write it.

    Before the fix the in-memory checkpointer reported no owner for API-created threads (the
    API saved ThreadInfo without user_id) and an unowned thread was open to everyone.
    """
    import anyio

    client, checkpointer = _real_ownership_app()
    body = {"state": {"context": []}, "config": {}}

    put = client.put("/v1/threads/t-new/state", json=body, headers=user_headers("alice"))
    assert put.status_code == HTTP_OK, put.text
    assert anyio.run(checkpointer.aget_thread_owner, "t-new") == "alice"

    assert client.get("/v1/threads/t-new/state", headers=user_headers("alice")).status_code == (
        HTTP_OK
    )
    assert (
        client.get("/v1/threads/t-new/state", headers=user_headers("mallory")).status_code
        == HTTP_FORBIDDEN
    )
    assert (
        client.put("/v1/threads/t-new/state", json=body, headers=user_headers("mallory"))
    ).status_code == HTTP_FORBIDDEN


def test_a_message_write_claims_the_thread_for_the_writer():
    import anyio

    client, checkpointer = _real_ownership_app()
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}

    put = client.post("/v1/threads/t-msg/messages", json=body, headers=user_headers("alice"))
    assert put.status_code == HTTP_OK, put.text
    assert anyio.run(checkpointer.aget_thread_owner, "t-msg") == "alice"
    assert (
        client.get("/v1/threads/t-msg/messages", headers=user_headers("mallory")).status_code
        == HTTP_FORBIDDEN
    )


def test_reads_of_an_unowned_thread_are_refused_for_everyone():
    """No owner means nothing of the caller's is there; only creating it is allowed."""
    client, _ = _real_ownership_app()
    for path in ("/v1/threads/ghost/state", "/v1/threads/ghost/messages", "/v1/threads/ghost"):
        resp = client.get(path, headers=user_headers("alice"))
        assert resp.status_code == HTTP_FORBIDDEN, f"{path} -> {resp.status_code}"
    resp = client.request("DELETE", "/v1/threads/ghost", json={}, headers=user_headers("alice"))
    assert resp.status_code == HTTP_FORBIDDEN


def test_server_thread_ids_refuse_a_client_named_new_thread():
    """runs.thread_ids = "server": nobody can claim an id before the server hands it out."""
    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    checkpointer = InMemoryCheckpointer()
    authz = OwnershipAuthorizationBackend(checkpointer=checkpointer, server_thread_ids=True)
    client = make_client(
        build_app(routers=[checkpointer_router], authz=authz, checkpointer=checkpointer)
    )
    body = {"state": {"context": []}, "config": {}}
    resp = client.put("/v1/threads/victim-chat/state", json=body, headers=user_headers("mallory"))
    assert resp.status_code == HTTP_FORBIDDEN


def test_a_deleted_thread_id_is_not_handed_to_another_user_right_away():
    """Other workers may still cache the old owner for one L1 TTL after a delete."""
    import anyio

    from tenxgraph_api.src.app.core.auth.authorization import OwnershipAuthorizationBackend

    checkpointer = InMemoryCheckpointer()
    authz = OwnershipAuthorizationBackend(checkpointer=checkpointer)
    client = make_client(
        build_app(routers=[checkpointer_router], authz=authz, checkpointer=checkpointer)
    )
    body = {"state": {"context": []}, "config": {}}
    put = client.put("/v1/threads/t-del/state", json=body, headers=user_headers("alice"))
    assert put.status_code == HTTP_OK
    deleted = client.request("DELETE", "/v1/threads/t-del", json={}, headers=user_headers("alice"))
    assert deleted.status_code == HTTP_OK, deleted.text

    # The delete reached this app's authorization backend: owner evicted, tombstone left.
    resolver = authz._get_resolver()
    assert "t-del" not in resolver._l1
    assert anyio.run(resolver.recently_deleted, "t-del") is True

    reuse = client.put("/v1/threads/t-del/state", json=body, headers=user_headers("mallory"))
    assert reuse.status_code == HTTP_FORBIDDEN


def test_local_store_download_streams_for_the_owner_only(tmp_path):
    """The disk-streaming download path keeps the ownership check."""
    import anyio

    from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType

    settings = MediaSettings(
        MEDIA_STORAGE_TYPE=MediaStorageType.LOCAL, MEDIA_STORAGE_PATH=str(tmp_path)
    )
    service = MediaService(settings=settings)
    client = make_client(build_app(routers=[media_router], extra_bindings={MediaService: service}))

    upload = client.post(
        "/v1/files/upload",
        files={"file": ("a.txt", b"secret-A" * 1000, "text/plain")},
        headers=user_headers("alice"),
    )
    assert upload.status_code == HTTP_OK, upload.text
    file_id = upload.json()["data"]["file_id"]
    assert anyio.run(service.local_file, file_id) is not None  # streamed path

    owner = client.get(f"/v1/files/{file_id}", headers=user_headers("alice"))
    assert owner.status_code == HTTP_OK
    assert owner.content == b"secret-A" * 1000
    assert client.get(f"/v1/files/{file_id}", headers=user_headers("mallory")).status_code == (
        HTTP_NOT_FOUND
    )


def test_the_owner_can_recreate_a_thread_they_just_deleted():
    client, _ = _real_ownership_app()
    body = {"state": {"context": []}, "config": {}}
    headers = user_headers("alice")
    assert client.put("/v1/threads/t-again/state", json=body, headers=headers).status_code == 200
    assert client.request(
        "DELETE", "/v1/threads/t-again", json={}, headers=headers
    ).status_code == (HTTP_OK)
    assert client.put("/v1/threads/t-again/state", json=body, headers=headers).status_code == (
        HTTP_OK
    )
