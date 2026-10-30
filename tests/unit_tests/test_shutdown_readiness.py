"""G-93 / G-108: shutdown grace periods, draining readiness, boot cleanup bound, final chunk."""

# ruff: noqa: S101

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from injectq import InjectQ
from injectq.integrations.fastapi import setup_fastapi
from tenxgraph.storage.checkpointer import BaseCheckpointer, InMemoryCheckpointer

from tenxgraph_api.cli.templates import defaults
from tenxgraph_api.src.app import uvicorn_worker
from tenxgraph_api.src.app.core import shutdown
from tenxgraph_api.src.app.routers.ping import readiness
from tenxgraph_api.src.app.routers.ping.router import router as ping_router


# ---------------------------------------------------------------- build templates


def test_container_grace_exceeds_gunicorn_graceful_timeout():
    graceful = defaults.GRACEFUL_TIMEOUT_SECONDS
    compose = defaults.generate_docker_compose_content("svc", 8000)
    match = re.search(r"stop_grace_period: (\d+)s", compose)
    assert match
    assert int(match.group(1)) > graceful
    assert (
        graceful + defaults.K8S_PRESTOP_SLEEP_SECONDS + 30
    ) <= defaults.K8S_TERMINATION_GRACE_SECONDS


def test_uvicorn_gives_up_before_gunicorn_kills_the_worker():
    uv = uvicorn_worker.TenxGraphUvicornWorker.CONFIG_KWARGS["timeout_graceful_shutdown"]
    assert uv == defaults.UVICORN_GRACEFUL_SHUTDOWN_SECONDS
    assert uv + 30 < defaults.GRACEFUL_TIMEOUT_SECONDS  # room for the lifespan drain


def test_dockerfile_and_compose_use_the_draining_worker():
    worker = "tenxgraph_api.src.app.uvicorn_worker.TenxGraphUvicornWorker"
    assert worker in defaults.generate_docker_compose_content("svc", 8000)
    assert worker in defaults.generate_dockerfile_content(
        "3.13", 8000, "requirements.txt", has_requirements=False, has_pyproject=True
    )


# ---------------------------------------------------------------- readiness


def _client(monkeypatch, draining=False):
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: SimpleNamespace(REDIS_URL=None))
    container = InjectQ()
    container.bind_instance(BaseCheckpointer, InMemoryCheckpointer())
    app = FastAPI()
    setup_fastapi(container, app)
    app.include_router(ping_router)
    app.state.draining = draining
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh():
    readiness.reset_cache()
    shutdown.reset()
    yield
    readiness.reset_cache()
    shutdown.reset()


def test_draining_app_is_not_ready(monkeypatch):
    assert _client(monkeypatch).get("/health/ready").status_code == 200
    resp = _client(monkeypatch, draining=True).get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "draining"


def test_redis_outage_alone_stays_ready_unless_a_feature_fails_closed():
    checks = {"checkpointer": "ok", "redis": "fail"}
    assert readiness.is_ready(checks, redis_is_required=False)
    assert not readiness.is_ready(checks, redis_is_required=True)
    assert not readiness.is_ready({"checkpointer": "fail", "redis": "ok"}, redis_is_required=False)
    assert readiness.redis_required(SimpleNamespace(token_revocation="redis", rate_limit=None))
    assert readiness.redis_required(
        SimpleNamespace(
            token_revocation=None, rate_limit=SimpleNamespace(backend="redis", fail_open=False)
        )
    )
    assert not readiness.redis_required(
        SimpleNamespace(
            token_revocation="memory", rate_limit=SimpleNamespace(backend="redis", fail_open=True)
        )
    )


# ---------------------------------------------------------------- boot cleanup


@pytest.mark.asyncio
async def test_boot_cleanup_is_bounded(monkeypatch):
    from tenxgraph_api.src.app import main

    monkeypatch.setattr(main, "BOOT_CLEANUP_TIMEOUT_SECONDS", 0.05)

    async def hang():
        await asyncio.sleep(30)

    monkeypatch.setattr(main, "_cleanup_temp_media_cache", hang)
    monkeypatch.setattr(main, "attach_all_modules", lambda *a, **k: _none())
    app = FastAPI()
    async with asyncio.timeout(5):
        async with main.lifespan(app):
            assert not getattr(app.state, "draining", False)
    assert app.state.draining is True


async def _none():
    return None


# ---------------------------------------------------------------- final chunk


async def _slow_stream():
    yield '{"event":"message"}\n'
    await asyncio.sleep(30)
    yield "never\n"


@pytest.mark.asyncio
async def test_stream_ends_with_an_interrupted_chunk_on_shutdown():
    lines = []

    async def consume():
        async for line in shutdown.end_on_shutdown(_slow_stream()):
            lines.append(line)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    shutdown.interrupt_streams()
    await asyncio.wait_for(task, 2)
    assert lines[0] == '{"event":"message"}\n'
    final = json.loads(lines[-1])
    assert final["event"] == "error"
    assert final["data"]["interrupted"] is True
    assert "resume" in final["data"]["reason"]
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_stream_closes_the_run_when_interrupted():
    closed = asyncio.Event()

    async def stream():
        try:
            yield "a\n"
            await asyncio.sleep(30)
        finally:
            closed.set()

    task = asyncio.create_task(_drain(shutdown.end_on_shutdown(stream())))
    await asyncio.sleep(0.05)
    shutdown.interrupt_streams()
    await asyncio.wait_for(task, 2)
    assert closed.is_set()


async def _drain(gen):
    return [line async for line in gen]


@pytest.mark.asyncio
async def test_normal_stream_is_unchanged():
    async def stream():
        yield "a\n"
        yield "b\n"

    assert await _drain(shutdown.end_on_shutdown(stream())) == ["a\n", "b\n"]


@pytest.mark.asyncio
async def test_http_stream_graph_ends_with_the_interrupted_chunk():
    from tenxgraph.core.state import StreamChunk, StreamEvent

    from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

    async def chunks(self, graph_input, user, server_config=None):
        yield StreamChunk(event=StreamEvent.UPDATES, data={"status": "started"})
        await asyncio.sleep(30)

    service = object.__new__(GraphService)
    service.stream_chunks = chunks.__get__(service)
    lines = []

    async def consume():
        async for line in service.stream_graph(None, {}):
            lines.append(line)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    shutdown.interrupt_streams()
    await asyncio.wait_for(task, 2)
    assert json.loads(lines[-1])["data"]["interrupted"] is True


@pytest.mark.asyncio
async def test_worker_server_flags_draining_and_interrupts_streams_before_deadline(monkeypatch):
    from uvicorn.config import Config
    from uvicorn.server import Server

    monkeypatch.setattr(uvicorn_worker, "STREAM_INTERRUPT_MARGIN_SECONDS", 0.0)
    seen = {}

    async def fake_shutdown(self, sockets=None):
        seen["draining"] = self.app_state.draining
        await asyncio.sleep(0.1)
        seen["interrupted"] = shutdown._interrupted

    monkeypatch.setattr(Server, "shutdown", fake_shutdown)
    state = SimpleNamespace(draining=False)
    server = uvicorn_worker.DrainingServer(Config(app=None, timeout_graceful_shutdown=0.05))
    server.app_state = state
    await server.shutdown()
    assert seen == {"draining": True, "interrupted": True}
