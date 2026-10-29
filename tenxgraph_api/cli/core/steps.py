"""Live step timelines for multi-stage CLI commands.

A command declares its stages up front, so the user sees the whole plan the
moment work starts and watches it resolve — rather than a single spinner that
reveals nothing about what remains. The timeline renders through one Rich
``Live`` region and is left on screen when the command finishes, so the final
state stays in scrollback.

Three implementations share one protocol:

``LiveTimeline``     animated, for interactive terminals
``StaticTimeline``   one line per transition, for pipes, CI, and plain mode
``StructuredTimeline`` versioned events, for ``--format json``/``jsonl``
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from tenxgraph_api.cli.core.theme import ACCENT, LINE, SUCCESS, Glyphs, sample_ramp


_SPINNER_FPS = 12
_REFRESH_PER_SECOND = 15
_ELAPSED_COLUMN = 62
_SECONDS_PER_MINUTE = 60


class StepState(StrEnum):
    """Lifecycle of one stage in a timeline."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Step:
    """One declared stage and everything needed to render its current state."""

    key: str
    title: str
    state: StepState = StepState.PENDING
    detail: str = ""
    started: float | None = None
    finished: float | None = None

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished or time.monotonic()) - self.started


class StepHandle:
    """Handle a command uses to annotate the stage it is currently running."""

    def __init__(self, step: Step, on_change: Callable[[], None]) -> None:
        self._step = step
        self._on_change = on_change

    def detail(self, message: str) -> None:
        """Replace the sub-line under the active stage."""
        self._step.detail = message
        self._on_change()

    def skip(self, reason: str = "") -> None:
        """Mark this stage as intentionally not performed."""
        self._step.state = StepState.SKIPPED
        if reason:
            self._step.detail = reason
        self._on_change()

    def fail(self, reason: str = "") -> None:
        """Mark this stage failed without aborting the rest of the timeline.

        Raising would be the usual way to fail a stage, but some commands want
        to record one failure and carry on with the remaining work.
        """
        self._step.state = StepState.FAILED
        if reason:
            self._step.detail = reason
        self._on_change()


class Timeline(Protocol):
    """Shared surface across animated, plain, and structured renderers."""

    def __enter__(self) -> Timeline: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def add(self, key: str, title: str) -> None: ...

    def step(self, key: str, title: str | None = None) -> Any: ...


class ProgressRun(Protocol):
    """Determinate progress surface shared across output modes."""

    def __enter__(self) -> ProgressRun: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def record(self, label: str, *, status: str, detail: str = "") -> None: ...

    def step(self, label: str, message: str) -> None: ...


_STEP_MESSAGE_LIMIT = 100
_LINE_BREAKS = re.compile(r"\s*[\r\n\t]+\s*")


def one_line(text: str, limit: int = _STEP_MESSAGE_LIMIT) -> str:
    """Collapse whitespace and newlines and cut ``text`` to at most ``limit`` characters."""
    collapsed = _LINE_BREAKS.sub(" ", str(text)).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def format_elapsed(seconds: float) -> str:
    """Render a duration compactly enough to sit in a fixed-width column."""
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), _SECONDS_PER_MINUTE)
    return f"{minutes}m {remainder:02d}s"


class _BaseTimeline:
    """State machine shared by every timeline renderer."""

    def __init__(self, steps: Sequence[tuple[str, str]] = ()) -> None:
        self._steps: list[Step] = [Step(key=key, title=title) for key, title in steps]
        self._index: dict[str, Step] = {step.key: step for step in self._steps}

    def add(self, key: str, title: str) -> None:
        """Append a stage discovered while the timeline is already running."""
        if key in self._index:
            self._index[key].title = title
            return
        step = Step(key=key, title=title)
        self._steps.append(step)
        self._index[key] = step
        self._changed()

    def _resolve(self, key: str, title: str | None) -> Step:
        step = self._index.get(key)
        if step is None:
            step = Step(key=key, title=title or key)
            self._steps.append(step)
            self._index[key] = step
        elif title:
            step.title = title
        return step

    def _changed(self) -> None:
        """Hook for renderers that need to repaint on every state change."""

    @contextmanager
    def _run(self, key: str, title: str | None) -> Iterator[StepHandle]:
        step = self._resolve(key, title)
        step.state = StepState.ACTIVE
        step.started = time.monotonic()
        step.finished = None
        self._on_start(step)
        handle = StepHandle(step, self._changed)
        try:
            yield handle
        except BaseException:
            step.finished = time.monotonic()
            step.state = StepState.FAILED
            self._on_finish(step)
            raise
        step.finished = time.monotonic()
        # A stage that already reported skip() or fail() keeps that verdict.
        if step.state is StepState.ACTIVE:
            step.state = StepState.DONE
        self._on_finish(step)

    def step(self, key: str, title: str | None = None) -> Any:
        """Run one stage as a context manager."""
        return self._run(key, title)

    def _on_start(self, step: Step) -> None:
        self._changed()

    def _on_finish(self, step: Step) -> None:
        self._changed()

    @property
    def failed(self) -> bool:
        return any(step.state is StepState.FAILED for step in self._steps)


class LiveTimeline(_BaseTimeline):
    """Animated timeline for interactive terminals."""

    def __init__(
        self,
        console: Console,
        *,
        glyphs: Glyphs,
        title: str | None = None,
        steps: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(steps)
        self._console = console
        self._glyphs = glyphs
        self._title = title
        self._live: Live | None = None

    def __enter__(self) -> LiveTimeline:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        live = self._live
        self._live = None
        if live is None:
            return
        # Paint the resolved state before releasing the region so the final
        # frame is what stays in scrollback.
        live.update(self._render(), refresh=True)
        live.__exit__(exc_type, exc, traceback)
        self._console.print()

    def _changed(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=False)

    def _render(self) -> RenderableType:
        glyphs = self._glyphs
        lines: list[RenderableType] = []

        if self._title:
            heading = Text("  ")
            heading.append(f"{glyphs.diamond} ", style="tenxgraph.brand")
            heading.append(self._title, style="tenxgraph.command")
            lines.append(heading)

        total = len(self._steps)
        for index, step in enumerate(self._steps):
            last = index == total - 1
            lines.append(self._render_step(step, last=last))
            if step.detail:
                lines.append(self._render_detail(step, last=last))
        return Group(*lines)

    def _render_step(self, step: Step, *, last: bool) -> Text:
        glyphs = self._glyphs
        connector = glyphs.tree_end if last else glyphs.tree_branch
        line = Text("  ")
        line.append(f"{connector}{glyphs.thin_rule} ", style="tenxgraph.rule")
        line.append_text(self._symbol(step))
        line.append(" ")
        line.append(step.title, style=self._title_style(step))

        if step.state in {StepState.ACTIVE, StepState.DONE, StepState.FAILED}:
            elapsed = format_elapsed(step.elapsed)
            padding = max(_ELAPSED_COLUMN - line.cell_len, 1)
            line.append(" " * padding)
            line.append(elapsed, style="tenxgraph.elapsed")
        return line

    def _render_detail(self, step: Step, *, last: bool) -> Text:
        glyphs = self._glyphs
        stem = " " if last else glyphs.tree_stem
        line = Text("  ")
        line.append(f"{stem}   ", style="tenxgraph.rule")
        line.append(step.detail, style="tenxgraph.muted")
        return line

    def _symbol(self, step: Step) -> Text:
        glyphs = self._glyphs
        if step.state is StepState.DONE:
            return Text(glyphs.check, style="tenxgraph.success")
        if step.state is StepState.FAILED:
            return Text(glyphs.cross, style="tenxgraph.error")
        if step.state is StepState.SKIPPED:
            return Text(glyphs.skipped, style="tenxgraph.muted")
        if step.state is StepState.ACTIVE:
            frames = glyphs.spinner
            frame = frames[int(time.monotonic() * _SPINNER_FPS) % len(frames)]
            # Cycle the brand ramp with the spinner so the active row reads as
            # the one live element on screen.
            tint = sample_ramp((time.monotonic() * 0.5) % 1.0)
            return Text(frame, style=f"bold {tint}")
        return Text(glyphs.pending, style="tenxgraph.pending")

    @staticmethod
    def _title_style(step: Step) -> str:
        return {
            StepState.DONE: "tenxgraph.command",
            StepState.ACTIVE: "bold",
            StepState.FAILED: "tenxgraph.error",
            StepState.SKIPPED: "tenxgraph.muted",
            StepState.PENDING: "tenxgraph.pending",
        }[step.state]


class StaticTimeline(_BaseTimeline):
    """Line-per-transition timeline for pipes, CI, and plain output."""

    def __init__(
        self,
        *,
        glyphs: Glyphs,
        emit: Callable[[str], None],
        title: str | None = None,
        steps: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(steps)
        self._glyphs = glyphs
        self._emit = emit
        self._title = title

    def __enter__(self) -> StaticTimeline:
        if self._title:
            self._emit(f"{self._glyphs.diamond} {self._title}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def _on_start(self, step: Step) -> None:
        self._emit(f"{self._glyphs.arrow} {step.title}")

    def _on_finish(self, step: Step) -> None:
        symbol = {
            StepState.DONE: self._glyphs.check,
            StepState.FAILED: self._glyphs.cross,
            StepState.SKIPPED: self._glyphs.skipped,
        }.get(step.state, self._glyphs.bullet)
        suffix = f" ({format_elapsed(step.elapsed)})" if step.state is not StepState.SKIPPED else ""
        detail = f" — {step.detail}" if step.detail else ""
        self._emit(f"{symbol} {step.title}{suffix}{detail}")


class StructuredTimeline(_BaseTimeline):
    """Versioned event stream for ``--format json`` and ``--format jsonl``."""

    def __init__(
        self,
        *,
        emit: Callable[[str, str, dict[str, Any]], None],
        title: str | None = None,
        steps: Sequence[tuple[str, str]] = (),
    ) -> None:
        super().__init__(steps)
        self._emit = emit
        self._title = title

    def __enter__(self) -> StructuredTimeline:
        self._emit(
            "timeline_start",
            self._title or "",
            {"steps": [step.key for step in self._steps]},
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._emit(
            "timeline_end",
            self._title or "",
            {"status": "failed" if self.failed else "completed"},
        )

    def _on_start(self, step: Step) -> None:
        self._emit("step_start", step.title, {"step": step.key})

    def _on_finish(self, step: Step) -> None:
        self._emit(
            "step_end",
            step.title,
            {
                "step": step.key,
                "status": str(step.state),
                "duration_seconds": round(step.elapsed, 3),
                "detail": step.detail,
            },
        )


class QuietTimeline(_BaseTimeline):
    """No-op timeline used when output is suppressed."""

    def __enter__(self) -> QuietTimeline:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _BaseProgressRun:
    """Determinate progress over a known number of items, with a live tally.

    Used where a timeline would not fit: many short, homogeneous units of work
    whose individual names matter less than how many have passed so far.
    """

    def __init__(self, *, total: int, title: str) -> None:
        self.total = total
        self.title = title
        self.completed = 0
        self.tally: dict[str, int] = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}

    def __enter__(self) -> _BaseProgressRun:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def record(self, label: str, *, status: str, detail: str = "") -> None:
        """Register one finished item and repaint."""
        self.completed += 1
        if status in self.tally:
            self.tally[status] += 1
        self._render(label, status, detail)

    def step(self, label: str, message: str) -> None:
        """Show an in-progress line for ``label`` without advancing the bar or tally."""
        self._render_step(label, one_line(message))

    def _render(self, label: str, status: str, detail: str) -> None:
        """Emit whatever this output mode shows for one finished item."""

    def _render_step(self, label: str, message: str) -> None:
        """Emit whatever this output mode shows for one in-progress line."""


class LiveProgressRun(_BaseProgressRun):
    """Animated bar with per-item result lines printed above it."""

    _STATUS_STYLES = {
        "passed": "tenxgraph.success",
        "failed": "tenxgraph.error",
        "error": "tenxgraph.error",
        "skipped": "tenxgraph.muted",
    }

    def __init__(
        self,
        console: Console,
        *,
        total: int,
        title: str,
        glyphs: Glyphs,
    ) -> None:
        super().__init__(total=total, title=title)
        self._console = console
        self._glyphs = glyphs
        self._progress: Any | None = None
        self._task: Any | None = None

    def __enter__(self) -> LiveProgressRun:
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeElapsedColumn,
        )

        self._progress = Progress(
            SpinnerColumn(spinner_name="dots12", style="tenxgraph.brand"),
            TextColumn("[tenxgraph.command]{task.description}"),
            BarColumn(
                bar_width=None,
                style=LINE,
                complete_style=ACCENT,
                finished_style=SUCCESS,
            ),
            TextColumn("[tenxgraph.muted]{task.completed}/{task.total}"),
            TextColumn("{task.fields[tally]}"),
            TimeElapsedColumn(),
            console=self._console,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._progress.__enter__()
        self._task = self._progress.add_task(self.title, total=self.total, tally="")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        progress = self._progress
        self._progress = None
        if progress is not None:
            progress.__exit__(exc_type, exc, traceback)
            self._console.print()

    def _render(self, label: str, status: str, detail: str) -> None:
        glyphs = self._glyphs
        symbol = glyphs.check if status == "passed" else glyphs.cross
        style = self._STATUS_STYLES.get(status, "tenxgraph.muted")

        line = Text(f"  {symbol} ", style=style)
        line.append(label, style="tenxgraph.command")
        if detail:
            line.append(f"  {detail}", style="tenxgraph.muted")
        # Printed through the progress object so Rich scrolls it above the bar
        # instead of fighting the bar for the same terminal row.
        if self._progress is not None and self._task is not None:
            self._progress.console.print(line)
            self._progress.update(self._task, completed=self.completed, tally=self._tally_text())

    def _render_step(self, label: str, message: str) -> None:
        if self._progress is None:
            return
        line = Text("    ")
        line.append(f"{label}  ", style="tenxgraph.muted")
        line.append(message, style="tenxgraph.muted")
        self._progress.console.print(line)

    def _tally_text(self) -> Text:
        glyphs = self._glyphs
        text = Text()
        text.append(f"{glyphs.check}{self.tally['passed']}", style="tenxgraph.success")
        failures = self.tally["failed"] + self.tally["error"]
        if failures:
            text.append(f" {glyphs.cross}{failures}", style="tenxgraph.error")
        return text


class StaticProgressRun(_BaseProgressRun):
    """One line per finished item, for pipes, CI, and plain output."""

    def __init__(
        self,
        *,
        total: int,
        title: str,
        glyphs: Glyphs,
        emit: Callable[[str], None],
    ) -> None:
        super().__init__(total=total, title=title)
        self._glyphs = glyphs
        self._emit = emit

    def __enter__(self) -> StaticProgressRun:
        self._emit(f"{self._glyphs.diamond} {self.title} ({self.total})")
        return self

    def _render(self, label: str, status: str, detail: str) -> None:
        symbol = self._glyphs.check if status == "passed" else self._glyphs.cross
        suffix = f"  {detail}" if detail else ""
        self._emit(f"[{self.completed:>3}/{self.total}] {symbol} {label}  {status.upper()}{suffix}")

    def _render_step(self, label: str, message: str) -> None:
        self._emit(f"      {label}  {message}")


class StructuredProgressRun(_BaseProgressRun):
    """Versioned per-item events for ``--format json`` and ``--format jsonl``."""

    def __init__(
        self,
        *,
        total: int,
        title: str,
        emit: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        super().__init__(total=total, title=title)
        self._emit = emit

    def __enter__(self) -> StructuredProgressRun:
        self._emit("progress_start", self.title, {"total": self.total})
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._emit("progress_end", self.title, {"total": self.total, **self.tally})

    def _render(self, label: str, status: str, detail: str) -> None:
        self._emit(
            "progress",
            label,
            {
                "status": status,
                "detail": detail,
                "completed": self.completed,
                "total": self.total,
            },
        )

    def _render_step(self, label: str, message: str) -> None:
        self._emit("eval_step", label, {"message": message})


class QuietProgressRun(_BaseProgressRun):
    """No-op progress used when output is suppressed."""
