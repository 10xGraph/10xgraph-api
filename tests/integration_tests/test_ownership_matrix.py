"""Ownership matrix: what each reach may do with another user's threads, files and memories.

``alice`` (org-a) owns a thread with state and a message, an uploaded file and a memory.
Other callers: ``bob`` in the same tenant (org-a) and ``carol`` in another tenant (org-b), each
with reach ``own`` (role member), ``tenant`` (org_admin) or ``all`` (admin).

- Reads, lists and deletes follow the reach: ``own`` sees only its own data, ``tenant`` the
  data of its tenant, ``all`` everything. Anything outside the reach is a 404.
- Writes (PUT state, message feedback, a file reference in a run) always stay ``own``: 404 /
  422 for everyone but the owner, admins included.

Threads run on the real ``InMemoryCheckpointer`` (no tenant columns, so reach ``tenant`` is
skipped there) and on ``TenantCheckpointer`` (the same, plus an ``org_id`` tenant column).
Memories use the ``TenantStore`` stand-in: core has no in-memory store.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from injectq import InjectQ
from tenxgraph.core.authz import Reach
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.storage.store import BaseStore
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.auth.rbac import RbacConfig, RoleConfig
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.media import MediaService
from tenxgraph_api.src.app.routers.media.router import router as media_router
from tenxgraph_api.src.app.routers.store.router import router as store_router
from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

from .conftest import ClientThreadIdsConfig, build_app, make_client
from .test_tenant_isolation import TenantAuth, TenantCheckpointer, TenantStore, _seed_cfg


OK, NOT_FOUND, UNPROCESSABLE = 200, 404, 422
ROLES = {Reach.OWN: "member", Reach.TENANT: "org_admin", Reach.ALL: "admin"}


class ReachAuth(TenantAuth):
    rbac = RbacConfig(
        tenant_claims=["org_id"],
        roles={
            "member": RoleConfig(permissions=["*"], reach=Reach.OWN),
            "org_admin": RoleConfig(permissions=["*"], reach=Reach.TENANT),
            "admin": RoleConfig(permissions=["*"], reach=Reach.ALL),
        },
        default_role="member",
    )


def _as(user: str, org: str, reach: Reach = Reach.OWN) -> dict[str, str]:
    return {"X-Test-User": user, "X-Test-Org": org, "X-Test-Role": ROLES[reach]}


ALICE = _as("alice", "org-a")
CALLERS = {"same_tenant": ("bob", "org-a"), "other_tenant": ("carol", "org-b")}


def _reaches(caller: str, reach: Reach) -> bool:
    """Whether ``reach`` covers alice's data for this caller (the rule under test)."""
    if reach is Reach.ALL:
        return True
    return reach is Reach.TENANT and caller == "same_tenant"


def _file_ref(key: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "image", "media": {"kind": "url", "url": f"graph://media/{key}"}}],
    }


class _World:
    """The app over one backend, with alice's thread ``t-a``, file and memory seeded."""

    def __init__(self, backend: str, checkpointer: Any = None) -> None:
        if checkpointer is None:
            checkpointer = TenantCheckpointer() if backend == "tenant" else InMemoryCheckpointer()
        self.cp = checkpointer
        settings = MediaSettings(
            _env_file=None, MEDIA_STORAGE_TYPE=MediaStorageType.MEMORY, MEDIA_REQUIRE_OWNER=True
        )  # type: ignore[call-arg]
        self.media = MediaService(settings=settings, checkpointer=self.cp)
        self.store = TenantStore()
        self.telemetry = TelemetryStore()
        # The checkpointer service finds media and telemetry through the global container.
        InjectQ.get_instance().bind_instance(MediaService, self.media)
        InjectQ.get_instance().bind_instance(TelemetryStore, self.telemetry)
        app = build_app(
            routers=[checkpointer_router, media_router, store_router],
            auth=ReachAuth(),
            checkpointer=self.cp,
            extra_bindings={
                MediaService: self.media,
                BaseStore: self.store,
                GraphConfig: ClientThreadIdsConfig(),
            },
        )
        self.client = make_client(app)

        cfg = _seed_cfg("t-a", "alice", "org-a")
        self.message = Message.text_message("secret of alice")
        anyio.run(self.cp.aput_thread, cfg, ThreadInfo(thread_id="t-a"))
        anyio.run(self.cp.aput_state, cfg, AgentState())
        anyio.run(self.cp.aput_messages, cfg, [self.message])
        self.telemetry.start_run("t-a", "r1", 0.0)

        upload = self.client.post(
            "/v1/files/upload", files={"file": ("a.txt", b"secret", "text/plain")}, headers=ALICE
        )
        assert upload.status_code == OK, upload.text
        self.file_id = upload.json()["data"]["file_id"]
        # Link the file to t-a the way the API does: alice writes a message referencing it.
        put = self.put_state("t-a", ALICE, [_file_ref(self.file_id)])
        assert put.status_code == OK, put.text

        memory = self.client.post("/v1/store/memories", json={"content": "note"}, headers=ALICE)
        assert memory.status_code == OK, memory.text
        self.memory_id = memory.json()["data"]["memory_id"]

    def put_state(self, thread_id: str, headers: dict[str, str], context=None) -> Any:
        body = {"state": {"context": context or []}, "config": {}}
        return self.client.put(f"/v1/threads/{thread_id}/state", json=body, headers=headers)

    def delete(self, path: str, headers: dict[str, str]) -> Any:
        return self.client.request("DELETE", path, json={}, headers=headers)

    def thread_exists(self) -> bool:
        cfg = _seed_cfg("t-a", "alice", "org-a")
        return anyio.run(self.cp.aget_thread, cfg) is not None

    def file_meta(self) -> dict[str, Any] | None:
        return anyio.run(self.media.store.get_metadata, self.file_id)


@pytest.fixture(autouse=True)
def _isolated_container():
    with InjectQ.test_mode():
        yield


MATRIX = [
    pytest.param(backend, caller, reach, id=f"{backend}-{caller}-{reach.value}")
    for backend in ("plain", "tenant")
    for caller in CALLERS
    for reach in Reach
]


def _skip_tenant_on_plain(backend: str, reach: Reach) -> None:
    if backend == "plain" and reach is Reach.TENANT:
        pytest.skip("reach tenant needs tenant columns; InMemoryCheckpointer declares none")


# ---------------------------------------------------------------------------
# The owner can do everything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["plain", "tenant"])
def test_owner_reads_writes_and_deletes(backend):
    w = _World(backend)
    mid = w.message.message_id
    for path in ("", "/state", "/messages", f"/messages/{mid}"):
        assert w.client.get(f"/v1/threads/t-a{path}", headers=ALICE).status_code == OK
    feedback = {"feedback": {"rating": "up"}}
    put = w.client.put(f"/v1/threads/t-a/messages/{mid}", json=feedback, headers=ALICE)
    assert put.status_code == OK, put.text
    assert w.client.get(f"/v1/files/{w.file_id}/info", headers=ALICE).status_code == OK
    assert (
        w.client.post(f"/v1/store/memories/{w.memory_id}", json={}, headers=ALICE).status_code == OK
    )
    assert w.delete(f"/v1/threads/t-a/messages/{mid}", ALICE).status_code == OK
    assert w.delete("/v1/threads/t-a", ALICE).status_code == OK
    assert not w.thread_exists()
    assert w.file_meta() is None  # its last thread is gone (media.delete_with_thread)
    assert w.telemetry.get_runs("t-a") == []


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("backend", "caller", "reach"), MATRIX)
def test_thread_routes_follow_reach_and_writes_stay_own(backend, caller, reach):
    _skip_tenant_on_plain(backend, reach)
    w = _World(backend)
    headers = _as(*CALLERS[caller], reach)
    mid = w.message.message_id
    allowed = _reaches(caller, reach)
    expect = OK if allowed else NOT_FOUND

    for path in ("", "/state", "/messages", f"/messages/{mid}"):
        resp = w.client.get(f"/v1/threads/t-a{path}", headers=headers)
        assert resp.status_code == expect, (path, resp.text)
    listed = w.client.get("/v1/threads", headers=headers).json()["data"]["threads"]
    assert ("t-a" in {t["thread_id"] for t in listed}) is allowed

    # Writes never reach another user's thread, whatever the reach.
    assert w.put_state("t-a", headers).status_code == NOT_FOUND
    feedback = {"feedback": {"rating": "down"}}
    resp = w.client.put(f"/v1/threads/t-a/messages/{mid}", json=feedback, headers=headers)
    assert resp.status_code == NOT_FOUND

    # Deletes follow the reach; a refused one leaves everything in place.
    assert w.delete(f"/v1/threads/t-a/messages/{mid}", headers).status_code == expect
    assert w.delete("/v1/threads/t-a/state", headers).status_code == expect
    assert w.delete("/v1/threads/t-a", headers).status_code == expect
    assert w.thread_exists() is not allowed
    if not allowed:
        assert w.client.get(f"/v1/threads/t-a/messages/{mid}", headers=ALICE).status_code == OK


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("backend", "caller", "reach"), MATRIX)
def test_file_routes_follow_reach_and_references_stay_own(backend, caller, reach):
    _skip_tenant_on_plain(backend, reach)
    w = _World(backend)
    headers = _as(*CALLERS[caller], reach)
    allowed = _reaches(caller, reach)
    expect = OK if allowed else NOT_FOUND

    for path in (f"/v1/files/{w.file_id}/info", f"/v1/files/{w.file_id}"):
        assert w.client.get(path, headers=headers).status_code == expect, path
    listed = w.client.get("/v1/files", headers=headers).json()["data"]["files"]
    assert (w.file_id in {f["file_id"] for f in listed}) is allowed

    # Referencing alice's file in the caller's own thread is refused for every reach.
    resp = w.put_state("t-own", headers, [_file_ref(w.file_id)])
    assert resp.status_code == UNPROCESSABLE, resp.text
    assert w.file_meta()["threads"] == ["t-a"]  # not linked to the caller's thread

    assert w.delete(f"/v1/files/{w.file_id}", headers).status_code == expect
    assert (w.file_meta() is None) is allowed


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("caller", "reach"), [(c, r) for c in CALLERS for r in Reach])
def test_memory_routes_follow_reach(caller, reach):
    w = _World("tenant")
    headers = _as(*CALLERS[caller], reach)
    allowed = _reaches(caller, reach)
    expect = OK if allowed else NOT_FOUND

    got = w.client.post(f"/v1/store/memories/{w.memory_id}", json={}, headers=headers)
    assert got.status_code == expect, got.text
    found = w.client.post("/v1/store/search", json={"query": "note"}, headers=headers)
    assert (w.memory_id in {m["id"] for m in found.json()["data"]["results"]}) is allowed
    gone = w.delete(f"/v1/store/memories/{w.memory_id}", headers)
    assert gone.status_code == expect
    assert (w.memory_id in w.store.rows) is not allowed


# ---------------------------------------------------------------------------
# Thread delete: cleanup only after storage deleted something (AUTH-S0-11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["plain", "tenant"])
def test_a_foreign_delete_leaves_the_victims_files_and_traces(backend):
    w = _World(backend)
    resp = w.delete("/v1/threads/t-a", _as("mallory", "org-a"))
    assert resp.status_code == NOT_FOUND
    assert w.thread_exists()
    assert w.file_meta()["threads"] == ["t-a"]
    assert len(w.telemetry.get_runs("t-a")) == 1


def test_an_org_admin_delete_releases_the_owners_files():
    w = _World("tenant")
    assert w.delete("/v1/threads/t-a", _as("bob", "org-a", Reach.TENANT)).status_code == OK
    assert w.file_meta() is None
    assert w.telemetry.get_runs("t-a") == []


@pytest.mark.parametrize("backend", ["plain", "tenant"])
def test_delete_then_recreate_by_another_user(backend):
    w = _World(backend)
    assert w.delete("/v1/threads/t-a", ALICE).status_code == OK
    bob = _as("bob", "org-a")
    assert w.put_state("t-a", bob).status_code == OK
    assert (
        w.client.get("/v1/threads/t-a", headers=bob).json()["data"]["thread_data"]["thread"][
            "user_id"
        ]
        == "bob"
    )
    assert w.client.get("/v1/threads/t-a", headers=ALICE).status_code == NOT_FOUND


class _FailingRead(InMemoryCheckpointer):
    """Fails the scoped thread read once ``fail`` is set; counts deletes."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.cleaned = 0

    async def aget_thread(self, config):
        if self.fail:
            raise RuntimeError("database is down")
        return await super().aget_thread(config)

    async def aclean_thread(self, config):
        self.cleaned += 1
        return await super().aclean_thread(config)


def test_a_storage_error_in_the_scoped_read_fails_closed():
    cp = _FailingRead()
    w = _World("plain", checkpointer=cp)
    cp.fail = True
    resp = w.delete("/v1/threads/t-a", ALICE)
    assert resp.status_code >= 500
    assert cp.cleaned == 0
    assert w.file_meta()["threads"] == ["t-a"]
    assert len(w.telemetry.get_runs("t-a")) == 1
    for path in ("/v1/threads/t-a", "/v1/threads/t-a/state", "/v1/threads/t-a/messages"):
        assert w.client.get(path, headers=ALICE).status_code >= 500


def test_unknown_thread_is_404_under_server_thread_ids():
    """With the default ``runs.thread_ids: "server"`` a client cannot name a new thread."""
    cp = InMemoryCheckpointer()
    app = build_app(routers=[checkpointer_router], auth=ReachAuth(), checkpointer=cp)
    client = make_client(app)
    body = {"state": {"context": []}, "config": {}}
    resp = client.put("/v1/threads/t-new/state", json=body, headers=ALICE)
    assert resp.status_code == NOT_FOUND
    assert anyio.run(cp.aget_thread_owner, "t-new") is None
