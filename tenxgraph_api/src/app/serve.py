"""Run the API as one Uvicorn process, with the same SIGTERM handling as the Gunicorn image.

    python -m tenxgraph_api.src.app.serve --host 0.0.0.0 --port 8000

For Kubernetes and other platforms that scale by replicas: one process, no Gunicorn. On SIGTERM
:class:`DrainingServer` marks the app as draining first (``/health/ready`` answers 503), and
shortly before ``timeout_graceful_shutdown`` tells open NDJSON streams to end with a final
"interrupted by shutdown" chunk (``core/shutdown.py``). The Gunicorn worker
(``uvicorn_worker.py``) uses the same server class.

Gunicorn is not imported here, so an image without it can run this module.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from uvicorn.config import Config
from uvicorn.importer import import_from_string
from uvicorn.server import STARTUP_FAILURE, Server

from tenxgraph_api.src.app.core.config.server_options import (
    event_loop_choice,
    http_parser_choice,
    max_request_size,
)
from tenxgraph_api.src.app.core.shutdown import interrupt_streams


APP = "tenxgraph_api.src.app.main:app"

# Seconds uvicorn waits for open responses after SIGTERM. Must stay below the gunicorn
# ``--graceful-timeout`` of the generated image (600) with room for the lifespan close
# (``graph.aclose``: the three SHUTDOWN_*_SECONDS settings, 50 s in total by default).
DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 540
# Open streams are told to finish this long before uvicorn would cancel them.
STREAM_INTERRUPT_MARGIN_SECONDS = 5.0
# What the Gunicorn image gets from gunicorn's own defaults, so both paths behave alike.
GUNICORN_KEEPALIVE_SECONDS = 2
GUNICORN_FORWARDED_ALLOW_IPS = "127.0.0.1,::1"


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


def build_config(host: str, port: int) -> Config:
    return Config(
        APP,
        host=host,
        port=port,
        loop=event_loop_choice(),  # type: ignore[arg-type]
        http=http_parser_choice(),  # type: ignore[arg-type]
        timeout_graceful_shutdown=graceful_shutdown_seconds(),
        timeout_keep_alive=GUNICORN_KEEPALIVE_SECONDS,
        ws_max_size=max_request_size(),
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", GUNICORN_FORWARDED_ALLOW_IPS),
    )


def build_server(config: Config) -> DrainingServer:
    """Load the app (exits non-zero on an import error) and wire its state into the server."""
    config.load()
    server = DrainingServer(config=config)
    server.app_state = import_from_string(APP).state  # already imported by ``load``
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tenxgraph_api.src.app.serve")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container listen address
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    server = build_server(build_config(args.host, args.port))
    server.run()
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
