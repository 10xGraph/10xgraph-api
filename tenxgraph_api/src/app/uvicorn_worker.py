"""Gunicorn worker class used by the generated image (``10xgraph build``).

Differences from ``uvicorn_worker.UvicornWorker``:

- ``timeout_graceful_shutdown`` is set, so uvicorn gives up on open responses before gunicorn's
  ``--graceful-timeout`` SIGKILLs the worker. Without it a long stream holds shutdown until the
  kill and the lifespan drain never runs.
- Shutdown marks the app as draining first (``/health/ready`` answers 503), and shortly before
  the deadline tells open NDJSON streams to end with a final "interrupted by shutdown" chunk
  (``core/shutdown.py``), so clients see why the stream ended and can resume the thread.

Open WebSockets are still closed by uvicorn with 1012 (service restart) at that point; the run
is saved INTERRUPTED and the client reconnects.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from gunicorn.arbiter import Arbiter
from uvicorn.server import Server
from uvicorn_worker import UvicornWorker

from tenxgraph_api.src.app.core.shutdown import interrupt_streams


# Seconds uvicorn waits for open responses after SIGTERM. Must stay below the gunicorn
# ``--graceful-timeout`` of the generated image (600) with room for the lifespan drain
# (30 s) and ``graph.aclose``.
DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 540
# Open streams are told to finish this long before uvicorn would cancel them.
STREAM_INTERRUPT_MARGIN_SECONDS = 5.0


def graceful_shutdown_seconds() -> int:
    raw = os.environ.get("TENXGRAPH_GRACEFUL_SHUTDOWN_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_GRACEFUL_SHUTDOWN_SECONDS
    return value if value > 0 else DEFAULT_GRACEFUL_SHUTDOWN_SECONDS


class DrainingServer(Server):
    """Uvicorn server that flags the app as draining before it stops serving."""

    app_state: Any = None

    async def shutdown(self, sockets: Any = None) -> None:
        if self.app_state is not None:
            self.app_state.draining = True
        timeout = self.config.timeout_graceful_shutdown
        timer = None
        if timeout:
            delay = max(timeout - STREAM_INTERRUPT_MARGIN_SECONDS, 0.0)
            timer = asyncio.get_running_loop().call_later(delay, interrupt_streams)
        try:
            await super().shutdown(sockets=sockets)
        finally:
            if timer is not None:
                timer.cancel()


class TenxGraphUvicornWorker(UvicornWorker):
    CONFIG_KWARGS = {
        **UvicornWorker.CONFIG_KWARGS,
        "timeout_graceful_shutdown": graceful_shutdown_seconds(),
    }

    async def _serve(self) -> None:
        self.config.app = self.wsgi
        server = DrainingServer(config=self.config)
        server.app_state = getattr(self.wsgi, "state", None)
        self._install_sigquit_handler()
        await server.serve(sockets=self.sockets)
        if not server.started:
            sys.exit(Arbiter.WORKER_BOOT_ERROR)
