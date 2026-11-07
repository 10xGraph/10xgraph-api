"""Tenant isolation through the API: reach, tenant values and client filters.

Two tenants hold a user with the same ``user_id`` (``alice`` in ``org-a`` and in ``org-b``).
Every route family must keep them apart, the tenant always coming from the authenticated
identity (``config["user"]``) and never from request config or ``filters``.

The checkpointer and store here are small in-memory stand-ins for ``PgCheckpointer`` /
``PgVectorStore`` with ``tenant_columns=["org_id"]``. They resolve values and isolation with
the real core helpers (``ColumnSet.resolve``, ``owner_filter``, ``validate_filters``), so what
is tested is the API's side of the core contract.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from tenxgraph.core.authz import Identity, Reach, owner_filter
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.storage.columns import parse_columns
from tenxgraph.storage.sql_filter import FilterError, FilterSchema, validate_filters
from tenxgraph.storage.store import BaseStore, MemorySearchResult
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.rbac import RbacConfig, RoleConfig
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.store.router import router as store_router

from .conftest import build_app, make_client


HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_UNPROCESSABLE = 422


# ---------------------------------------------------------------------------
# Auth and storage stand-ins
# ---------------------------------------------------------------------------


class TenantAuth(BaseAuth):
    """Claims from headers: ``X-Test-User``, ``X-Test-Org`` and ``X-Test-Role``.

    ``org_admin`` has reach ``tenant`` (every thread and memory of the org); everyone else
    has reach ``own``.
    """

    rbac = RbacConfig(
        tenant_claims=["org_id"],
        roles={
            "member": RoleConfig(permissions=["*"], reach=Reach.OWN),
            "org_admin": RoleConfig(permissions=["*"], reach=Reach.TENANT),
        },
        default_role="member",
    )

    def authenticate(self, request: Request, response: Response, credential: Any) -> dict:
        user_id = request.headers.get("X-Test-User")
        if not user_id:
            raise HTTPException(status_code=401, detail="X-Test-User header required")
        claims: dict[str, Any] = {"user_id": user_id}
        if org := request.headers.get("X-Test-Org"):
            claims["org_id"] = org
        if role := request.headers.get("X-Test-Role"):
            claims["roles"] = [role]
        return claims


def _seed_cfg(thread_id: str, user_id: str, org_id: str, plan: str = "pro") -> dict[str, Any]:
    identity = Identity(user_id=user_id, tenant={"org_id": org_id}, claims={"plan": plan})
    return {"thread_id": thread_id, "user_id": user_id, "user": identity}


class TenantCheckpointer(InMemoryCheckpointer):
    """``InMemoryCheckpointer`` plus ``org_id`` (tenant) and ``plan`` (extra) columns."""

    def __init__(self) -> None:
        super().__init__()
        self.columns = parse_columns(["org_id"], ["plan"], table="threads")
        self.tenant_column_names = self.columns.tenant_names
        self.filter_schema = FilterSchema.for_threads(self.columns)
        self.rows: dict[str, dict[str, Any]] = {}
        self.list_calls: list[Any] = []

    async def aput_thread(self, config, thread_info):
        # Values come from config["user"] only and are set once, at creation.
        values = self.columns.resolve(config, write=True)
        row = {"user_id": str(config["user_id"]), **values}
        self.rows.setdefault(str(config["thread_id"]), row)
        return await super().aput_thread(config, thread_info)

    async def aget_thread_scope(self, thread_id):
        owner = await self.aget_thread_owner(thread_id)
        if owner is None:
            return None
        return {"user_id": owner, "org_id": self.rows.get(str(thread_id), {}).get("org_id")}

    def _visible(self, config, thread_id) -> bool:
        """Whether the row passes the caller's reach; a missing row is visible (brand new)."""
        filters = owner_filter(config, self.tenant_column_names)
        row = self.rows.get(str(thread_id))
        if filters is None or row is None:
            return True
        return all(row.get(column) == value for column, value in filters.items())

    async def aput_state(self, config, state):
        if not self._visible(config, config.get("thread_id")):
            raise PermissionError("thread belongs to another tenant")
        return await super().aput_state(config, state)

    async def aget_thread(self, config):
        if not self._visible(config, config.get("thread_id")):
            return None
        return await super().aget_thread(config)

    async def aget_state(self, config):
        if not self._visible(config, config.get("thread_id")):
            return None
        return await super().aget_state(config)

    async def aget_state_cache(self, config):
        if not self._visible(config, config.get("thread_id")):
            return None
        return await super().aget_state_cache(config)

    async def alist_messages(self, config, search=None, offset=None, limit=None):
        if not self._visible(config, config.get("thread_id")):
            return []
        return await super().alist_messages(config, search, offset, limit)

    async def aclean_thread(self, config):
        if not self._visible(config, config.get("thread_id")):
            return False
        return await super().aclean_thread(config)

    async def alist_threads(self, config, search=None, offset=None, limit=None, filters=None):
        self.list_calls.append(filters)
        conditions = validate_filters(filters, self.filter_schema)
        threads = await super().alist_threads(config, search, offset, limit)
        kept = []
        for thread in threads:
            row = self.rows.get(str(thread.thread_id), {})
            if not self._visible(config, thread.thread_id):
                continue
            if all(c.op == "eq" and row.get(c.column.name) == c.value for c in conditions):
                kept.append(thread)
        return kept


class TenantStore:
    """A store with ``org_id`` (tenant) and ``topic`` (extra) columns, like ``PgVectorStore``."""

    def __init__(self) -> None:
        self.columns = parse_columns(["org_id"], ["topic"], table="memories")
        self.tenant_column_names = self.columns.tenant_names
        self.filter_schema = FilterSchema.for_memories(self.columns)
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def _match(self, config, row) -> bool:
        filters = owner_filter(config, self.tenant_column_names)
        if filters is None:
            filters = {"user_id": str(config["user_id"])}
        return all(row[column] == value for column, value in filters.items())

    def _narrow(self, rows, filters):
        filters = filters or {}
        columns = {k: v for k, v in filters.items() if self.filter_schema.get(k) is not None}
        conditions = validate_filters(columns, self.filter_schema)
        return [r for r in rows if all(r.get(c.column.name) == c.value for c in conditions)]

    @staticmethod
    def _result(row) -> MemorySearchResult:
        return MemorySearchResult(
            id=row["id"], content=row["content"], user_id=row["user_id"], metadata={}
        )

    async def astore(self, config, content, memory_type=None, category=None, metadata=None, **kw):
        self.calls.append("astore")
        values = self.columns.resolve(config, write=True)
        memory_id = str(uuid4())
        self.rows[memory_id] = {
            "id": memory_id,
            "user_id": str(config["user_id"]),
            "content": str(content),
            **values,
            "topic": (metadata or {}).get("topic"),
        }
        return memory_id

    async def asearch(self, config, query, *, filters=None, limit=10, **kw):
        self.calls.append("asearch")
        rows = [r for r in self.rows.values() if self._match(config, r)]
        return [self._result(r) for r in self._narrow(rows, filters)][:limit]

    async def aget(self, config, memory_id, **kw):
        self.calls.append("aget")
        row = self.rows.get(memory_id)
        return self._result(row) if row and self._match(config, row) else None

    async def aget_all(self, config, limit=100, **kw):
        self.calls.append("aget_all")
        return [self._result(r) for r in self.rows.values() if self._match(config, r)][:limit]

    async def aupdate(self, config, memory_id, content, metadata=None, **kw):
        self.calls.append("aupdate")
        row = self.rows.get(memory_id)
        if not row or not self._match(config, row):
            return False
        row["content"] = str(content)
        return True

    async def adelete(self, config, memory_id, **kw):
        self.calls.append("adelete")
        row = self.rows.get(memory_id)
        if not row or not self._match(config, row):
            return False
        del self.rows[memory_id]
        return True

    async def aforget_memory(self, config, filters=None, **kw):
        self.calls.append("aforget_memory")
        gone = [
            r["id"] for r in self._narrow(self.rows.values(), filters) if self._match(config, r)
        ]
        for memory_id in gone:
            del self.rows[memory_id]
        return len(gone)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def _probe_router() -> APIRouter:
    """Stand-ins for invoke / stream: the real permission check, then the identity echoed."""
    probe = APIRouter()

    @probe.post("/v1/graph/invoke")
    async def _invoke(
        payload: dict, identity: Identity = Depends(RequirePermission("graph", "invoke"))
    ):
        return {"reach": identity.reach, "tenant": identity.tenant}

    return probe


def _app(*, checkpointer: Any = None, store: Any = None) -> tuple[Any, TenantCheckpointer]:
    checkpointer = checkpointer if checkpointer is not None else TenantCheckpointer()
    app = build_app(
        routers=[checkpointer_router, store_router, _probe_router()],
        auth=TenantAuth(),
        checkpointer=checkpointer,
        extra_bindings={BaseStore: store if store is not None else TenantStore()},
    )
    return make_client(app), checkpointer


def _as(user: str, org: str | None = None, role: str | None = None) -> dict[str, str]:
    headers = {"X-Test-User": user}
    if org:
        headers["X-Test-Org"] = org
    if role:
        headers["X-Test-Role"] = role
    return headers


ALICE_A = _as("alice", "org-a")
ALICE_B = _as("alice", "org-b")


def _seed_two_tenants(cp: TenantCheckpointer) -> None:
    """``alice`` owns ``t-a`` in org-a and ``t-b`` in org-b; ``bob`` owns ``t-bob`` in org-a."""
    for thread_id, user, org, plan in (
        ("t-a", "alice", "org-a", "pro"),
        ("t-b", "alice", "org-b", "pro"),
        ("t-bob", "bob", "org-a", "free"),
    ):
        cfg = _seed_cfg(thread_id, user, org, plan)
        anyio.run(cp.aput_thread, cfg, ThreadInfo(thread_id=thread_id, user_id=user))
        anyio.run(cp.aput_state, cfg, AgentState(context=[Message.text_message(f"hi {org}")]))
        anyio.run(cp.aput_messages, cfg, [Message.text_message(f"secret of {org}")])


def _listed(client, headers, **params) -> set[str]:
    resp = client.get("/v1/threads", headers=headers, params=params)
    assert resp.status_code == HTTP_OK, resp.text
    return {t["thread_id"] for t in resp.json()["data"]["threads"]}


# ---------------------------------------------------------------------------
# Threads: two tenants, the same user_id
# ---------------------------------------------------------------------------


def test_thread_listing_keeps_two_tenants_with_the_same_user_id_apart():
    client, cp = _app()
    _seed_two_tenants(cp)

    assert _listed(client, ALICE_A) == {"t-a"}
    assert _listed(client, ALICE_B) == {"t-b"}


def test_thread_reads_show_nothing_of_the_other_tenant():
    client, cp = _app()
    _seed_two_tenants(cp)

    def read(thread_id, path, headers):
        resp = client.get(f"/v1/threads/{thread_id}{path}", headers=headers)
        assert resp.status_code == HTTP_OK, resp.text
        return resp.json()["data"]

    assert read("t-a", "", ALICE_A)["thread_data"]["thread"] is not None
    assert read("t-a", "/state", ALICE_A)["state"] is not None
    assert read("t-a", "/messages", ALICE_A)["messages"]
    assert read("t-b", "", ALICE_A)["thread_data"]["thread"] is None
    assert read("t-b", "/state", ALICE_A)["state"] is None
    assert read("t-b", "/messages", ALICE_A)["messages"] == []
    assert read("t-a", "/state", ALICE_B)["state"] is None


def test_thread_writes_and_deletes_do_not_touch_the_other_tenant():
    client, cp = _app()
    _seed_two_tenants(cp)

    put = client.put("/v1/threads/t-b/state", json={"state": {}}, headers=ALICE_A)
    assert put.status_code != HTTP_OK
    client.request("DELETE", "/v1/threads/t-b", json={}, headers=ALICE_A)
    assert "t-b" in cp.rows
    assert _listed(client, ALICE_B) == {"t-b"}
    state = client.get("/v1/threads/t-b/state", headers=ALICE_B).json()["data"]["state"]
    assert state is not None


def test_a_caller_without_the_tenant_value_is_refused():
    """The auth config names org_id as a tenant claim; a token without one is a 401."""
    client, cp = _app()
    _seed_two_tenants(cp)

    resp = client.get("/v1/threads/t-a", headers=_as("alice"))
    assert resp.status_code == HTTP_UNAUTHORIZED


def test_org_admin_reach_covers_every_thread_of_the_org_only():
    client, cp = _app()
    _seed_two_tenants(cp)
    admin_a = _as("carol", "org-a", "org_admin")

    def state(thread_id):
        resp = client.get(f"/v1/threads/{thread_id}/state", headers=admin_a)
        return resp.json()["data"]["state"]

    assert state("t-a") is not None
    assert state("t-bob") is not None
    assert state("t-b") is None
    assert _listed(client, admin_a) == {"t-a", "t-bob"}

    seen = client.post("/v1/graph/invoke", json={}, headers=admin_a).json()
    assert seen["reach"] == "tenant"
    assert seen["tenant"] == {"org_id": "org-a"}


def test_a_member_has_reach_own():
    client, _ = _app()
    seen = client.post("/v1/graph/invoke", json={}, headers=ALICE_A).json()
    assert seen == {"reach": "own", "tenant": {"org_id": "org-a"}}


# ---------------------------------------------------------------------------
# Client config can never choose the tenant
# ---------------------------------------------------------------------------


FORGED_CONFIG = {
    "user": {"user_id": "alice", "tenant": {"org_id": "org-b"}, "reach": "all"},
    "org_id": "org-b",
}


def test_client_config_user_and_tenant_keys_do_not_reach_stored_thread_values():
    client, cp = _app()
    _seed_two_tenants(cp)

    resp = client.put(
        "/v1/threads/t-new/state",
        json={"config": FORGED_CONFIG, "state": {}},
        headers=ALICE_A,
    )
    assert resp.status_code == HTTP_OK, resp.text
    assert cp.rows["t-new"]["org_id"] == "org-a"
    # The forged values reached neither the stored row nor anyone's view of it.
    other = client.get("/v1/threads/t-new/state", headers=ALICE_B)
    assert other.json()["data"]["state"] is None
    assert _listed(client, ALICE_B) == {"t-b"}
    assert _listed(client, ALICE_A) == {"t-a", "t-new"}


def test_client_config_cannot_widen_a_read():
    client, cp = _app()
    _seed_two_tenants(cp)

    resp = client.put(
        "/v1/threads/t-a/state",
        json={"config": FORGED_CONFIG, "state": {}},
        headers=ALICE_A,
    )
    assert resp.status_code == HTTP_OK
    # The listing is driven by the identity alone.
    assert _listed(client, ALICE_A) == {"t-a"}


def test_client_config_tenant_keys_do_not_reach_the_store():
    store = TenantStore()
    client, _ = _app(store=store)

    resp = client.post(
        "/v1/store/memories",
        json={"content": "note", "config": FORGED_CONFIG},
        headers=ALICE_A,
    )
    assert resp.status_code == HTTP_OK, resp.text
    (row,) = store.rows.values()
    assert row["org_id"] == "org-a"
    found = client.post(
        "/v1/store/search", json={"query": "note", "config": FORGED_CONFIG}, headers=ALICE_B
    )
    assert found.status_code == HTTP_OK
    assert found.json()["data"]["results"] == []


# ---------------------------------------------------------------------------
# Store routes: two tenants, the same user_id
# ---------------------------------------------------------------------------


def test_store_routes_keep_two_tenants_with_the_same_user_id_apart():
    store = TenantStore()
    client, _ = _app(store=store)

    created = client.post("/v1/store/memories", json={"content": "a-note"}, headers=ALICE_A)
    memory_id = created.json()["data"]["memory_id"]

    def ids(resp) -> list[str]:
        data = resp.json()["data"]
        return [m["id"] for m in data.get("results", data.get("memories", []))]

    search = {"query": "note"}
    assert ids(client.post("/v1/store/search", json=search, headers=ALICE_A)) == [memory_id]
    assert ids(client.post("/v1/store/search", json=search, headers=ALICE_B)) == []
    assert ids(client.post("/v1/store/memories/list", json={}, headers=ALICE_B)) == []
    got = client.post(f"/v1/store/memories/{memory_id}", json={}, headers=ALICE_B)
    assert got.json()["data"]["memory"] is None
    upd = client.put(f"/v1/store/memories/{memory_id}", json={"content": "x"}, headers=ALICE_B)
    assert upd.json()["data"]["data"] is False
    gone = client.request("DELETE", f"/v1/store/memories/{memory_id}", json={}, headers=ALICE_B)
    assert gone.json()["data"]["data"] is False
    forget = client.post("/v1/store/memories/forget", json={}, headers=ALICE_B)
    assert forget.json()["data"]["data"] == 0
    assert list(store.rows) == [memory_id]
    assert store.rows[memory_id]["content"] == "a-note"


# ---------------------------------------------------------------------------
# filters on GET /v1/threads
# ---------------------------------------------------------------------------


def test_thread_filters_narrow_the_listing():
    client, cp = _app()
    _seed_two_tenants(cp)
    admin_a = _as("carol", "org-a", "org_admin")

    assert _listed(client, admin_a, filters=json.dumps({"plan": "pro"})) == {"t-a"}
    assert _listed(client, admin_a, filters=json.dumps({"plan": {"eq": "free"}})) == {"t-bob"}
    assert cp.list_calls[-1] == {"plan": {"eq": "free"}}
    assert _listed(client, admin_a, filters="") == {"t-a", "t-bob"}


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"org_id": "org-b"}, "isolation column"),
        ({"user_id": "alice"}, "isolation column"),
        ({"plan": {"gt": "a"}}, "not supported"),
        ({"nope": 1}, "Unknown filter column"),
        ({"plan": {"in": []}}, ""),
    ],
)
def test_bad_thread_filters_are_422_before_listing(filters, message):
    client, cp = _app()
    _seed_two_tenants(cp)

    resp = client.get("/v1/threads", headers=ALICE_A, params={"filters": json.dumps(filters)})
    assert resp.status_code == HTTP_UNPROCESSABLE, resp.text
    assert message in resp.json()["error"]["message"]
    assert cp.list_calls == []


@pytest.mark.parametrize(
    "raw", ["{not json", "[1, 2]", '"plan"', "NaN", '{"plan": NaN}', "[" * 5000 + "]" * 5000]
)
def test_unparseable_thread_filters_are_422(raw):
    client, cp = _app()

    resp = client.get("/v1/threads", headers=ALICE_A, params={"filters": raw})
    assert resp.status_code == HTTP_UNPROCESSABLE, resp.text
    assert "filters" in resp.json()["error"]["message"]
    assert "not json" not in resp.json()["error"]["message"]
    assert cp.list_calls == []


def test_thread_filters_need_a_checkpointer_with_filterable_columns():
    cp = InMemoryCheckpointer()
    client, _ = _app(checkpointer=cp)

    assert client.get("/v1/threads", headers=ALICE_A).status_code == HTTP_OK
    resp = client.get(
        "/v1/threads", headers=ALICE_A, params={"filters": json.dumps({"plan": "pro"})}
    )
    assert resp.status_code == HTTP_UNPROCESSABLE
    assert "InMemoryCheckpointer" in resp.json()["error"]["message"]


def test_thread_filter_messages_survive_production(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MODE", "production")
    monkeypatch.setattr(settings, "ORIGINS", "https://app.example.com")
    client, _ = _app()

    resp = client.get(
        "/v1/threads", headers=ALICE_A, params={"filters": json.dumps({"org_id": "x"})}
    )
    assert resp.status_code == HTTP_UNPROCESSABLE
    assert "isolation column" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# filters on the store routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/v1/store/search", {"query": "q", "filters": {"org_id": "org-b"}}),
        ("/v1/store/search", {"query": "q", "filters": {"user_id": "bob"}}),
        ("/v1/store/search", {"query": "q", "filters": {"topic": {"bogus": 1}}}),
        ("/v1/store/search", {"query": "q", "filters": {f"k{i}": i for i in range(11)}}),
        ("/v1/store/memories/forget", {"filters": {"org_id": "org-b"}}),
        ("/v1/store/memories/forget", {"filters": {"created_at": {"gte": "yesterday"}}}),
    ],
)
def test_bad_store_filters_are_422_before_the_store_runs(route, body):
    store = TenantStore()
    client, _ = _app(store=store)

    resp = client.post(route, json=body, headers=ALICE_A)
    assert resp.status_code == HTTP_UNPROCESSABLE, resp.text
    assert store.calls == []


def test_store_filters_on_columns_and_metadata_pass_through():
    store = TenantStore()
    client, _ = _app(store=store)
    for topic in ("billing", "support"):
        client.post(
            "/v1/store/memories",
            json={"content": topic, "metadata": {"topic": topic}},
            headers=ALICE_A,
        )

    resp = client.post(
        "/v1/store/search",
        json={"query": "q", "filters": {"topic": "billing", "source": "chat"}},
        headers=ALICE_A,
    )
    assert resp.status_code == HTTP_OK, resp.text
    assert [r["content"] for r in resp.json()["data"]["results"]] == ["billing"]


def test_a_filter_error_raised_by_the_store_is_a_422():
    class _Strict(TenantStore):
        async def asearch(self, config, query, **kw):
            raise FilterError("Unsupported filter on a metadata key")

    client, _ = _app(store=_Strict())
    resp = client.post(
        "/v1/store/search", json={"query": "q", "filters": {"x": [1]}}, headers=ALICE_A
    )
    assert resp.status_code == HTTP_UNPROCESSABLE
    assert "metadata key" in resp.json()["error"]["message"]


def test_a_store_without_a_filter_schema_keeps_its_own_filters():
    store = AsyncMock(spec=BaseStore)
    store.asearch.return_value = []
    client, _ = _app(store=store)

    resp = client.post(
        "/v1/store/search",
        json={"query": "q", "filters": {"org_id": "x", "user_id": "bob", "AND": [{"a": 1}]}},
        headers=ALICE_A,
    )
    assert resp.status_code == HTTP_OK, resp.text
    # Unchanged behaviour: reserved owner keys are dropped, everything else is forwarded.
    assert store.asearch.await_args.kwargs["filters"] == {"org_id": "x", "AND": [{"a": 1}]}
