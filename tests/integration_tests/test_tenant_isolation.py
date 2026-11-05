"""Tenant isolation through the API: per-user isolation scopes, thread scopes and filters.

Two tenants hold a user with the same ``user_id`` (``alice`` in ``org-a`` and in ``org-b``).
Every route family must keep them apart, the tenant always coming from the authenticated
identity (``config["user"]``) and never from request config or ``filters``.

The checkpointer and store here are small in-memory stand-ins for ``PgCheckpointer`` /
``PgVectorStore`` with ``tenant_columns=["org_id"]``. They resolve values and isolation with
the real core helpers (``ColumnSet.resolve``, ``isolation_columns``, ``isolation_conditions``,
``validate_filters``), so what is tested is the API's side of the core contract.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.core.authz import build_authz, isolation_columns
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer
from tenxgraph.storage.columns import isolation_conditions, parse_columns
from tenxgraph.storage.sql_filter import FilterError, FilterSchema, validate_filters
from tenxgraph.storage.store import BaseStore, MemorySearchResult
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.auth.authorization import (
    AuthorizationBackend,
    OwnershipAuthorizationBackend,
)
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.config.setup_middleware import setup_middleware
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler
from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.store.router import router as store_router

from .conftest import make_client


HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_UNPROCESSABLE = 422


# ---------------------------------------------------------------------------
# Auth, authorization and storage stand-ins
# ---------------------------------------------------------------------------


class TenantAuth(BaseAuth):
    """Identity from headers: ``X-Test-User``, ``X-Test-Org`` and ``X-Test-Role``."""

    def authenticate(self, request: Request, response: Response, credential: Any) -> dict:
        user_id = request.headers.get("X-Test-User")
        if not user_id:
            raise HTTPException(status_code=401, detail="X-Test-User header required")
        user: dict[str, Any] = {"user_id": user_id}
        if org := request.headers.get("X-Test-Org"):
            user["org_id"] = org
        if role := request.headers.get("X-Test-Role"):
            user["role"] = role
        return user


class TenantAuthz(OwnershipAuthorizationBackend):
    """Owner scope for everyone; an org admin sees every thread and memory of the org."""

    def isolation_scope(self, user=None):
        if user and user.get("role") == "org_admin":
            return ["org_id"]
        return "owner"


class _AuthOn:
    def auth_config(self) -> str:
        return "custom"


def _seed_cfg(thread_id: str, user_id: str, org_id: str) -> dict[str, Any]:
    user = {"user_id": user_id, "org_id": org_id}
    user["authz"] = build_authz(user_id, scope="owner")
    return {"thread_id": thread_id, "user_id": user_id, "user": user}


class TenantCheckpointer(InMemoryCheckpointer):
    """``InMemoryCheckpointer`` plus ``org_id`` (tenant) and ``plan`` (extra) columns."""

    def __init__(self) -> None:
        super().__init__()
        self.columns = parse_columns(["org_id"], ["plan"], table="threads")
        self.tenant_column_names = self.columns.tenant_names
        self.filter_schema = FilterSchema.for_threads(self.columns)
        self.rows: dict[str, dict[str, Any]] = {}
        self.scope_lookups = 0
        self.list_calls: list[Any] = []

    async def aput_thread(self, config, thread_info):
        # Values come from config["user"] only and are set once, at creation.
        values = self.columns.resolve(config, write=True)
        self.rows.setdefault(str(config["thread_id"]), values)
        return await super().aput_thread(config, thread_info)

    async def aget_thread_scope(self, thread_id):
        self.scope_lookups += 1
        owner = await self.aget_thread_owner(thread_id)
        if owner is None:
            return None
        return {"user_id": owner, "org_id": self.rows.get(str(thread_id), {}).get("org_id")}

    def _visible(self, config, thread_id) -> bool:
        picked = isolation_columns(config, self.tenant_column_names)
        conditions = isolation_conditions(
            self.columns.scope_specs(picked), self.columns.resolve(config)
        )
        row = self.rows.get(str(thread_id), {})
        return all(row.get(spec.name) == value for spec, value in conditions)

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
        picked = isolation_columns(config, self.tenant_column_names)
        if picked is None:
            picked = ("user_id", *self.tenant_column_names)
        if "user_id" in picked and row["user_id"] != str(config.get("user_id")):
            return False
        conditions = isolation_conditions(
            self.columns.scope_specs(picked), self.columns.resolve(config)
        )
        return all(row[spec.name] == value for spec, value in conditions)

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
    """Stand-ins for invoke / stream: the real permission check, then the stamped authz."""
    probe = APIRouter()

    @probe.post("/v1/graph/invoke")
    async def _invoke(payload: dict, user: dict = Depends(RequirePermission("graph", "invoke"))):
        return {"authz": user.get("authz")}

    @probe.post("/v1/graph/stream")
    async def _stream(payload: dict, user: dict = Depends(RequirePermission("graph", "stream"))):
        return {"authz": user.get("authz")}

    return probe


def _app(
    *,
    checkpointer: Any = None,
    store: Any = None,
    authz: AuthorizationBackend | None = None,
) -> tuple[Any, TenantCheckpointer]:
    checkpointer = checkpointer if checkpointer is not None else TenantCheckpointer()
    app = FastAPI()
    setup_middleware(app)
    init_errors_handler(app)
    container = InjectQ()
    container.bind_instance(GraphConfig, _AuthOn())
    container.bind_instance(BaseAuth, TenantAuth())
    container.bind_instance(
        AuthorizationBackend,
        authz if authz is not None else TenantAuthz(checkpointer=checkpointer),
    )
    container.bind_instance(BaseCheckpointer, checkpointer)
    container.bind_instance(BaseStore, store if store is not None else TenantStore())
    setup_fastapi(container, app)
    for router in (checkpointer_router, store_router, _probe_router()):
        app.include_router(router)
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
        cfg = _seed_cfg(thread_id, user, org)
        cfg["user"]["plan"] = plan
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


@pytest.mark.parametrize(
    "path", ["/v1/threads/{t}", "/v1/threads/{t}/state", "/v1/threads/{t}/messages"]
)
def test_thread_reads_refuse_the_other_tenant(path):
    client, cp = _app()
    _seed_two_tenants(cp)

    assert client.get(path.format(t="t-a"), headers=ALICE_A).status_code == HTTP_OK
    assert client.get(path.format(t="t-b"), headers=ALICE_B).status_code == HTTP_OK
    assert client.get(path.format(t="t-b"), headers=ALICE_A).status_code == HTTP_FORBIDDEN
    assert client.get(path.format(t="t-a"), headers=ALICE_B).status_code == HTTP_FORBIDDEN


def test_thread_writes_and_deletes_refuse_the_other_tenant():
    client, cp = _app()
    _seed_two_tenants(cp)

    put = client.put("/v1/threads/t-b/state", json={"state": {}}, headers=ALICE_A)
    assert put.status_code == HTTP_FORBIDDEN
    delete = client.request("DELETE", "/v1/threads/t-b", json={}, headers=ALICE_A)
    assert delete.status_code == HTTP_FORBIDDEN
    assert "t-b" in cp.rows
    assert _listed(client, ALICE_B) == {"t-b"}


@pytest.mark.parametrize("route", ["/v1/graph/invoke", "/v1/graph/stream"])
def test_runs_refuse_a_thread_of_the_other_tenant(route):
    client, cp = _app()
    _seed_two_tenants(cp)

    assert client.post(route, json={"thread_id": "t-a"}, headers=ALICE_A).status_code == HTTP_OK
    assert (
        client.post(route, json={"thread_id": "t-b"}, headers=ALICE_A).status_code == HTTP_FORBIDDEN
    )
    assert (
        client.post(route, json={"config": {"thread_id": "t-a"}}, headers=ALICE_B).status_code
        == HTTP_FORBIDDEN
    )


def test_a_caller_without_the_tenant_value_is_refused():
    """The owner scope picks org_id; an identity without one never matches (fail closed)."""
    client, cp = _app()
    _seed_two_tenants(cp)

    assert client.get("/v1/threads/t-a", headers=_as("alice")).status_code == HTTP_FORBIDDEN


def test_org_admin_scope_reaches_every_thread_of_the_org_only():
    client, cp = _app()
    _seed_two_tenants(cp)
    admin_a = _as("carol", "org-a", "org_admin")

    assert client.get("/v1/threads/t-a/state", headers=admin_a).status_code == HTTP_OK
    assert client.get("/v1/threads/t-bob/messages", headers=admin_a).status_code == HTTP_OK
    assert client.get("/v1/threads/t-b/state", headers=admin_a).status_code == HTTP_FORBIDDEN
    assert _listed(client, admin_a) == {"t-a", "t-bob"}

    stamped = client.post("/v1/graph/invoke", json={}, headers=admin_a).json()["authz"]
    assert stamped["scope"] == ["org_id"]


def test_thread_scope_lookups_are_cached():
    client, cp = _app()
    _seed_two_tenants(cp)

    for _ in range(4):
        client.get("/v1/threads/t-a/state", headers=ALICE_A)
        client.get("/v1/threads/t-a/state", headers=ALICE_B)
    assert cp.scope_lookups == 1


# ---------------------------------------------------------------------------
# Client config can never choose the tenant
# ---------------------------------------------------------------------------


FORGED_CONFIG = {
    "user": {"user_id": "alice", "org_id": "org-b"},
    "org_id": "org-b",
    "authz": {"user_id": "alice", "scope": "none", "scopes": []},
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
    assert client.get("/v1/threads/t-new/state", headers=ALICE_B).status_code == HTTP_FORBIDDEN
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
    client, _ = _app(checkpointer=cp, authz=TenantAuthz(checkpointer=cp))

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


# ---------------------------------------------------------------------------
# isolation_scope: per-user form, old no-arg form, bad scopes
# ---------------------------------------------------------------------------


def test_old_no_arg_isolation_scope_backend_still_works():
    class _Legacy(OwnershipAuthorizationBackend):
        def isolation_scope(self):  # pre-1.0 signature: no user
            return "owner"

    cp = TenantCheckpointer()
    client, _ = _app(checkpointer=cp, authz=_Legacy(checkpointer=cp))
    _seed_two_tenants(cp)

    stamped = client.post("/v1/graph/invoke", json={}, headers=ALICE_A).json()["authz"]
    assert stamped["scope"] == "owner"
    assert client.get("/v1/threads/t-a/state", headers=ALICE_A).status_code == HTTP_OK
    assert client.get("/v1/threads/t-b/state", headers=ALICE_A).status_code == HTTP_FORBIDDEN
    assert _listed(client, ALICE_A) == {"t-a"}


def test_old_no_arg_isolation_scope_on_a_plain_backend_still_works():
    class _Shared(AuthorizationBackend):
        async def authorize(self, user, resource, action, resource_id=None, **ctx):
            return True

        def isolation_scope(self):
            return "none"

    client, _ = _app(authz=_Shared())
    stamped = client.post("/v1/graph/invoke", json={}, headers=ALICE_A).json()["authz"]
    assert stamped["scope"] == "none"


def test_isolation_scope_receives_the_authenticated_user():
    seen: list[dict] = []

    class _Recording(AuthorizationBackend):
        async def authorize(self, user, resource, action, resource_id=None, **ctx):
            return True

        def isolation_scope(self, user):
            seen.append(dict(user))
            return ["user_id", "org_id"]

    client, _ = _app(authz=_Recording())
    stamped = client.post("/v1/graph/invoke", json={}, headers=ALICE_A).json()["authz"]
    assert stamped["scope"] == ["user_id", "org_id"]
    assert seen[-1]["org_id"] == "org-a"


def test_a_malformed_scope_list_is_refused_not_turned_into_no_isolation():
    class _Broken(AuthorizationBackend):
        async def authorize(self, user, resource, action, resource_id=None, **ctx):
            return True

        def isolation_scope(self, user):
            return ["org id; drop"]

    client, _ = _app(authz=_Broken())
    assert client.get("/v1/threads", headers=ALICE_A).status_code == HTTP_FORBIDDEN


def test_a_scope_column_the_checkpointer_does_not_declare_denies_thread_access():
    class _Team(TenantAuthz):
        def isolation_scope(self, user=None):
            return ["team_id"]

    cp = TenantCheckpointer()
    client, _ = _app(checkpointer=cp, authz=_Team(checkpointer=cp))
    _seed_two_tenants(cp)

    assert client.get("/v1/threads/t-a/state", headers=ALICE_A).status_code == HTTP_FORBIDDEN


def test_a_checkpointer_that_cannot_answer_tenant_values_fails_closed():
    """Declares org_id but answers only the owner (the base aget_thread_scope)."""

    class _OwnerOnly(TenantCheckpointer):
        async def aget_thread_scope(self, thread_id):
            owner = await self.aget_thread_owner(thread_id)
            return None if owner is None else {"user_id": owner}

    cp = _OwnerOnly()
    client, _ = _app(checkpointer=cp, authz=TenantAuthz(checkpointer=cp))
    _seed_two_tenants(cp)

    assert client.get("/v1/threads/t-a/state", headers=ALICE_A).status_code == HTTP_FORBIDDEN
