"""Single-process Uvicorn entry (``python -m tenxgraph_api.src.app.serve``)."""

# ruff: noqa: S101, S603

import http.client
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from tenxgraph_api.src.app import serve, uvicorn_worker


APP_MODULE = textwrap.dedent(
    """
    import asyncio

    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from tenxgraph_api.src.app.core.shutdown import end_on_shutdown

    app = FastAPI()


    async def forever():
        n = 0
        while True:
            draining = str(getattr(app.state, "draining", False)).lower()
            yield f'{{"n": {n}, "draining": {draining}}}\\n'
            n += 1
            await asyncio.sleep(0.2)


    @app.get("/stream")
    async def stream():
        return StreamingResponse(end_on_shutdown(forever()), media_type="application/x-ndjson")
    """
)


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    (tmp_path / "serve_test_app.py").write_text(APP_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(serve, "APP", "serve_test_app:app")
    return tmp_path


def test_worker_reexports_the_shared_server():
    assert uvicorn_worker.DrainingServer is serve.DrainingServer
    assert uvicorn_worker.graceful_shutdown_seconds is serve.graceful_shutdown_seconds


def test_serve_does_not_import_gunicorn():
    code = (
        "import sys; import tenxgraph_api.src.app.serve; "
        "sys.exit(1 if any(m.startswith('gunicorn') for m in sys.modules) else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_config_matches_the_gunicorn_image(monkeypatch):
    monkeypatch.setenv("TENXGRAPH_GRACEFUL_SHUTDOWN_SECONDS", "120")
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    config = serve.build_config("0.0.0.0", 9000)
    assert config.app == serve.APP == "tenxgraph_api.src.app.main:app"
    assert (config.host, config.port) == ("0.0.0.0", 9000)
    assert config.timeout_graceful_shutdown == 120
    assert (config.loop, config.http) == ("auto", "auto")
    assert config.timeout_keep_alive == 2
    assert config.forwarded_allow_ips == "127.0.0.1,::1"
    assert config.proxy_headers is True
    assert config.workers == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 10485760), ("2048", 2048), ("abc", 10485760), ("0", 10485760), ("-5", 10485760)],
)
def test_config_ws_max_size_follows_max_request_size(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MAX_REQUEST_SIZE", raising=False)
    else:
        monkeypatch.setenv("MAX_REQUEST_SIZE", raw)
    assert serve.build_config("0.0.0.0", 9000).ws_max_size == expected


def test_worker_config_kwargs_cap_websocket_messages():
    assert uvicorn_worker.TenxGraphUvicornWorker.CONFIG_KWARGS["ws_max_size"] == (
        serve.max_request_size()
    )


def test_config_default_graceful_timeout(monkeypatch):
    monkeypatch.delenv("TENXGRAPH_GRACEFUL_SHUTDOWN_SECONDS", raising=False)
    config = serve.build_config("127.0.0.1", 8000)
    assert config.timeout_graceful_shutdown == serve.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS == 540


def test_build_server_loads_the_app_and_wires_its_state(app_module):
    import serve_test_app

    server = serve.build_server(serve.build_config("127.0.0.1", 0))
    assert isinstance(server, serve.DrainingServer)
    assert server.config.loaded
    assert server.app_state is serve_test_app.app.state


def test_import_error_exits_non_zero(monkeypatch):
    monkeypatch.setattr(serve, "APP", "no_such_module_xyz:app")
    with pytest.raises(SystemExit) as exc:
        serve.build_server(serve.build_config("127.0.0.1", 0))
    assert exc.value.code == serve.STARTUP_FAILURE != 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
def test_sigterm_ends_open_stream_with_interrupted_chunk(app_module):
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(app_module), os.environ.get("PYTHONPATH", "")]),
        # Streams are interrupted 5 s before this deadline: 1 s after SIGTERM.
        "TENXGRAPH_GRACEFUL_SHUTDOWN_SECONDS": "6",
    }
    script = (
        "import sys; from tenxgraph_api.src.app import serve; "
        "serve.APP = 'serve_test_app:app'; serve.main(sys.argv[1:])"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/stream")
                response = conn.getresponse()
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.1)
        assert response.status == 200
        assert b'"n": 0' in response.readline()

        proc.send_signal(signal.SIGTERM)
        lines = []
        while line := response.readline():
            lines.append(line)
        assert b'"interrupted":true' in lines[-1].replace(b" ", b"")
        # Draining is flagged as soon as shutdown starts, before the lifespan runs.
        assert b'"draining": true' in lines[-2]

        # Clean shutdown; uvicorn re-raises the captured SIGTERM once it has finished.
        assert proc.wait(timeout=15) in (0, -signal.SIGTERM)
        assert b"Finished server process" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
