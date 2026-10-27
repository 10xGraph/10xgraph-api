"""Unit tests for the Graph API router endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.routers.graph.router import (
    fix_graph,
    graph_details,
    invoke_graph,
    state_schema,
    stop_graph,
    stream_graph,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    FixGraphRequestSchema,
    GraphInputSchema,
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
    service.check_media_urls = MagicMock()
    return service


@pytest.fixture
def mock_user():
    return {"user_id": "user-123", "role": "admin"}


@pytest.mark.asyncio
async def test_invoke_graph_endpoint(mock_request, mock_service, mock_user):
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    mock_service.invoke_graph.return_value = {"messages": []}

    with patch("tenxgraph_api.src.app.routers.graph.router.success_response") as mock_success:
        mock_success.return_value = {"status": "success"}
        res = await invoke_graph(
            request=mock_request,
            graph_input=graph_input,
            service=mock_service,
            user=mock_user,
        )
        assert res == {"status": "success"}
        mock_service.invoke_graph.assert_called_once_with(graph_input, mock_user)
        mock_success.assert_called_once()


@pytest.mark.asyncio
async def test_stream_graph_endpoint(mock_service, mock_user):
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )

    mock_stream = MagicMock()
    mock_service.stream_graph.return_value = mock_stream

    with patch(
        "tenxgraph_api.src.app.routers.graph.router.StreamingResponse"
    ) as mock_streaming_response:
        mock_streaming_response.return_value = "streaming_response_obj"
        res = await stream_graph(
            graph_input=graph_input,
            service=mock_service,
            user=mock_user,
        )
        assert res == "streaming_response_obj"
        mock_service.stream_graph.assert_called_once_with(graph_input, mock_user)
        mock_streaming_response.assert_called_once()


@pytest.mark.asyncio
async def test_graph_details_endpoint(mock_request, mock_service, mock_user):
    mock_service.graph_details.return_value = {"info": {}}

    with patch("tenxgraph_api.src.app.routers.graph.router.success_response") as mock_success:
        mock_success.return_value = {"status": "success"}
        res = await graph_details(
            request=mock_request,
            service=mock_service,
            user=mock_user,
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
            user=mock_user,
        )
        assert res == {"status": "success"}
        mock_service.get_state_schema.assert_called_once()


@pytest.mark.asyncio
async def test_stop_graph_endpoint(mock_request, mock_service, mock_user):
    stop_req = GraphStopSchema(thread_id="thread-abc", config={"force": True})
    mock_service.stop_graph.return_value = {"status": "stopped"}

    with patch("tenxgraph_api.src.app.routers.graph.router.success_response") as mock_success:
        mock_success.return_value = {"status": "success"}
        res = await stop_graph(
            request=mock_request,
            stop_request=stop_req,
            service=mock_service,
            user=mock_user,
        )
        assert res == {"status": "success"}
        mock_service.stop_graph.assert_called_once_with("thread-abc", mock_user, {"force": True})


@pytest.mark.asyncio
async def test_fix_graph_endpoint(mock_request, mock_service, mock_user):
    fix_req = FixGraphRequestSchema(thread_id="thread-abc", config={"clean": True})
    mock_service.fix_graph.return_value = {"status": "fixed"}

    with patch("tenxgraph_api.src.app.routers.graph.router.success_response") as mock_success:
        mock_success.return_value = {"status": "success"}
        res = await fix_graph(
            request=mock_request,
            fix_request=fix_req,
            service=mock_service,
            user=mock_user,
        )
        assert res == {"status": "success"}
        mock_service.fix_graph.assert_called_once_with("thread-abc", mock_user, {"clean": True})


FORGED_THREAD_ID = "x\n2026-10-26 ERROR admin login ok"
ROUTER_MODULE = "tenxgraph_api.src.app.routers.graph.router"


async def _call_thread_endpoints(which, request, service, user):
    from tenxgraph_api.src.app.routers.graph.router import observability

    if "observability" in which:
        await observability(
            request=request, thread_id=FORGED_THREAD_ID, run_id=None, service=service, user=user
        )
    if "stop" in which:
        await stop_graph(
            request=request,
            stop_request=GraphStopSchema.model_construct(thread_id=FORGED_THREAD_ID, config={}),
            service=service,
            user=user,
        )
    if "fix" in which:
        await fix_graph(
            request=request,
            fix_request=FixGraphRequestSchema.model_construct(
                thread_id=FORGED_THREAD_ID, config={}
            ),
            service=service,
            user=user,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["observability", "stop", "fix"])
async def test_client_thread_id_is_escaped_in_development_logs(
    endpoint, mock_request, mock_service, mock_user, caplog
):
    caplog.set_level("INFO")
    with (
        patch(f"{ROUTER_MODULE}._is_production", return_value=False),
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
        patch(f"{ROUTER_MODULE}._is_production", return_value=True),
        patch(f"{ROUTER_MODULE}.success_response"),
    ):
        await _call_thread_endpoints(
            ["observability", "stop", "fix"], mock_request, mock_service, mock_user
        )

    assert "ERROR admin login ok" not in caplog.text
    assert "thread" not in caplog.text
