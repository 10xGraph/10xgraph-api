"""App assembly smoke tests.

Prove the app wires up (middleware + routers + DI) and a public route serves, which the
old fully-commented module did not.
"""

from __future__ import annotations

from tenxgraph_api.src.app.routers.checkpointer.router import router as checkpointer_router
from tenxgraph_api.src.app.routers.ping.router import router as ping_router

from .conftest import build_app, make_client


HTTP_OK = 200




def test_protected_and_public_routers_coexist():
    app = build_app(routers=[ping_router, checkpointer_router])
    client = make_client(app)
    # Public route open, protected route requires identity.
    assert client.get("/ping").status_code == HTTP_OK
    assert client.get("/v1/threads/t1/state").status_code == 401
