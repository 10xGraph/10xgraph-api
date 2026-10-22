import importlib.util
import logging

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIWebSocketRoute

from .checkpointer.router import router as checkpointer_router
from .evals import router as evals_router
from .graph import router as graph_router
from .media.router import router as media_router
from .ping.router import router as ping_router
from .store import router as store_router


logger = logging.getLogger("tenxgraph_api")

AG_UI_INSTALL_HINT = 'pip install "10xgraph-api[ag-ui]"'
WEBSOCKET_INSTALL_HINT = 'pip install "10xgraph-api[websocket]"'


def _import_ag_ui_router() -> APIRouter:
    from .ag_ui import router

    return router


def _ag_ui_router() -> APIRouter:
    """Import the AG-UI router, which needs the optional ``ag-ui-protocol`` package.

    Imported only when ``ag_ui.enabled`` is set, so servers that leave it off never need the
    package installed.
    """
    try:
        return _import_ag_ui_router()
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "ag_ui":
            raise RuntimeError(
                "ag_ui is enabled in 10xgraph.json but the AG-UI SDK is not installed. "
                f"Install it with: {AG_UI_INSTALL_HINT}"
            ) from exc
        raise


def websocket_library_available() -> bool:
    """Uvicorn serves WebSockets only with ``websockets`` or ``wsproto`` installed."""
    return any(importlib.util.find_spec(name) is not None for name in ("websockets", "wsproto"))


def _graph_router(websocket: bool | None) -> APIRouter:
    """The graph router, without ``/v1/graph/ws`` and ``/v1/graph/live`` when disabled.

    ``websocket`` is ``websocket.enabled`` from 10xgraph.json: ``False`` drops both routes,
    ``True`` requires a WebSocket library, ``None`` (unset) keeps them and only warns when the
    library is missing, as servers did before the key existed.
    """
    if websocket is False:
        http_only = APIRouter()
        http_only.routes = [
            route for route in graph_router.routes if not isinstance(route, APIWebSocketRoute)
        ]
        return http_only

    if not websocket_library_available():
        message = (
            "WebSocket routes (/v1/graph/ws, /v1/graph/live) need the websockets package. "
            f"Install it with: {WEBSOCKET_INSTALL_HINT}, or set "
            '"websocket": {"enabled": false} in 10xgraph.json.'
        )
        if websocket is True:
            raise RuntimeError(f"websocket.enabled is true, but no WebSocket library. {message}")
        logger.warning(message)
    return graph_router


def init_routes(
    app: FastAPI, *, evals: bool = True, ag_ui: bool = False, websocket: bool | None = None
):
    """
    Initialize the routes for the FastAPI application.

    This function includes the graph and checkpointer routers for 10xGraph functionality.
    Auth and GraphQL routers are disabled for now.

    Args:
        app (FastAPI): The FastAPI application instance to which the routes
        will be added.
        evals (bool): Mount the evals report viewer. It has no auth, so production
        leaves it out.
        ag_ui (bool): Mount the AG-UI endpoint (``POST /v1/ag-ui``). Off unless
        ``ag_ui.enabled`` is set in 10xgraph.json.
        websocket (bool | None): ``websocket.enabled`` from 10xgraph.json. ``False`` leaves
        out ``/v1/graph/ws`` and ``/v1/graph/live``; see ``_graph_router``.
    """
    app.include_router(_graph_router(websocket))
    app.include_router(checkpointer_router)
    app.include_router(store_router)
    if evals:
        app.include_router(evals_router)
    if ag_ui:
        app.include_router(_ag_ui_router())
    app.include_router(ping_router)
    app.include_router(media_router)
