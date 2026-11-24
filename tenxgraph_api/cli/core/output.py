"""Adaptive terminal output utilities for the CLI."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TextIO

import click
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from tenxgraph_api.cli.capabilities import (
    ColorMode,
    OutputFormat,
    ProgressMode,
    TerminalCapabilities,
)
from tenxgraph_api.cli.constants import CLI_VERSION
from tenxgraph_api.cli.core.animations import render_command_intro, render_session_header
from tenxgraph_api.cli.core.prompts import PromptService
from tenxgraph_api.cli.core.screen import AppFrame
from tenxgraph_api.cli.core.steps import (
    LiveProgressRun,
    LiveTimeline,
    ProgressRun,
    QuietProgressRun,
    QuietTimeline,
    StaticProgressRun,
    StaticTimeline,
    StructuredProgressRun,
    StructuredTimeline,
    Timeline,
)
from tenxgraph_api.cli.core.theme import (
    ACCENT,
    LINE,
    TENXGRAPH_THEME,
    Glyphs,
    glyphs_for,
)


class OutputFormatter:
    """Render semantic CLI output for terminals, pipes, and automation."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        error_stream: TextIO | None = None,
        output_format: OutputFormat = OutputFormat.HUMAN,
        color_mode: ColorMode = ColorMode.AUTO,
        progress_mode: ProgressMode = ProgressMode.AUTO,
        quiet: bool = False,
    ) -> None:
        self._stream = stream
        self._error_stream = error_stream
        self.output_format = output_format
        self.color_mode = color_mode
        self.progress_mode = progress_mode
        self.quiet = quiet
        self._session_console: Console | None = None
        self._frame: AppFrame | None = None
        self._fullscreen_requested = False
        self._consoles: dict[bool, Console] = {}
        self.capabilities = TerminalCapabilities.detect(
            stream=self.stream,
            output_format=output_format,
            color_mode=color_mode,
            progress_mode=progress_mode,
        )

    @property
    def stream(self) -> TextIO:
        """Current stdout stream, remaining compatible with CliRunner capture."""
        return self._stream or sys.stdout

    @property
    def error_stream(self) -> TextIO:
        """Current stderr stream, or the explicit test stream when supplied."""
        if self._error_stream is not None:
            return self._error_stream
        if self._stream is not None:
            return self._stream
        return sys.stderr

    def configure(
        self,
        *,
        output_format: OutputFormat | None = None,
        color_mode: ColorMode | None = None,
        progress_mode: ProgressMode | None = None,
        quiet: bool | None = None,
    ) -> None:
        """Apply invocation-level rendering policy."""
        if output_format is not None:
            self.output_format = output_format
        if color_mode is not None:
            self.color_mode = color_mode
        if progress_mode is not None:
            self.progress_mode = progress_mode
        if quiet is not None:
            self.quiet = quiet
        # Color and terminal policy are baked into a Console at construction.
        self._consoles.clear()
        self.capabilities = TerminalCapabilities.detect(
            stream=self.stream,
            output_format=self.output_format,
            color_mode=self.color_mode,
            progress_mode=self.progress_mode,
        )

    def _console(self, *, error: bool = False) -> Console:
        """Return the one console bound to this stream.

        Consoles are cached rather than rebuilt per call because a Rich live
        display only knows to render ordinary prints *above* itself when those
        prints go through the same console instance.
        """
        if self._session_console is not None:
            return self._session_console
        cached = self._consoles.get(error)
        if cached is None:
            cached = Console(
                file=self.error_stream if error else self.stream,
                theme=TENXGRAPH_THEME,
                no_color=not self.capabilities.color,
                force_terminal=self.capabilities.color,
                highlight=False,
                soft_wrap=False,
            )
            self._consoles[error] = cached
        return cached

    @property
    def glyphs(self) -> Glyphs:
        """Symbol set matching this terminal's encoding support."""
        return glyphs_for(self.capabilities.unicode)

    @property
    def fullscreen_active(self) -> bool:
        """Whether this invocation currently owns the alternate screen."""
        return self._frame is not None and self._frame.active

    def request_fullscreen(self, enabled: bool) -> None:
        """Record whether this invocation may claim a full-screen surface.

        The screen is not taken here. It is claimed lazily by the first
        ``command_header`` call, so script-shaped commands — ``--version``,
        ``config get`` — never take over the terminal or pause on exit.
        """
        self._fullscreen_requested = enabled

    def start_fullscreen_session(self) -> bool:
        """Claim the alternate screen for the whole command."""
        if self.fullscreen_active or self.quiet or not self.capabilities.animation:
            return False

        console = self._console()
        frame = AppFrame(console, glyphs=self.glyphs, color=self.capabilities.color)
        if not frame.open():
            return False

        # Bind every later render to this console so chrome, live displays, and
        # ordinary prints all agree on where the cursor is.
        self._session_console = console
        self._frame = frame
        return True

    def end_fullscreen_session(self) -> None:
        """Restore the user's terminal, pausing on the closing hint after a normal finish.

        Called from the CLI context's close callback, which runs while an exception is
        still propagating. Ctrl+C (``KeyboardInterrupt``, or Click's ``Abort`` wrapping
        it) releases the screen immediately instead of waiting for Enter; a failure
        still pauses so its message can be read before the screen is discarded.
        """
        frame = self._frame
        self._frame = None
        self._session_console = None
        if frame is not None:
            in_flight = sys.exc_info()[1]
            interrupted = isinstance(in_flight, KeyboardInterrupt | click.exceptions.Abort)
            frame.close(hold=not interrupted)

    def refresh_chrome(self) -> None:
        """Repaint pinned chrome that something else may have drawn over."""
        if self._frame is not None:
            self._frame.redraw_chrome()

    def prompts(self, *, interactive: bool | None = None) -> PromptService:
        """Build a prompt service bound to this invocation's terminal state."""
        return PromptService(
            interactive=interactive,
            on_answered=self.refresh_chrome,
        )

    @property
    def _structured(self) -> bool:
        return self.capabilities.output_format in {OutputFormat.JSON, OutputFormat.JSONL}

    def _emit_event(self, event: str, message: str, **data: Any) -> None:
        payload = {
            "schema": "agentflow.cli/v1",
            "type": event,
            "message": message,
            **data,
        }
        print(json.dumps(payload, ensure_ascii=False, default=str), file=self.stream, flush=True)

    def print_banner(
        self,
        title: str,
        subtitle: str | None = None,
        color: str = LINE,
        width: int = 50,
    ) -> None:
        """Render a compact section heading or a panel on an interactive terminal."""
        if self.quiet:
            return
        if self._structured:
            self._emit_event("section", title, subtitle=subtitle)
            return
        if self.capabilities.output_format == OutputFormat.PLAIN:
            print(f"\n== {title} ==", file=self.stream)
            if subtitle:
                print(subtitle, file=self.stream)
            print("", file=self.stream)
            return

        body = f"[tenxgraph.title]{title}[/tenxgraph.title]"
        if subtitle:
            body += f"\n[tenxgraph.muted]{subtitle}[/tenxgraph.muted]"
        self._console().print(Panel.fit(body, border_style=color, padding=(0, 1), width=width))

    def command_header(
        self,
        command: str,
        subtitle: str | None = None,
        *,
        color: str = LINE,
        hint: str | None = None,
    ) -> None:
        """Render an animated command identity when the terminal supports it.

        ``hint`` is the closing line pinned in the full-screen footer; it should
        say how to leave whatever the command is about to do.
        """
        if self.quiet:
            return
        if self._structured:
            self._emit_event("section", command, subtitle=subtitle)
            return
        if self.capabilities.output_format == OutputFormat.PLAIN:
            self.print_banner(command.title(), subtitle, color=color)
            return
        if not self.capabilities.animation:
            # Still branded, just motionless — reduced motion should not mean
            # a downgrade in the information the header carries.
            render_session_header(
                self._console(),
                command=command,
                subtitle=subtitle,
                glyphs=self.glyphs,
                version=CLI_VERSION,
            )
            return
        if self._fullscreen_requested:
            self.start_fullscreen_session()
        # Without the opt-in frame the intro borrows a temporary screen, then prints a
        # durable header into the normal scrollback: nothing to dismiss on exit.
        render_command_intro(
            self._console(),
            command=command,
            subtitle=subtitle,
            unicode=self.capabilities.unicode,
            persistent_screen=self.fullscreen_active,
        )
        if self._frame is not None:
            # The intro owned the whole canvas; pin the chrome it collapses into
            # and hand the body region over to the command's output.
            self._frame.install_chrome(
                command=command,
                subtitle=subtitle,
                version=CLI_VERSION,
                hint=hint,
            )

    def timeline(
        self,
        title: str | None = None,
        *,
        steps: Sequence[tuple[str, str]] = (),
    ) -> Timeline:
        """Create a multi-stage progress timeline for the current output mode.

        Declaring ``steps`` up front lets the user see the whole plan the moment
        work begins, with pending stages dimmed and the running one animated.
        """
        if self.quiet:
            return QuietTimeline(steps)
        if self._structured:
            return StructuredTimeline(emit=self._emit_event_data, title=title, steps=steps)
        if not self.capabilities.animation:
            return StaticTimeline(
                glyphs=self.glyphs,
                emit=self._write_line,
                title=title,
                steps=steps,
            )
        return LiveTimeline(self._console(), glyphs=self.glyphs, title=title, steps=steps)

    def progress_run(self, title: str, *, total: int) -> ProgressRun:
        """Create a determinate progress display with a running pass/fail tally."""
        if self.quiet:
            return QuietProgressRun(total=total, title=title)
        if self._structured:
            return StructuredProgressRun(total=total, title=title, emit=self._emit_event_data)
        if not self.capabilities.animation:
            return StaticProgressRun(
                total=total,
                title=title,
                glyphs=self.glyphs,
                emit=self._write_line,
            )
        return LiveProgressRun(self._console(), total=total, title=title, glyphs=self.glyphs)

    def _emit_event_data(self, event: str, message: str, data: dict[str, Any]) -> None:
        self._emit_event(event, message, **data)

    def _write_line(self, message: str) -> None:
        """Write one pre-formatted line without interpreting console markup."""
        if self.capabilities.output_format == OutputFormat.PLAIN:
            print(message, file=self.stream)
            return
        self._console().print(Text(message))

    def success(self, message: str, emoji: bool = True) -> None:
        if self.quiet:
            return
        symbol = "✓" if self.capabilities.unicode else "OK"
        self._message("success", message, symbol if emoji else "", error=False)

    def error(self, message: str, emoji: bool = True) -> None:
        symbol = "✗" if self.capabilities.unicode else "X"
        self._message("error", message, symbol if emoji else "", error=True)

    def info(self, message: str, emoji: bool = True) -> None:
        if self.quiet:
            return
        self._message("info", message, "i" if emoji else "", error=False)

    def warning(self, message: str, emoji: bool = True) -> None:
        if self.quiet:
            return
        self._message("warning", message, "!" if emoji else "", error=False)

    def _message(
        self,
        level: str,
        message: str,
        symbol: str,
        *,
        error: bool,
    ) -> None:
        if self._structured:
            self._emit_event(level, message)
            return
        prefix = f"{symbol} " if symbol else ""
        if self.capabilities.output_format == OutputFormat.PLAIN:
            print(f"{prefix}{message}", file=self.error_stream if error else self.stream)
            return
        self._console(error=error).print(
            f"[tenxgraph.{level}]{prefix}{message}[/tenxgraph.{level}]"
        )

    def emphasize(self, message: str) -> None:
        if self.quiet:
            return
        self._message(
            "info",
            message,
            "•" if self.capabilities.unicode else "*",
            error=False,
        )

    def print_list(
        self,
        items: list[str],
        title: str | None = None,
        bullet: str = "•",
    ) -> None:
        if self.quiet:
            return
        if self._structured:
            self._emit_event("list", title or "", items=items)
            return
        console = self._console()
        if title:
            console.print(f"\n[bold]{title}[/bold]")
        for item in items:
            console.print(f"  {bullet} {item}")

    def print_key_value_pairs(
        self,
        pairs: dict[str, Any],
        title: str | None = None,
        indent: int = 2,
    ) -> None:
        if self.quiet:
            return
        if self._structured:
            self._emit_event("data", title or "", data=pairs)
            return
        console = self._console()
        if title:
            console.print(f"\n[bold]{title}[/bold]")
        pad = " " * indent
        for key, value in pairs.items():
            console.print(f"{pad}[tenxgraph.muted]{key}[/tenxgraph.muted]  {value}")

    def print_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        title: str | None = None,
    ) -> None:
        if self.quiet:
            return
        if self._structured:
            records = [
                {
                    header: row[index] if index < len(row) else ""
                    for index, header in enumerate(headers)
                }
                for row in rows
            ]
            self._emit_event("table", title or "", columns=headers, rows=records)
            return

        table = Table(title=title, header_style="bold cyan", show_lines=False)
        for header in headers:
            table.add_column(str(header), overflow="fold")
        for row in rows:
            table.add_row(*(str(row[i]) if i < len(row) else "" for i in range(len(headers))))
        self._console().print(table)

    @contextmanager
    def status(self, message: str, *, spinner: str = "dots12") -> Iterator[Status | None]:
        """Show an indeterminate status when animation is safe."""
        if self._structured:
            self._emit_event("progress_start", message)
            try:
                yield None
            except Exception:
                self._emit_event("progress_end", message, status="failed")
                raise
            else:
                self._emit_event("progress_end", message, status="completed")
            return
        if self.quiet or not self.capabilities.animation:
            if not self.quiet and self.capabilities.progress_mode == ProgressMode.PLAIN:
                self.info(message, emoji=False)
            yield None
            return
        with self._console().status(
            f"[tenxgraph.progress]{message}[/tenxgraph.progress]",
            spinner=spinner,
            spinner_style="tenxgraph.brand",
        ) as status:
            yield status

    def completion_screen(
        self,
        title: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        next_steps: list[str] | None = None,
    ) -> None:
        """Render a polished final state for a completed workflow."""
        if self.quiet:
            return
        if self._structured:
            self._emit_event(
                "completion",
                message,
                title=title,
                details=details or {},
                next_steps=next_steps or [],
            )
            return
        if self.capabilities.output_format == OutputFormat.PLAIN:
            self.success(f"{title}: {message}")
            if details:
                self.print_key_value_pairs(details)
            if next_steps:
                self.print_list(next_steps, title="Next steps", bullet="->")
            return

        console = self._console()
        rows = list((details or {}).items())
        steps = list(next_steps or [])
        total = len(rows) + len(steps)

        if not self.capabilities.animation or not total:
            console.print(self._completion_panel(title, message, rows, steps, total))
            return

        # Reveal one row at a time so the result reads as an outcome landing
        # rather than a wall of text appearing at once.
        with Live(
            console=console,
            auto_refresh=False,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        ) as live:
            for visible in range(total + 1):
                live.update(
                    self._completion_panel(title, message, rows, steps, visible),
                    refresh=True,
                )
                time.sleep(0.05)

    def _completion_panel(
        self,
        title: str,
        message: str,
        rows: list[tuple[str, Any]],
        steps: list[str],
        visible: int,
    ) -> Panel:
        """Build the completion panel with only its first ``visible`` rows shown."""
        glyphs = self.glyphs
        body = Text()
        body.append(f"{glyphs.check} ", style="tenxgraph.success")
        body.append(message, style="tenxgraph.command")

        shown = 0
        if rows:
            label_width = max(len(str(key)) for key, _ in rows) + 2
            body.append("\n")
            for key, value in rows:
                if shown >= visible:
                    break
                body.append(f"\n  {key!s:<{label_width}}", style="tenxgraph.muted")
                body.append(str(value), style="tenxgraph.command")
                shown += 1

        if steps and visible > len(rows):
            body.append("\n\n")
            body.append("Next", style=f"bold {ACCENT}")
            for step in steps:
                if shown >= visible:
                    break
                body.append(f"\n  {glyphs.arrow} ", style="tenxgraph.accent")
                body.append(step, style="tenxgraph.command")
                shown += 1

        return Panel(
            body,
            title=Text(f" {title} ", style="tenxgraph.title"),
            title_align="left",
            box=box.ROUNDED,
            border_style=LINE,
            padding=(1, 2),
        )


output = OutputFormatter()


def print_banner(title: str, subtitle: str | None = None, color: str = LINE) -> None:
    output.print_banner(title, subtitle, color)


def success(message: str, emoji: bool = True) -> None:
    output.success(message, emoji)


def error(message: str, emoji: bool = True) -> None:
    output.error(message, emoji)


def info(message: str, emoji: bool = True) -> None:
    output.info(message, emoji)


def warning(message: str, emoji: bool = True) -> None:
    output.warning(message, emoji)


def emphasize(message: str) -> None:
    output.emphasize(message)
