"""Graph service/route tests.

The full invoke path needs a compiled graph; these cover the two things that do not:
auth enforcement on the graph routes, and the B2 regression -- a client cannot override
the authenticated ``user_id`` through the request-body ``config``.
"""

from __future__ import annotations

from typing import Any

import anyio
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

from .conftest import _AuthOnConfig


class _RecordingCheckpointer:
    """Captures the config handed to ``aget_state`` and reports no state (short-circuit)."""

    def __init__(self) -> None:
        self.seen_config: dict[str, Any] | None = None

    async def aget_state(self, config: dict[str, Any]):
        self.seen_config = config


def _service(checkpointer) -> GraphService:
    # graph and thread_name_generator are unused on the fix short-circuit path.
    return GraphService(
        graph=object(),  # type: ignore[arg-type]
        checkpointer=checkpointer,  # type: ignore[arg-type]
        config=_AuthOnConfig(),  # type: ignore[arg-type]
    )


def test_fix_graph_ignores_client_supplied_user_id():
    """B2: request-body config must NOT override the authenticated identity."""
    cp = _RecordingCheckpointer()
    service = _service(cp)

    anyio.run(
        service.fix_graph,
        "thread-A",
        Identity(user_id="alice"),  # authenticated user
        {"user_id": "victim", "thread_id": "thread-A"},  # attacker-controlled config
    )

    assert cp.seen_config is not None
    assert cp.seen_config["user_id"] == "alice"  # trusted identity won, not "victim"


def test_stop_graph_sets_trusted_user_id_over_client_config():
    """B2 companion: stop_graph must also overlay the trusted user_id last."""

    class _StopGraph:
        def __init__(self) -> None:
            self.seen_config: dict[str, Any] | None = None

        async def astop(self, config: dict[str, Any]):
            self.seen_config = config
            return {"stopped": True}

    graph = _StopGraph()
    service = GraphService(
        graph=graph,  # type: ignore[arg-type]
        checkpointer=_RecordingCheckpointer(),  # type: ignore[arg-type]
        config=_AuthOnConfig(),  # type: ignore[arg-type]
    )

    anyio.run(
        service.stop_graph,
        "thread-A",
        Identity(user_id="alice"),
        {"user_id": "victim"},
    )

    assert graph.seen_config is not None
    assert graph.seen_config["user_id"] == "alice"
