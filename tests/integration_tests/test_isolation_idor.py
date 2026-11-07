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

from .conftest import build_app, make_client, user_headers


HTTP_OK = 200
HTTP_BAD_REQUEST = 400
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
# Thread ownership with the REAL in-memory checkpointer (storage enforces it)
# ---------------------------------------------------------------------------

BODY = {"state": {"context": []}, "config": {}}


def _thread_client():
    checkpointer = InMemoryCheckpointer()
    app = build_app(routers=[checkpointer_router], checkpointer=checkpointer)
    return make_client(app), checkpointer


def test_a_state_write_claims_the_thread_for_the_writer():
    """The writer of a new thread becomes its owner; another user cannot overwrite it."""
    import anyio

    client, checkpointer = _thread_client()

    put = client.put("/v1/threads/t-new/state", json=BODY, headers=user_headers("alice"))
    assert put.status_code == HTTP_OK, put.text
    assert anyio.run(checkpointer.aget_thread_owner, "t-new") == "alice"

    mallory = client.put("/v1/threads/t-new/state", json=BODY, headers=user_headers("mallory"))
    assert mallory.status_code >= HTTP_BAD_REQUEST, mallory.text
    # The owner is unchanged.
    assert anyio.run(checkpointer.aget_thread_owner, "t-new") == "alice"


def test_a_foreign_thread_looks_missing_to_other_users():
    client, _ = _thread_client()
    put = client.put("/v1/threads/t-priv/state", json=BODY, headers=user_headers("alice"))
    assert put.status_code == HTTP_OK, put.text

    state = client.get("/v1/threads/t-priv/state", headers=user_headers("mallory"))
    assert state.status_code == HTTP_OK
    assert state.json()["data"]["state"] is None

    thread = client.get("/v1/threads/t-priv", headers=user_headers("mallory"))
    assert thread.status_code == HTTP_OK
    assert thread.json()["data"]["thread_data"]["thread"] is None


def test_the_owner_can_recreate_a_thread_they_just_deleted():
    client, _ = _thread_client()
    headers = user_headers("alice")
    assert client.put("/v1/threads/t-again/state", json=BODY, headers=headers).status_code == (
        HTTP_OK
    )
    deleted = client.request("DELETE", "/v1/threads/t-again", json={}, headers=headers)
    assert deleted.status_code == HTTP_OK
    assert client.put("/v1/threads/t-again/state", json=BODY, headers=headers).status_code == (
        HTTP_OK
    )


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
