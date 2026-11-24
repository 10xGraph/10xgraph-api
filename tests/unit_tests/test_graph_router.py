"""Unit tests for the Graph API router endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.routers.graph.router import (
    fix_graph,
    graph_details,
    state_schema,
    stop_graph,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    FixGraphRequestSchema,
    GraphStopSchema,
)


@pytest.fixture
def mock_request():
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.state.request_id = "test-request-id"
    request.state.timestamp = "2024-01-01T00:00:00Z"
    return request


@pytest.fixture
def mock_service():
    service = AsyncMock()
    # stream_graph returns an async generator/iterable, not a direct awaitable coroutine
    # but we can mock it as returning an async iterable or mock object
    service.stream_graph = MagicMock()
    service.runs_policy = MagicMock(return_value=RunsConfig())
    service.check_run_input = MagicMock()
    return service


@pytest.fixture
def mock_user():
    return Identity(user_id="user-123", roles=frozenset({"admin"}))






@pytest.mark.asyncio
async def test_graph_details_endpoint(mock_request, mock_service, mock_user):
    mock_service.graph_details.return_value = {"info": {}}

    with patch("tenxgraph_api.src.app.routers.graph.router.success_response") as mock_success:
        mock_success.return_value = {"status": "success"}
        res = await graph_details(
            request=mock_request,
            service=mock_service,
            identity=mock_user,
        )
        assert res == {"status": "success"}
        mock_service.graph_details.assert_called_once()


@pytest.mark.asyncio
async def test_state_schema_endpoint(mock_request, mock_service, mock_user):
    mock_service.get_state_schema.return_value = {"schema": {}}

    with patch("tenxgraph_api.src.app.routers.graph.router.success_response") as mock_success:
        mock_success.return_value = {"status": "success"}
        res = await state_schema(
            request=mock_request,
            service=mock_service,
            identity=mock_user,
        )
        assert res == {"status": "success"}
        mock_service.get_state_schema.assert_called_once()






FORGED_THREAD_ID = "x\n2026-10-26 ERROR admin login ok"
ROUTER_MODULE = "tenxgraph_api.src.app.routers.graph.router"


async def _call_thread_endpoints(which, request, service, user):
    from tenxgraph_api.src.app.routers.graph.router import observability

    if "observability" in which:
        await observability(
            request=request, thread_id=FORGED_THREAD_ID, run_id=None, service=service, identity=user
        )
    if "stop" in which:
        await stop_graph(
            request=request,
            stop_request=GraphStopSchema.model_construct(thread_id=FORGED_THREAD_ID, config={}),
            service=service,
            identity=user,
        )
    if "fix" in which:
        await fix_graph(
            request=request,
            fix_request=FixGraphRequestSchema.model_construct(
                thread_id=FORGED_THREAD_ID, config={}
            ),
            service=service,
            identity=user,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["observability", "stop", "fix"])
async def test_client_thread_id_is_escaped_in_development_logs(
    endpoint, mock_request, mock_service, mock_user, caplog
):
    caplog.set_level("INFO")
    with (
        patch(f"{ROUTER_MODULE}.is_production", return_value=False),
        patch(f"{ROUTER_MODULE}.success_response"),
    ):
        await _call_thread_endpoints([endpoint], mock_request, mock_service, mock_user)

    assert "\\n2026-10-26 ERROR admin login ok" in caplog.text
    assert all("\n2026" not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_client_thread_id_lines_not_emitted_in_production(
    mock_request, mock_service, mock_user, caplog
):
    caplog.set_level("DEBUG")
    with (
        patch(f"{ROUTER_MODULE}.is_production", return_value=True),
        patch(f"{ROUTER_MODULE}.success_response"),
    ):
        await _call_thread_endpoints(
            ["observability", "stop", "fix"], mock_request, mock_service, mock_user
        )

    assert "ERROR admin login ok" not in caplog.text
    assert "thread" not in caplog.text
