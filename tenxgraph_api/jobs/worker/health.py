"""Kubernetes probes for ``10xgraph worker --health-port`` (a tiny asyncio HTTP server).

- ``GET /healthz``: 200 while the worker loop is alive (it answers from the same event loop,
  and the worker's ticker ran recently), else 503.
- ``GET /readyz``: 200 while the store and transport are reachable and the worker is not
  draining, else 503.

Answers are JSON (``{"status": "ok"}``) with ``Connection: close``. No dependency beyond the
standard library; one request per connection, request line and headers capped at 8 KB and
read within 5 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable


logger = logging.getLogger("tenxgraph_api.jobs")

READ_TIMEOUT = 5.0
MAX_HEADER_BYTES = 8192
PROBE_TIMEOUT = 2.0

_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}


class HealthServer:
    """Serves ``/healthz`` and ``/readyz``. ``alive`` is sync; ``ready`` is awaited with a
    ``PROBE_TIMEOUT`` (a timeout or an error counts as not ready)."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        ready: Callable[[], Awaitable[bool]],
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._alive = alive
        self._ready = ready
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        """Start listening; returns the bound port (useful with ``port=0``)."""
        self._server = await asyncio.start_server(
            self._serve, self.host, self.port, limit=MAX_HEADER_BYTES
        )
        sockets = self._server.sockets or ()
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        logger.info("jobs health probes on %s:%d (/healthz, /readyz)", self.host, self.port)
        return self.port

    async def aclose(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

    async def _status(self, path: str) -> tuple[int, dict[str, str]]:
        if path == "/healthz":
            ok = False
            try:
                ok = bool(self._alive())
            except Exception:
                logger.warning("jobs liveness check failed", exc_info=True)
            return (200, {"status": "ok"}) if ok else (503, {"status": "unavailable"})
        if path == "/readyz":
            ok = False
            try:
                ok = bool(await asyncio.wait_for(self._ready(), PROBE_TIMEOUT))
            except Exception:
                ok = False
            return (200, {"status": "ok"}) if ok else (503, {"status": "unavailable"})
        return 404, {"status": "not found"}

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                async with asyncio.timeout(READ_TIMEOUT):
                    head = await reader.readuntil(b"\r\n\r\n")
            except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                return
            if len(head) > MAX_HEADER_BYTES:
                return
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if len(parts) < 2:  # noqa: PLR2004 - method and target
                return
            method, target = parts[0], parts[1].split("?", 1)[0]
            if method not in ("GET", "HEAD"):
                code, body = 405, {"status": "method not allowed"}
            else:
                code, body = await self._status(target)
            payload = json.dumps(body).encode()
            header = (
                f"HTTP/1.1 {code} {_REASONS.get(code, '')}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            writer.write(header if method == "HEAD" else header + payload)
            with contextlib.suppress(ConnectionError):
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
