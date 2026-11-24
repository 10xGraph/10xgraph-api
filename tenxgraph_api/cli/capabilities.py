"""Terminal capability and output-mode detection for the CLI."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO


class OutputFormat(StrEnum):
    """Supported user-facing output formats."""

    HUMAN = "human"
    PLAIN = "plain"
    JSON = "json"
    JSONL = "jsonl"


class ColorMode(StrEnum):
    """Color rendering policy."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class ProgressMode(StrEnum):
    """Progress rendering policy."""

    AUTO = "auto"
    TTY = "tty"
    PLAIN = "plain"
    JSON = "json"
    QUIET = "quiet"


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    """Resolved capabilities for one CLI invocation."""

    interactive: bool
    is_ci: bool
    color: bool
    unicode: bool
    animation: bool
    output_format: OutputFormat
    progress_mode: ProgressMode

    @classmethod
    def detect(
        cls,
        *,
        stream: TextIO | None = None,
        output_format: OutputFormat = OutputFormat.HUMAN,
        color_mode: ColorMode = ColorMode.AUTO,
        progress_mode: ProgressMode = ProgressMode.AUTO,
    ) -> TerminalCapabilities:
        """Detect terminal behavior while honoring explicit user policy."""
        target = stream or sys.stdout
        is_tty = bool(getattr(target, "isatty", lambda: False)())
        is_ci = truthy_env("CI")
        term_is_dumb = os.environ.get("TERM", "").lower() == "dumb"
        no_color = "NO_COLOR" in os.environ

        effective_format = output_format
        if output_format == OutputFormat.HUMAN and not is_tty:
            effective_format = OutputFormat.PLAIN

        if color_mode == ColorMode.ALWAYS:
            color = True
        elif color_mode == ColorMode.NEVER:
            color = False
        else:
            color = is_tty and not is_ci and not term_is_dumb and not no_color

        if progress_mode == ProgressMode.AUTO:
            effective_progress = (
                ProgressMode.TTY
                if is_tty and not is_ci and effective_format == OutputFormat.HUMAN
                else ProgressMode.PLAIN
            )
        else:
            effective_progress = progress_mode

        structured = effective_format in {OutputFormat.JSON, OutputFormat.JSONL}
        animation = (
            effective_progress == ProgressMode.TTY
            and not structured
            and not truthy_env(cli_env_name("NO_SPINNER"))
        )
        encoding = getattr(target, "encoding", None)
        try:
            if encoding:
                "✓→•".encode(encoding)
            encoding_supports_unicode = True
        except UnicodeEncodeError:
            encoding_supports_unicode = False
        unicode = (
            not term_is_dumb and encoding_supports_unicode and not truthy_env(cli_env_name("ASCII"))
        )

        return cls(
            interactive=is_tty and not is_ci,
            is_ci=is_ci,
            color=color and not structured,
            unicode=unicode,
            animation=animation,
            output_format=effective_format,
            progress_mode=effective_progress,
        )


def cli_env_name(suffix: str) -> str:
    """``TENXGRAPH_<suffix>``, or the pre-rename ``AGENTFLOW_<suffix>`` when only it is set."""
    name = f"TENXGRAPH_{suffix}"
    legacy = f"AGENTFLOW_{suffix}"
    if name not in os.environ and legacy in os.environ:
        return legacy
    return name


def truthy_env(name: str) -> bool:
    """Read an environment variable as an opt-in boolean flag."""
    value = os.environ.get(name, "")
    return value.lower() in {"1", "true", "yes", "on"}
