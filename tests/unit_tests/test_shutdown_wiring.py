"""The lifespan hands shutdown to core ``graph.aclose`` (plan Batch F)."""

# ruff: noqa: S101

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tenxgraph.core.exceptions import GraphClosedError

from tenxgraph_api.src.app import main as app_main
from tenxgraph_api.src.app.core.exceptions.handle_errors import init_errors_handler


def _graph(order: list[str]) -> AsyncMock:
    graph = AsyncMock()

    async def aclose(**kwargs):
        order.append("aclose")
        graph.kwargs = kwargs
        return {"runs": {}}

    graph.aclose = AsyncMock(side_effect=aclose)
    return graph


@pytest.mark.asyncio
async def test_lifespan_calls_aclose_with_the_configured_times():
    order: list[str] = []
    graph = _graph(order)
    with (
        patch.object(app_main, "attach_all_modules", AsyncMock(return_value=graph)),
        patch.object(app_main, "_cleanup_temp_media_cache", AsyncMock()),
        patch.object(app_main.settings, "SHUTDOWN_GRACE_SECONDS", 3.0),
        patch.object(app_main.settings, "SHUTDOWN_STOP_SECONDS", 2.0),
        patch.object(app_main.settings, "SHUTDOWN_TIMEOUT_SECONDS", 7.0),
    ):
        app = FastAPI()
        async with app_main.lifespan(app):
            pass
    graph.aclose.assert_awaited_once_with(grace=3.0, stop_timeout=2.0, timeout=7.0)
    assert app.state.draining is True


@pytest.mark.asyncio
async def test_sweeper_is_cancelled_before_the_graph_closes():
    order: list[str] = []
    graph = _graph(order)

    async def sweep_loop(interval):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("sweeper_cancelled")
            raise

    with (
        patch.object(app_main, "attach_all_modules", AsyncMock(return_value=graph)),
        patch.object(app_main, "_cleanup_temp_media_cache", AsyncMock()),
        patch.object(app_main, "_sweep_media_loop", sweep_loop),
    ):
        async with app_main.lifespan(FastAPI()):
            await asyncio.sleep(0)  # let the sweeper start
    assert order == ["sweeper_cancelled", "aclose"]


@pytest.mark.asyncio
async def test_a_failing_close_does_not_stop_the_rest_of_shutdown():
    graph = AsyncMock()
    graph.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(app_main, "attach_all_modules", AsyncMock(return_value=graph)),
        patch.object(app_main, "_cleanup_temp_media_cache", AsyncMock()),
    ):
        async with app_main.lifespan(FastAPI()):
            pass  # exiting must not raise


def test_graph_closed_error_is_503_with_retry_after():
    app = FastAPI()
    init_errors_handler(app)

    @app.get("/run")
    async def run():
        raise GraphClosedError()

    res = TestClient(app).get("/run")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert int(res.headers["Retry-After"]) >= 1
