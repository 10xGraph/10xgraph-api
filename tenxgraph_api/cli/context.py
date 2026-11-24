"""Shared runtime context for 10xGraph CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from tenxgraph_api.cli.capabilities import TerminalCapabilities


@dataclass(frozen=True, slots=True)
class CLIContext:
    """Invocation-scoped CLI state passed through Typer's context object."""

    capabilities: TerminalCapabilities
    cwd: Path
    verbosity: int = 0
    quiet: bool = False
    debug: bool = False
    yes: bool = False
    non_interactive: bool = False
    invocation_id: str = ""

    @classmethod
    def create(
        cls,
        *,
        capabilities: TerminalCapabilities,
        cwd: Path,
        verbosity: int = 0,
        quiet: bool = False,
        debug: bool = False,
        yes: bool = False,
        non_interactive: bool = False,
    ) -> CLIContext:
        return cls(
            capabilities=capabilities,
            cwd=cwd,
            verbosity=verbosity,
            quiet=quiet,
            debug=debug,
            yes=yes,
            non_interactive=non_interactive,
            invocation_id=uuid4().hex[:12],
        )
