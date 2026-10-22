"""API server command implementation."""

import ipaddress
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import uvicorn
from dotenv import load_dotenv

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.constants import (
    DEFAULT_CONFIG_FILE,
    DEFAULT_HOST,
    DEFAULT_PLAYGROUND_URL,
    DEFAULT_PORT,
    LEGACY_CONFIG_FILE,
)
from tenxgraph_api.cli.core.config import ConfigManager
from tenxgraph_api.cli.core.validation import validate_cli_options
from tenxgraph_api.cli.exceptions import ConfigurationError, ServerError


# Mirrors Settings.MAX_REQUEST_SIZE; read from the environment the command just loaded.
DEFAULT_MAX_REQUEST_SIZE = 10 * 1024 * 1024


def _max_request_size() -> int:
    raw = os.environ.get("MAX_REQUEST_SIZE")
    try:
        value = int(raw) if raw else DEFAULT_MAX_REQUEST_SIZE
    except ValueError:
        return DEFAULT_MAX_REQUEST_SIZE
    return value if value > 0 else DEFAULT_MAX_REQUEST_SIZE


class APICommand(BaseCommand):
    """Command to start the 10xGraph API server."""

    _PLAYGROUND_WAIT_TIMEOUT_SECONDS = 30.0
    _PLAYGROUND_WAIT_INTERVAL_SECONDS = 0.25

    def execute(
        self,
        config: str = DEFAULT_CONFIG_FILE,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        reload: bool = True,
        open_playground: bool = False,
        playground_url: str = DEFAULT_PLAYGROUND_URL,
        **kwargs: Any,
    ) -> int:
        """Execute the API server command.

        Args:
            config: Path to config file
            host: Host to bind to
            port: Port to bind to
            reload: Enable auto-reload
            open_playground: Open the hosted playground after the API becomes reachable
            playground_url: Hosted playground base URL
            **kwargs: Additional arguments

        Returns:
            Exit code
        """
        try:
            self.output.command_header(
                "dev",
                "Starting development server via Uvicorn. Not for production use.",
                hint="Ctrl+C to stop the development server",
            )

            timeline = self.output.timeline(
                "Preparing the 10xGraph runtime",
                steps=(
                    ("config", "Validating project configuration"),
                    ("runtime", "Loading environment and graph runtime"),
                    ("port", f"Reserving port {port}"),
                    ("launch", "Starting the development server"),
                ),
            )
            with timeline:
                with timeline.step("config") as step:
                    validated_options = validate_cli_options(host, port, config)
                    config_manager = ConfigManager()
                    actual_config_path = config_manager.find_config_file(
                        validated_options["config"]
                    )
                    config_manager.load_config(str(actual_config_path))
                    detail = str(actual_config_path)
                    if actual_config_path.name == LEGACY_CONFIG_FILE:
                        detail += f" (deprecated name; rename it to {DEFAULT_CONFIG_FILE})"
                    step.detail(detail)

                with timeline.step("runtime") as step:
                    env_file_path = config_manager.resolve_env_file()
                    if env_file_path:
                        self.logger.info("Loading environment from: %s", env_file_path)
                        load_dotenv(env_file_path)
                        step.detail(f"environment: {env_file_path}")
                    else:
                        load_dotenv()
                        step.detail("environment: process defaults")

                    os.environ["GRAPH_PATH"] = str(actual_config_path)

                    # Add project root to sys.path for importing graph modules
                    sys.path.insert(0, str(actual_config_path.parent))

                    # Ensure we're using the correct module path
                    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

                browser_host = self._normalize_browser_host(validated_options["host"])
                with timeline.step("port") as step:
                    # Reported rather than raised: Uvicorn owns the bind, and its
                    # own error is the authoritative one if the port is taken.
                    if self._port_in_use(browser_host, validated_options["port"]):
                        step.detail("already in use — Uvicorn will report the conflict")
                    else:
                        step.detail("available")

                with timeline.step("launch") as step:
                    self.logger.info(
                        "Starting API with config: %s, host: %s, port: %d",
                        actual_config_path,
                        validated_options["host"],
                        validated_options["port"],
                    )
                    launch_url = None
                    if open_playground:
                        launch_url = self._schedule_playground_launch(
                            host=validated_options["host"],
                            port=validated_options["port"],
                            playground_base_url=playground_url,
                        )
                    step.detail(
                        f"playground opens at {launch_url} once the API responds"
                        if launch_url
                        else f"http://{browser_host}:{validated_options['port']}"
                    )

            self.output.completion_screen(
                "Ready to serve",
                "10xGraph runtime configured successfully",
                details={
                    "API": f"http://{browser_host}:{validated_options['port']}",
                    "Docs": f"http://{browser_host}:{validated_options['port']}/docs",
                    "Config": actual_config_path,
                    "Reload": "enabled" if reload else "disabled",
                },
                next_steps=(
                    ["The playground will open automatically when the API becomes reachable."]
                    if open_playground
                    else ["Press Ctrl+C to stop the development server."]
                ),
            )

            uvicorn.run(
                "tenxgraph_api.src.app.main:app",
                host=validated_options["host"],
                port=validated_options["port"],
                reload=reload,
                workers=1,
                # Refuse WebSocket frames over the HTTP body cap before buffering them
                # (uvicorn's own default is 16 MiB).
                ws_max_size=_max_request_size(),
            )

            return 0

        except (ConfigurationError, ServerError) as e:
            return self.handle_error(e)
        except Exception as e:
            server_error = ServerError(
                f"Failed to start API server: {e}",
                host=host,
                port=port,
            )
            return self.handle_error(server_error)

    @staticmethod
    def _port_in_use(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex((host, port)) == 0

    def _schedule_playground_launch(
        self,
        host: str,
        port: int,
        playground_base_url: str,
    ) -> str:
        """Start the readiness watcher and return the URL it will open."""
        browser_host = self._normalize_browser_host(host)
        launch_url = self._build_playground_url(browser_host, port, playground_base_url)
        launch_thread = threading.Thread(
            target=self._open_playground_when_ready,
            args=(launch_url, browser_host, port),
            daemon=True,
            name="10xgraph-playground-launcher",
        )
        launch_thread.start()
        return launch_url

    def _open_playground_when_ready(
        self,
        launch_url: str,
        host: str,
        port: int,
    ) -> None:
        if not self._wait_for_server(host, port):
            self.logger.warning(
                "API server did not become reachable in time. Open the playground manually: %s",
                launch_url,
            )
            return

        opened = webbrowser.open_new_tab(launch_url)
        if opened:
            self.logger.info("Opened playground URL: %s", launch_url)
            # A plain line, not a panel: this fires from the watcher thread while
            # Uvicorn is already streaming logs, and a boxed reveal there would
            # interleave with them.
            self.output.success(f"Playground opened: {launch_url}")
            return

        message = f"Browser launch returned false. Open the playground manually: {launch_url}"
        self.logger.warning(message)
        self.output.warning(message)

    def _wait_for_server(
        self,
        host: str,
        port: int,
    ) -> bool:
        deadline = time.monotonic() + self._PLAYGROUND_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1):
                    return True
            except OSError:
                time.sleep(self._PLAYGROUND_WAIT_INTERVAL_SECONDS)

        return False

    def _build_playground_url(
        self,
        host: str,
        port: int,
        playground_base_url: str,
    ) -> str:
        backend_host = host
        if ":" in backend_host and not backend_host.startswith("["):
            backend_host = f"[{backend_host}]"

        backend_url = f"http://{backend_host}:{port}"
        return f"{playground_base_url}?{urlencode({'backendUrl': backend_url})}"

    def _normalize_browser_host(self, host: str) -> str:
        if not host:
            return "127.0.0.1"

        normalized_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host

        try:
            if ipaddress.ip_address(normalized_host).is_unspecified:
                return "127.0.0.1"
        except ValueError:
            pass

        return normalized_host
