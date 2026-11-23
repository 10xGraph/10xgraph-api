"""Model registry choices: graph info, the early check, and the production 422."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from tenxgraph.core.exceptions import ModelNotFoundError

from tenxgraph_api.src.app.core.config.setup_middleware import RequestIDMiddleware
from tenxgraph_api.src.app.core.exceptions import handle_errors
from tenxgraph_api.src.app.core.validation.request_config import (
    client_config,
    client_error_detail,
)
from tenxgraph_api.src.app.routers.graph.router import stream_graph
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema
from tenxgraph_api.src.app.routers.graph.services.graph_service import (
    GraphService,
    _stream_error_data,
)


INFO = {
    "node_count": 1,
    "edge_count": 0,
    "checkpointer": False,
    "publisher": False,
    "store": False,
}
MODELS = {"MAIN": {"default": "gpt", "names": ["gpt", "vllm"], "config_key": "model"}}
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def make_service(models=None):
    graph = MagicMock()
    info = dict(INFO)
    if models is not None:
        info["models"] = models
    graph.generate_graph.return_value = {"info": info, "nodes": [], "edges": []}

    class FakeState(BaseModel):
        a: int = 0

    graph._state = FakeState
    srv = GraphService.__new__(GraphService)
    srv._graph = graph
    srv.checkpointer = MagicMock()
    srv.checkpointer.aput_thread = AsyncMock(return_value=True)
    srv.config = MagicMock()
    srv.config.runs = None
    srv.thread_name_generator = None
    srv._media_service = None
    srv._telemetry = None
    return srv


def test_client_config_passes_model_keys():
    assert client_config({"model": "vllm", "llm": "x"}) == {"model": "vllm", "llm": "x"}


@pytest.mark.asyncio
async def test_graph_info_includes_models():
    res = await make_service(MODELS).graph_details()
    choice = res.info.models["MAIN"]
    assert (choice.default, choice.names, choice.config_key) == ("gpt", ["gpt", "vllm"], "model")


@pytest.mark.asyncio
async def test_graph_info_models_empty_by_default():
    assert (await make_service().graph_details()).info.models == {}


def test_known_name_passes():
    make_service(MODELS).check_model_choice({"model": "vllm"})


def test_absent_key_passes():
    make_service(MODELS).check_model_choice({"plan": "pro"})


def test_no_models_passes_anything():
    make_service().check_model_choice({"model": "whatever"})


def test_unknown_name_message_matches_core():
    with pytest.raises(ModelNotFoundError) as exc:
        make_service(MODELS).check_model_choice({"model": "nope"})
    assert str(exc.value) == (
        "model 'nope' is not registered on agent node 'MAIN'; registered: gpt, vllm"
    )


@pytest.mark.parametrize("value", [5, ["gpt"], {"a": 1}])
def test_non_string_rejected(value):
    with pytest.raises(ModelNotFoundError):
        make_service(MODELS).check_model_choice({"model": value})


def test_none_means_default():
    # Core treats None as the node's default model; the API agrees.
    make_service(MODELS).check_model_choice({"model": None})


@pytest.mark.asyncio
async def test_ws_unknown_name_is_error_chunk_before_run():
    import json
    from unittest.mock import AsyncMock, MagicMock

    from tenxgraph_api.src.app.routers.graph.router import _ws_media_urls_allowed

    svc = make_service(MODELS)
    svc.check_media_urls = MagicMock()
    websocket = MagicMock()
    websocket.send_text = AsyncMock()
    graph_input = MagicMock(messages=[], config={"model": "nope"})

    assert await _ws_media_urls_allowed(websocket, svc, graph_input) is None
    sent = json.loads(websocket.send_text.await_args.args[0])
    assert sent["event"] == "error"
    assert "not registered" in sent["data"]["reason"]


def test_custom_config_key():
    models = {"MAIN": {"default": "a", "names": ["a"], "config_key": "llm"}}
    svc = make_service(models)
    svc.check_model_choice({"llm": "a", "model": "free"})
    with pytest.raises(ModelNotFoundError):
        svc.check_model_choice({"llm": "b"})


@pytest.mark.asyncio
async def test_invoke_unknown_name_is_422():
    from fastapi import HTTPException

    svc = make_service(MODELS)
    gi = GraphInputSchema(messages=MESSAGES, config={"model": "nope"})
    with pytest.raises(HTTPException) as exc:
        await svc.invoke_graph(gi, None)
    assert exc.value.status_code == 422
    assert "not registered on agent node 'MAIN'" in exc.value.detail


@pytest.mark.asyncio
async def test_stream_router_rejects_before_streaming():
    svc = make_service(MODELS)
    svc.check_media_urls = MagicMock()
    svc.stream_graph = MagicMock()
    gi = GraphInputSchema(messages=MESSAGES, config={"model": "nope"})
    with pytest.raises(ModelNotFoundError):
        await stream_graph(graph_input=gi, service=svc, identity=None)
    svc.stream_graph.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_input_rejects_unknown_name():
    svc = make_service(MODELS)
    gi = GraphInputSchema(messages=MESSAGES, config={"model": "nope"})
    with pytest.raises(ModelNotFoundError):
        await svc._prepare_input(gi)


def test_production_keeps_model_message(monkeypatch):
    settings = MagicMock(MODE="production")
    monkeypatch.setattr("tenxgraph_api.src.app.core.config.settings.get_settings", lambda: settings)
    exc = ModelNotFoundError("x", "MAIN", ["a", "b"])
    assert client_error_detail(exc) == str(exc)
    assert client_error_detail(ValueError("secret")) == "Invalid input provided."
    assert _stream_error_data(exc)["reason"] == str(exc)


def test_error_handler_keeps_message_in_production(monkeypatch):
    settings = MagicMock(MODE="production")
    monkeypatch.setattr("tenxgraph_api.src.app.core.config.settings.get_settings", lambda: settings)
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    handle_errors.init_errors_handler(app)

    @app.get("/boom")
    async def boom():
        raise ModelNotFoundError("x", "MAIN", ["a", "b"])

    res = TestClient(app).get("/boom")
    assert res.status_code == 422
    assert "model 'x' is not registered on agent node 'MAIN'" in res.text
