"""Config command implementation: a browser editor for ``10xgraph.json``."""

import json
import webbrowser
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.config_editor import ConfigEditorServer
from tenxgraph_api.cli.config_editor.validation import production_readiness_warnings
from tenxgraph_api.cli.constants import DEFAULT_CONFIG_FILE, LEGACY_CONFIG_FILE
from tenxgraph_api.cli.exceptions import ServerError


class ConfigCommand(BaseCommand):
    """Serve the config editor until the user stops it with Ctrl+C."""

    def execute(
        self,
        config: str = DEFAULT_CONFIG_FILE,
        port: int = 0,
        open_browser: bool = True,
        **kwargs: Any,
    ) -> int:
        # The editor always writes 10xgraph.json. When only the legacy agentflow.json exists,
        # it starts from that file's contents; the first save creates 10xgraph.json and
        # leaves agentflow.json untouched.
        config_path = Path(config).expanduser().resolve()
        seed_path = None
        if config == DEFAULT_CONFIG_FILE and not config_path.exists():
            legacy = config_path.with_name(LEGACY_CONFIG_FILE)
            seed_path = legacy if legacy.exists() else None
        try:
            server = ConfigEditorServer(config_path, port=port, seed_path=seed_path)
        except OSError as exc:
            return self.handle_error(
                ServerError(
                    f"Could not start the config editor: {exc}", host="127.0.0.1", port=port
                )
            )

        self.output.command_header(
            "config",
            f"Edit {config_path.name} in your browser",
            hint="Ctrl+C to stop the config editor",
        )
        self.output.completion_screen(
            "Config editor running",
            "Open the link below to edit, validate, and save your configuration.",
            details={
                "Editor": server.url,
                "File": f"{config_path}{_file_note(config_path, server.store.seeded, seed_path)}",
            },
            next_steps=["Press Ctrl+C to stop the editor when you are done."],
        )
        self._print_readiness_warnings(config_path)
        if open_browser:
            webbrowser.open_new_tab(server.url)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
        return 0

    def _print_readiness_warnings(self, config_path: Path) -> None:
        # Skip silently when the file is missing or not valid JSON; the editor reports that.
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for issue in production_readiness_warnings(data):
            self.output.warning(f"{issue['path']}: {issue['message']}")


def _file_note(config_path: Path, seeded: bool, seed_path: Path | None) -> str:
    if config_path.exists():
        return ""
    if seeded and seed_path is not None:
        return f" (new, starts from {seed_path.name}; saving creates it)"
    return " (new)"
