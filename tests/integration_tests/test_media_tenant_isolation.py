"""Uploaded files are scoped by tenant columns as well as ``user_id``.

The same ``user_id`` in two tenants (``alice`` in ``org-a`` and in ``org-b``) is two identities:
neither may list, read, sign, delete, reference or link the other's files, and each has its own
quota. The tenant values come from the authenticated identity (``config["user"]``) for the
tenant columns the bound checkpointer declares; a file records them at upload.

Also covered: a legacy file with no recorded tenant values is denied while tenant columns are
declared, typed columns compare after conversion (``uuid`` case), nothing changes without
tenant columns, and deleting a member's thread as an org admin releases the member's files.
"""

# ruff: noqa: S101, PLR2004, SLF001

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from injectq import InjectQ
from tenxgraph.core.authz import Identity
from tenxgraph.core.graph import CompiledGraph, StateGraph
from tenxgraph.core.state import AgentState, Message, TextBlock
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.columns import parse_columns
from tenxgraph.utils.constants import END
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.graph.router import router as graph_router
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    check_media_references,
)
from tenxgraph_api.src.app.routers.media import TENANT_KEY, MediaService
from tenxgraph_api.src.app.routers.media.router import router as media_router

from .conftest import build_app, make_client
from .test_tenant_isolation import TenantAuth, TenantCheckpointer, _as, _seed_cfg


ALICE_A = _as("alice", "org-a")
ALICE_B = _as("alice", "org-b")
SECRET_A = b"secret-of-alice-in-org-a"
USER_A = Identity(user_id="alice", tenant={"org_id": "org-a"})
USER_B = Identity(user_id="alice", tenant={"org_id": "org-b"})


class _Harness:
    """Media, graph and checkpointer routes over a checkpointer with ``org_id`` declared."""

    def __init__(self, tmp_path: Path, checkpointer: Any = None, **media_settings: Any) -> None:
        settings = MediaSettings(
            _env_file=None,
            MEDIA_STORAGE_TYPE=MediaStorageType.MEMORY,
            MEDIA_REQUIRE_OWNER=True,
            **media_settings,
        )  # type: ignore[call-arg]
        self.checkpointer = checkpointer if checkpointer is not None else TenantCheckpointer()
        self.media = MediaService(settings=settings, checkpointer=self.checkpointer)

        config_file = tmp_path / "10xgraph.json"
        config_file.write_text(
            json.dumps({"agent": "x:y", "auth": {"method": "custom", "path": "x:y"}})
        )
        config = GraphConfig(str(config_file))

        graph = StateGraph(AgentState())
        graph.add_node(
            "MAIN", lambda state, config: Message(role="assistant", content=[TextBlock(text="ok")])
        )
        graph.set_entry_point("MAIN")
        graph.add_edge("MAIN", END)
        compiled = graph.compile(checkpointer=self.checkpointer)

        bindings = {
            MediaService: self.media,
            BaseAuth: TenantAuth(),
            GraphConfig: config,
            BaseCheckpointer: self.checkpointer,
            CompiledGraph: compiled,
        }
        active = InjectQ.get_instance()
        for kind, value in bindings.items():
            active.bind_instance(kind, value, allow_none=True)
        app = build_app(
            routers=[media_router, graph_router, checkpointer_router],
            auth=TenantAuth(),
            checkpointer=self.checkpointer,
            extra_bindings=bindings,
        )
        self.client: TestClient = make_client(app)

    def upload(self, headers: dict[str, str], data: bytes = SECRET_A) -> Any:
        return self.client.post(
            "/v1/files/upload",
            files={"file": ("note.txt", data, "text/plain")},
            headers=headers,
        )

    def upload_key(self, headers: dict[str, str], data: bytes = SECRET_A) -> str:
        resp = self.upload(headers, data)
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]["file_id"]

    def metadata(self, key: str) -> dict[str, Any] | None:
        return anyio.run(self.media.store.get_metadata, key)


@pytest.fixture
def h(tmp_path):
    with InjectQ.test_mode():
        yield _Harness(tmp_path)


def _image(key: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "image", "media": {"kind": "url", "url": f"graph://media/{key}"}}],
    }


def _listed(h: _Harness, headers: dict[str, str]) -> list[str]:
    resp = h.client.get("/v1/files", headers=headers)
    assert resp.status_code == 200, resp.text
    return [f["file_id"] for f in resp.json()["data"]["files"]]


# --------------------------------------------------------------------------- media routes


def test_upload_records_the_callers_tenant_values(h):
    key = h.upload_key(ALICE_A)
    meta = h.metadata(key)
    assert meta["owner_id"] == "alice"
    assert meta[TENANT_KEY] == {"org_id": "org-a"}


def test_same_user_id_in_another_tenant_cannot_reach_the_file(h):
    key = h.upload_key(ALICE_A)
    for path in (f"/v1/files/{key}", f"/v1/files/{key}/info", f"/v1/files/{key}/url"):
        resp = h.client.get(path, headers=ALICE_B)
        assert resp.status_code == 404, f"{path} -> {resp.status_code}"
        assert SECRET_A.decode() not in resp.text
    assert h.client.delete(f"/v1/files/{key}", headers=ALICE_B).status_code == 404
    assert _listed(h, ALICE_B) == []

    # Within the tenant every route works, and the delete above did nothing.
    own = h.client.get(f"/v1/files/{key}", headers=ALICE_A)
    assert own.status_code == 200 and own.content == SECRET_A
    assert h.client.get(f"/v1/files/{key}/info", headers=ALICE_A).status_code == 200
    assert h.client.get(f"/v1/files/{key}/url", headers=ALICE_A).status_code == 200
    assert _listed(h, ALICE_A) == [key]
    assert h.client.delete(f"/v1/files/{key}", headers=ALICE_A).status_code == 200
    assert h.metadata(key) is None


def test_listing_keeps_the_two_tenants_apart(h):
    key_a = h.upload_key(ALICE_A)
    key_b = h.upload_key(ALICE_B, b"secret-of-alice-in-org-b")
    assert _listed(h, ALICE_A) == [key_a]
    assert _listed(h, ALICE_B) == [key_b]


def test_a_token_without_a_tenant_claim_is_refused(h):
    assert h.upload(_as("alice")).status_code == 401
    assert h.client.get("/v1/files", headers=_as("alice")).status_code == 401


@pytest.mark.asyncio
async def test_an_identity_without_a_tenant_value_cannot_use_the_file(h):
    key = h.upload_key(ALICE_A)
    no_tenant = Identity(user_id="alice")
    with pytest.raises(PermissionError):
        await h.media.ensure_can_access(key, "alice", no_tenant)
    assert await h.media.list_files("alice", user=no_tenant) == ([], False)


def test_quotas_are_counted_per_tenant(tmp_path):
    with InjectQ.test_mode():
        h = _Harness(tmp_path, MEDIA_MAX_FILES_PER_USER=1)
        h.upload_key(ALICE_A)
        # The same user_id in org-b has its own quota.
        h.upload_key(ALICE_B)
        over = h.upload(ALICE_A)
        assert over.status_code == 413
        assert "Upload limit" in over.text


# --------------------------------------------------------------------------- references


def test_invoke_cannot_reference_the_other_tenants_file(h):
    key = h.upload_key(ALICE_A)
    body = {"messages": [_image(key)]}
    denied = h.client.post("/v1/graph/invoke", json=body, headers=ALICE_B)
    assert denied.status_code in (400, 422), denied.text
    assert SECRET_A.decode() not in denied.text
    allowed = h.client.post("/v1/graph/invoke", json=body, headers=ALICE_A)
    assert allowed.status_code == 200, allowed.text


def test_stream_cannot_reference_the_other_tenants_file(h):
    key = h.upload_key(ALICE_A)
    body = {"messages": [_image(key)]}
    denied = h.client.post("/v1/graph/stream", json=body, headers=ALICE_B)
    assert "File not found" in denied.text or denied.status_code in (400, 422)
    assert SECRET_A.decode() not in denied.text


def test_put_state_cannot_reference_the_other_tenants_file(h):
    key = h.upload_key(ALICE_A)
    cfg = _seed_cfg("t-b", "alice", "org-b")
    anyio.run(h.checkpointer.aput_thread, cfg, ThreadInfo(thread_id="t-b", user_id="alice"))
    body = {"state": {"context": [_image(key)]}}
    resp = h.client.put("/v1/threads/t-b/state", json=body, headers=ALICE_B)
    assert resp.status_code in (400, 422), resp.text


@pytest.mark.asyncio
async def test_reference_checks_and_links_compare_tenant_values(h):
    key = h.upload_key(ALICE_A)
    message = Message.model_validate(_image(key))
    user_a, user_b = USER_A, USER_B

    with pytest.raises(ClientInputError, match="not found"):
        await check_media_references([message], h.media, "alice", user_b)
    with pytest.raises(ClientInputError, match="not found"):
        await check_media_references([message], h.media, "alice")  # no identity: no tenant
    await check_media_references([message], h.media, "alice", user_a)

    await h.media.link_threads([key], "t1", "alice", user_b)
    assert (await h.media.store.get_metadata(key))["threads"] == []
    await h.media.link_threads([key], "t1", "alice", user_a)
    assert (await h.media.store.get_metadata(key))["threads"] == ["t1"]


# --------------------------------------------------------------------------- legacy and types


@pytest.mark.asyncio
async def test_a_file_without_tenant_values_is_denied_under_tenant_columns(h):
    """Uploaded before tenant tracking: no recorded tenant, so no tenant may use it."""
    key = await h.media.store.store(b"legacy", "text/plain", metadata={"owner_id": "alice"})
    user_a = USER_A
    with pytest.raises(PermissionError):
        await h.media.ensure_can_access(key, "alice", user_a)
    assert await h.media.list_files("alice", user=user_a) == ([], False)


@pytest.mark.asyncio
async def test_an_ownerless_file_is_denied_under_tenant_columns_even_in_development(tmp_path):
    with InjectQ.test_mode():
        h = _Harness(tmp_path)
        h.media._settings.MEDIA_REQUIRE_OWNER = False
        key = await h.media.store.store(b"legacy", "text/plain", metadata={})
        with pytest.raises(PermissionError):
            await h.media.ensure_can_access(key, "alice", USER_A)


class _UuidCheckpointer(InMemoryCheckpointer):
    def __init__(self) -> None:
        super().__init__()
        self.columns = parse_columns({"org_id": "uuid"}, table="threads")
        self.tenant_column_names = self.columns.tenant_names


@pytest.mark.asyncio
async def test_typed_tenant_columns_record_and_compare_the_identity_text(tmp_path):
    org = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
    with InjectQ.test_mode():
        h = _Harness(tmp_path, checkpointer=_UuidCheckpointer())
        owner = Identity(user_id="alice", tenant={"org_id": org})
        result = await h.media.upload_file(b"x", "a.txt", "text/plain", user_id="alice", user=owner)
        key = result["file_id"]
        assert (await h.media.store.get_metadata(key))[TENANT_KEY] == {"org_id": org}
        await h.media.ensure_can_access(key, "alice", owner)
        other = Identity(user_id="alice", tenant={"org_id": "nope"})
        with pytest.raises(PermissionError):
            await h.media.ensure_can_access(key, "alice", other)


@pytest.mark.asyncio
async def test_no_tenant_columns_records_no_tenant_and_checks_the_owner_only(tmp_path):
    with InjectQ.test_mode():
        h = _Harness(tmp_path, checkpointer=InMemoryCheckpointer())
        result = await h.media.upload_file(b"x", "a.txt", "text/plain", user_id="alice")
        key = result["file_id"]
        assert TENANT_KEY not in await h.media.store.get_metadata(key)
        await h.media.ensure_can_access(key, "alice")
        await h.media.ensure_can_access(
            key, "alice", Identity(user_id="alice", tenant={"org_id": "any"})
        )
        with pytest.raises(PermissionError):
            await h.media.ensure_can_access(key, "bob")


# --------------------------------------------------------------------------- thread delete


def test_org_admin_thread_delete_releases_the_members_files(h):
    """The admin's own id owns nothing here: the release follows the thread's owner."""
    key = h.upload_key(ALICE_A)
    cfg = _seed_cfg("t-a", "alice", "org-a")
    anyio.run(h.checkpointer.aput_thread, cfg, ThreadInfo(thread_id="t-a", user_id="alice"))
    # Linked through a message that is gone since: only the metadata scan finds it.
    anyio.run(h.media.link_threads, [key], "t-a", "alice", USER_A)
    assert h.metadata(key)["threads"] == ["t-a"]

    admin = _as("carol", "org-a", "org_admin")
    resp = h.client.request("DELETE", "/v1/threads/t-a", json={}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert h.metadata(key) is None


def test_thread_delete_keeps_the_same_user_ids_file_in_another_tenant(h):
    """A file of alice@org-b linked to the same thread id is not alice@org-a's to release."""
    key_b = h.upload_key(ALICE_B, b"secret-of-alice-in-org-b")
    anyio.run(h.media.link_threads, [key_b], "t-a", "alice", USER_B)
    cfg = _seed_cfg("t-a", "alice", "org-a")
    anyio.run(h.checkpointer.aput_thread, cfg, ThreadInfo(thread_id="t-a", user_id="alice"))

    resp = h.client.request("DELETE", "/v1/threads/t-a", json={}, headers=ALICE_A)
    assert resp.status_code == 200, resp.text
    assert h.metadata(key_b)["threads"] == ["t-a"]
