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

Without Gunicorn (one process per pod), run ``python -m tenxgraph_api.src.app.serve``.
"""

from __future__ import annotations

import sys

from gunicorn.arbiter import Arbiter
from uvicorn_worker import UvicornWorker

from tenxgraph_api.src.app.core.config.server_options import (
    event_loop_choice,
    http_parser_choice,
    max_request_size,
)

# Shared with the Gunicorn-free single-process entry (``serve.py``); re-exported here.
from tenxgraph_api.src.app.serve import (
    DEFAULT_GRACEFUL_SHUTDOWN_SECONDS,
    STREAM_INTERRUPT_MARGIN_SECONDS,
    DrainingServer,
    graceful_shutdown_seconds,
)


__all__ = [
    "DEFAULT_GRACEFUL_SHUTDOWN_SECONDS",
    "STREAM_INTERRUPT_MARGIN_SECONDS",
    "DrainingServer",
    "TenxGraphUvicornWorker",
    "graceful_shutdown_seconds",
]


class TenxGraphUvicornWorker(UvicornWorker):
    CONFIG_KWARGS = {
        **UvicornWorker.CONFIG_KWARGS,
        "loop": event_loop_choice(),
        "http": http_parser_choice(),
        "timeout_graceful_shutdown": graceful_shutdown_seconds(),
        "ws_max_size": max_request_size(),
    }

    async def _serve(self) -> None:
        self.config.app = self.wsgi
        server = DrainingServer(config=self.config)
        server.app_state = getattr(self.wsgi, "state", None)
        self._install_sigquit_handler()
        await server.serve(sockets=self.sockets)
        if not server.started:
            sys.exit(Arbiter.WORKER_BOOT_ERROR)
