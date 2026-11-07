"""Live checkpointer API tests.

Drives the real ``CheckpointerService`` over a real ``InMemoryCheckpointer`` (no mocked
service), so these exercise the actual request -> service -> checkpointer path and the
server-side pagination cap.
"""

from __future__ import annotations

import pytest
from tenxgraph.core.authz import Identity
from tenxgraph.core.state import AgentState
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router

from .conftest import build_app, make_client, user_headers


HTTP_OK = 200
HTTP_UNPROCESSABLE = 422


@pytest.fixture
def seeded():
    """An app whose checkpointer already holds a thread for user 'alice'."""
    cp = InMemoryCheckpointer()
    owner = Identity(user_id="alice")
    cp.put_thread(
        {"thread_id": "t1", "user_id": "alice", "user": owner}, ThreadInfo(thread_id="t1")
    )
    app = build_app(routers=[checkpointer_router], checkpointer=cp)
    return app, cp


async def _seed_state(cp: InMemoryCheckpointer, thread_id: str, user_id: str) -> None:
    await cp.aput_state({"thread_id": thread_id, "user_id": user_id}, AgentState())


def test_get_state_for_existing_thread(seeded):
    import anyio

    app, cp = seeded
    anyio.run(_seed_state, cp, "t1", "alice")
    client = make_client(app)

    r = client.get("/v1/threads/t1/state", headers=user_headers("alice"))
    assert r.status_code == HTTP_OK


def test_list_messages_rejects_nonpositive_limit(seeded):
    app, _ = seeded
    client = make_client(app)

    r = client.get("/v1/threads/t1/messages?limit=0", headers=user_headers("alice"))
    assert r.status_code == HTTP_UNPROCESSABLE

    r_neg = client.get("/v1/threads/t1/messages?offset=-1", headers=user_headers("alice"))
    assert r_neg.status_code == HTTP_UNPROCESSABLE


def test_list_messages_accepts_huge_limit_without_error(seeded):
    """A huge limit must be clamped server-side, not forwarded verbatim (H3)."""
    app, _ = seeded
    client = make_client(app)

    r = client.get("/v1/threads/t1/messages?limit=100000000", headers=user_headers("alice"))
    assert r.status_code == HTTP_OK


def test_list_threads_rejects_nonpositive_limit(seeded):
    app, _ = seeded
    client = make_client(app)

    r = client.get("/v1/threads?limit=-5", headers=user_headers("alice"))
    assert r.status_code == HTTP_UNPROCESSABLE


def test_invalid_thread_id_is_422(seeded):
    app, _ = seeded
    client = make_client(app)

    r = client.get("/v1/threads/%20/state", headers=user_headers("alice"))
    assert r.status_code == HTTP_UNPROCESSABLE
