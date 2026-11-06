"""``--health-port`` probes: /healthz and /readyz."""

from __future__ import annotations

import asyncio
import json

import pytest

from tenxgraph_api.jobs.health import HealthServer


pytestmark = pytest.mark.asyncio


async def get(port: int, path: str, method: str = "GET") -> tuple[int, dict | None]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, (json.loads(body) if body else None)


async def test_probe_answers():
    state = {"alive": True, "ready": True}

    async def ready() -> bool:
        if state["ready"] == "raise":
            raise ConnectionError("store down")
        if state["ready"] == "hang":
            await asyncio.sleep(10)
        return bool(state["ready"])

    server = HealthServer(alive=lambda: state["alive"], ready=ready, host="127.0.0.1", port=0)
    port = await server.start()
    try:
        assert await get(port, "/healthz") == (200, {"status": "ok"})
        assert await get(port, "/readyz") == (200, {"status": "ok"})
        assert (await get(port, "/healthz?x=1"))[0] == 200
        assert (await get(port, "/nope"))[0] == 404
        assert (await get(port, "/healthz", "POST"))[0] == 405
        assert await get(port, "/healthz", "HEAD") == (200, None)
        state["ready"] = False
        assert await get(port, "/readyz") == (503, {"status": "unavailable"})
        state["ready"] = "raise"
        assert (await get(port, "/readyz"))[0] == 503
        state["alive"] = False
        assert (await get(port, "/healthz"))[0] == 503
    finally:
        await server.aclose()


async def test_readiness_times_out(monkeypatch):
    from tenxgraph_api.jobs import health

    monkeypatch.setattr(health, "PROBE_TIMEOUT", 0.05)

    async def hang() -> bool:
        await asyncio.sleep(10)
        return True

    server = HealthServer(alive=lambda: True, ready=hang, host="127.0.0.1", port=0)
    port = await server.start()
    try:
        assert (await get(port, "/readyz"))[0] == 503
    finally:
        await server.aclose()


async def test_garbage_request_is_dropped():
    server = HealthServer(alive=lambda: True, ready=lambda: asyncio.sleep(0, True), host="127.0.0.1")
    port = await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\r\n\r\n")
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
    finally:
        await server.aclose()
