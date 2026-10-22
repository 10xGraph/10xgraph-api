"""Behavioral tests for adaptive CLI output."""

from __future__ import annotations

import io
import json
import re
import sys
from types import SimpleNamespace

from tenxgraph_api.cli.capabilities import ColorMode, OutputFormat, ProgressMode
from tenxgraph_api.cli.core.output import (
    OutputFormatter,
    emphasize,
    error,
    info,
    output,
    print_banner,
    success,
    warning,
)
from tenxgraph_api.cli.core.theme import TENXGRAPH_THEME


def formatter(**kwargs) -> tuple[OutputFormatter, io.StringIO]:
    stream = io.StringIO()
    return OutputFormatter(stream=stream, **kwargs), stream


def test_initialization_default_and_custom_stream() -> None:
    assert OutputFormatter().stream == sys.stdout
    custom = io.StringIO()
    assert OutputFormatter(stream=custom).stream is custom


def test_plain_banner_contains_title_and_subtitle() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN)
    rendered.print_banner("Test Title", "Test Subtitle", width=100)
    value = stream.getvalue()
    assert "== Test Title ==" in value
    assert "Test Subtitle" in value


def test_semantic_messages_render_visible_text_and_symbols() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN)
    rendered.success("Operation successful")
    rendered.error("An error occurred")
    rendered.info("Information message")
    rendered.warning("Warning message")
    rendered.emphasize("Important message")
    value = stream.getvalue()
    for expected in (
        "Operation successful",
        "An error occurred",
        "Information message",
        "Warning message",
        "Important message",
    ):
        assert expected in value


def test_messages_can_omit_symbols() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN)
    rendered.success("success", emoji=False)
    rendered.error("error", emoji=False)
    rendered.info("info", emoji=False)
    rendered.warning("warning", emoji=False)
    assert stream.getvalue().splitlines() == ["success", "error", "info", "warning"]


def test_list_key_values_and_table_render_content() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN)
    rendered.print_list(["one", "two"], title="Items", bullet="-")
    rendered.print_key_value_pairs({"name": "Ada", "age": 30}, title="Person", indent=4)
    rendered.print_table(
        ["Name", "Age", "City"],
        [["Ada", "30"], ["Grace", "28", "New York", "ignored"]],
        title="People",
    )
    value = stream.getvalue()
    for expected in ("Items", "one", "two", "Person", "Ada", "30", "People", "New York"):
        assert expected in value


def test_empty_list_and_table_do_not_fail() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN)
    rendered.print_list([])
    rendered.print_table(["Name"], [])
    assert "Name" in stream.getvalue()


def test_jsonl_messages_are_versioned_objects() -> None:
    rendered, stream = formatter(output_format=OutputFormat.JSONL)
    rendered.success("done")
    rendered.print_table(["name"], [["agent"]], title="Agents")
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert payloads[0] == {
        "schema": "agentflow.cli/v1",
        "type": "success",
        "message": "done",
    }
    assert payloads[1]["type"] == "table"
    assert payloads[1]["rows"] == [{"name": "agent"}]


def test_quiet_suppresses_non_error_output() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN, quiet=True)
    rendered.print_banner("hidden")
    rendered.success("hidden")
    rendered.info("hidden")
    rendered.warning("hidden")
    rendered.print_list(["hidden"])
    rendered.error("visible", emoji=False)
    assert stream.getvalue() == "visible\n"


def test_no_color_produces_no_ansi_sequences() -> None:
    rendered, stream = formatter(
        output_format=OutputFormat.HUMAN,
        color_mode=ColorMode.NEVER,
    )
    rendered.success("done")
    assert "\x1b[" not in stream.getvalue()


def test_human_messages_use_styles_defined_in_the_theme(monkeypatch) -> None:
    # Rich renders an unknown style name as plain text without raising, so check the name.
    rendered = tty_formatter()
    printed: list[str] = []
    monkeypatch.setattr(
        rendered, "_console", lambda error=False: SimpleNamespace(print=printed.append)
    )
    rendered.success("done")
    rendered.error("boom")
    styles = {re.match(r"\[([^\]]+)\]", markup).group(1) for markup in printed}
    assert styles == {"tenxgraph.success", "tenxgraph.error"}
    assert styles <= set(TENXGRAPH_THEME.styles)


def test_plain_status_degrades_to_a_checkpoint() -> None:
    rendered, stream = formatter(
        output_format=OutputFormat.PLAIN,
        progress_mode=ProgressMode.PLAIN,
    )
    with rendered.status("Loading"):
        pass
    assert "Loading" in stream.getvalue()


def test_plain_command_header_degrades_to_static_banner() -> None:
    rendered, stream = formatter(
        output_format=OutputFormat.PLAIN,
        progress_mode=ProgressMode.PLAIN,
    )
    rendered.command_header("dev", "Start the playground")
    assert "== Dev ==" in stream.getvalue()
    assert "Start the playground" in stream.getvalue()


def test_forced_tty_header_uses_animation_renderer(monkeypatch) -> None:
    class TTYStream(io.StringIO):
        def isatty(self) -> bool:
            return True

    calls: list[tuple[str, str | None, bool, bool]] = []
    monkeypatch.setattr(
        "tenxgraph_api.cli.core.output.render_command_intro",
        lambda _console, *, command, subtitle, unicode, persistent_screen: calls.append(
            (command, subtitle, unicode, persistent_screen)
        ),
    )
    stream = TTYStream()
    rendered = OutputFormatter(
        stream=stream,
        output_format=OutputFormat.HUMAN,
        progress_mode=ProgressMode.TTY,
    )
    rendered.command_header("dev", "Start the playground")
    # The intro plays by default, on a temporary screen (not the opt-in pinned frame).
    assert calls == [("dev", "Start the playground", True, False)]


class FakeConsole:
    """Minimal console double that records the terminal control it receives."""

    is_terminal = True
    width = 100
    height = 30

    def __init__(self) -> None:
        self.file = io.StringIO()
        self.alt_screen_calls: list[bool] = []
        self.cursor_shown: list[bool] = []

    # Mirrors Rich: True when the alternate buffer was actually entered.
    alt_screen_supported = True

    def set_alt_screen(self, enable: bool) -> bool:
        self.alt_screen_calls.append(enable)
        return self.alt_screen_supported

    def show_cursor(self, show: bool) -> None:
        self.cursor_shown.append(show)

    def print(self, *_args, **_kwargs) -> None:
        return None


def test_fullscreen_session_owns_and_restores_alternate_screen(monkeypatch) -> None:
    rendered, _stream = formatter(progress_mode=ProgressMode.TTY, color_mode=ColorMode.ALWAYS)
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)

    assert rendered.start_fullscreen_session() is True
    assert rendered.fullscreen_active is True
    assert console.alt_screen_calls == [True]
    # A second call must not stack a nested screen on the first.
    assert rendered.start_fullscreen_session() is False

    rendered.end_fullscreen_session()
    assert rendered.fullscreen_active is False
    assert console.alt_screen_calls == [True, False]
    assert console.cursor_shown == [True]
    # The scrolling region must be released, or the user's shell inherits it.
    assert "\x1b[r" in console.file.getvalue()


def test_fullscreen_session_is_skipped_off_a_terminal(monkeypatch) -> None:
    class NonTerminalConsole(FakeConsole):
        is_terminal = False

    rendered, _stream = formatter(progress_mode=ProgressMode.TTY)
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: NonTerminalConsole())

    assert rendered.start_fullscreen_session() is False
    assert rendered.fullscreen_active is False


def test_fullscreen_session_is_skipped_on_a_terminal_too_short_for_chrome(monkeypatch) -> None:
    class ShortConsole(FakeConsole):
        height = 8

    rendered, _stream = formatter(progress_mode=ProgressMode.TTY, color_mode=ColorMode.ALWAYS)
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: ShortConsole())

    assert rendered.start_fullscreen_session() is False


def test_fullscreen_session_writes_nothing_when_the_console_refuses(monkeypatch) -> None:
    """A legacy console claims to be a terminal but rejects the alternate buffer.

    Painting anyway would leave a scrolling region on the user's real scrollback,
    which outlives the process.
    """

    class RefusingConsole(FakeConsole):
        alt_screen_supported = False

    rendered, _stream = formatter(progress_mode=ProgressMode.TTY, color_mode=ColorMode.ALWAYS)
    console = RefusingConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)

    assert rendered.start_fullscreen_session() is False
    assert rendered.fullscreen_active is False
    assert console.file.getvalue() == ""


class TTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def tty_formatter() -> OutputFormatter:
    """A formatter that stays in human mode, as it would on a real terminal."""
    return OutputFormatter(
        stream=TTYStream(),
        progress_mode=ProgressMode.TTY,
        color_mode=ColorMode.ALWAYS,
    )


def test_requesting_fullscreen_does_not_claim_the_screen_on_its_own(monkeypatch) -> None:
    """Script-shaped commands print and exit without ever taking the terminal."""
    rendered = tty_formatter()
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)

    rendered.request_fullscreen(True)
    rendered.success("done")

    assert rendered.fullscreen_active is False
    assert console.alt_screen_calls == []
    # Closing without a session must stay a no-op, including the Enter pause.
    rendered.end_fullscreen_session()
    assert console.alt_screen_calls == []


def test_command_header_claims_the_screen_when_fullscreen_was_requested(monkeypatch) -> None:
    rendered = tty_formatter()
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)
    monkeypatch.setattr(
        "tenxgraph_api.cli.core.output.render_command_intro",
        lambda *_args, **_kwargs: None,
    )

    rendered.request_fullscreen(True)
    rendered.command_header("dev", "Serve it")
    assert rendered.fullscreen_active is True
    assert console.alt_screen_calls == [True]


def test_command_header_stays_in_the_normal_buffer_without_fullscreen(monkeypatch) -> None:
    rendered = tty_formatter()
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)
    monkeypatch.setattr(
        "tenxgraph_api.cli.core.output.render_command_intro",
        lambda *_args, **_kwargs: None,
    )

    rendered.request_fullscreen(False)
    rendered.command_header("dev", "Serve it")
    assert rendered.fullscreen_active is False
    assert console.alt_screen_calls == []


def test_command_header_pins_chrome_inside_a_fullscreen_session(monkeypatch) -> None:
    # A real TTY stream, so the formatter stays in human mode and reaches the frame.
    rendered = tty_formatter()
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)
    monkeypatch.setattr(
        "tenxgraph_api.cli.core.output.render_command_intro",
        lambda *_args, **_kwargs: None,
    )

    assert rendered.start_fullscreen_session() is True
    rendered.command_header("dev", "Serve it", hint="Ctrl+C to stop")

    written = console.file.getvalue()
    # Header, footer, and a scrolling region confining output between them.
    assert "dev" in written
    assert "Ctrl+C to stop" in written
    assert f"\x1b[5;{console.height - 2}r" in written


def test_timeline_and_progress_pick_the_renderer_for_the_output_mode() -> None:
    from tenxgraph_api.cli.core.steps import (
        LiveProgressRun,
        LiveTimeline,
        QuietProgressRun,
        QuietTimeline,
        StaticProgressRun,
        StaticTimeline,
        StructuredProgressRun,
        StructuredTimeline,
    )

    quiet, _ = formatter(quiet=True)
    assert isinstance(quiet.timeline("t"), QuietTimeline)
    assert isinstance(quiet.progress_run("p", total=1), QuietProgressRun)

    structured, _ = formatter(output_format=OutputFormat.JSONL)
    assert isinstance(structured.timeline("t"), StructuredTimeline)
    assert isinstance(structured.progress_run("p", total=1), StructuredProgressRun)

    plain, _ = formatter(output_format=OutputFormat.PLAIN, progress_mode=ProgressMode.PLAIN)
    assert isinstance(plain.timeline("t"), StaticTimeline)
    assert isinstance(plain.progress_run("p", total=1), StaticProgressRun)

    animated, _ = formatter(progress_mode=ProgressMode.TTY)
    assert isinstance(animated.timeline("t"), LiveTimeline)
    assert isinstance(animated.progress_run("p", total=1), LiveProgressRun)


def test_plain_timeline_reports_every_stage_transition() -> None:
    rendered, stream = formatter(
        output_format=OutputFormat.PLAIN,
        progress_mode=ProgressMode.PLAIN,
    )
    timeline = rendered.timeline("Working", steps=(("a", "First stage"),))
    with timeline, timeline.step("a") as step:
        step.detail("all good")

    value = stream.getvalue()
    assert "Working" in value
    assert value.count("First stage") == 2
    assert "all good" in value


def test_structured_timeline_stays_valid_json_per_line() -> None:
    rendered, stream = formatter(output_format=OutputFormat.JSONL)
    timeline = rendered.timeline("Working", steps=(("a", "First stage"),))
    with timeline, timeline.step("a"):
        pass

    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [payload["type"] for payload in payloads] == [
        "timeline_start",
        "step_start",
        "step_end",
        "timeline_end",
    ]
    assert all(payload["schema"] == "agentflow.cli/v1" for payload in payloads)


def test_quiet_timeline_writes_nothing() -> None:
    rendered, stream = formatter(output_format=OutputFormat.PLAIN, quiet=True)
    timeline = rendered.timeline("Working", steps=(("a", "First stage"),))
    with timeline, timeline.step("a") as step:
        step.detail("hidden")
    assert stream.getvalue() == ""


def test_completion_screen_reveals_rows_progressively() -> None:
    rendered, _stream = formatter()
    rows = [("API", "http://localhost:8000"), ("Config", "10xgraph.json")]
    steps = ["Open the docs"]

    first = rendered._completion_panel("Ready", "All set", rows, steps, 0)
    assert "API" not in first.renderable.plain

    partial = rendered._completion_panel("Ready", "All set", rows, steps, 1)
    assert "API" in partial.renderable.plain
    assert "Config" not in partial.renderable.plain

    full = rendered._completion_panel("Ready", "All set", rows, steps, len(rows) + len(steps))
    assert "Config" in full.renderable.plain
    assert "Open the docs" in full.renderable.plain


def test_structured_activity_emits_lifecycle_events() -> None:
    rendered, stream = formatter(output_format=OutputFormat.JSONL)
    with rendered.activity("Loading graph", done="Graph loaded"):
        pass
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [payload["type"] for payload in payloads] == [
        "progress_start",
        "progress_end",
        "success",
    ]
    assert payloads[1]["status"] == "completed"


def test_structured_completion_screen_is_one_event() -> None:
    rendered, stream = formatter(output_format=OutputFormat.JSONL)
    rendered.completion_screen(
        "Ready",
        "Server configured",
        details={"API": "http://localhost:8000"},
        next_steps=["Open docs"],
    )
    payload = json.loads(stream.getvalue())
    assert payload["type"] == "completion"
    assert payload["title"] == "Ready"
    assert payload["details"]["API"] == "http://localhost:8000"


def test_global_convenience_functions_delegate(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        output, "print_banner", lambda value, *a, **k: calls.append(("banner", value))
    )
    monkeypatch.setattr(output, "success", lambda value, *a, **k: calls.append(("success", value)))
    monkeypatch.setattr(output, "error", lambda value, *a, **k: calls.append(("error", value)))
    monkeypatch.setattr(output, "info", lambda value, *a, **k: calls.append(("info", value)))
    monkeypatch.setattr(output, "warning", lambda value, *a, **k: calls.append(("warning", value)))
    monkeypatch.setattr(output, "emphasize", lambda value: calls.append(("emphasize", value)))

    print_banner("a")
    success("b")
    error("c")
    info("d")
    warning("e")
    emphasize("f")

    assert calls == [
        ("banner", "a"),
        ("success", "b"),
        ("error", "c"),
        ("info", "d"),
        ("warning", "e"),
        ("emphasize", "f"),
    ]


def test_global_instance_exists() -> None:
    assert isinstance(output, OutputFormatter)


def test_ctrl_c_releases_the_fullscreen_frame_without_waiting(monkeypatch) -> None:
    from tenxgraph_api.cli.core import screen

    rendered, _stream = formatter(progress_mode=ProgressMode.TTY, color_mode=ColorMode.ALWAYS)
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)
    waited: list[bool] = []
    monkeypatch.setattr(
        screen.AppFrame, "_wait_for_enter", staticmethod(lambda: waited.append(True))
    )

    assert rendered.start_fullscreen_session() is True
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        # The CLI context's close callback runs while the interrupt is in flight.
        rendered.end_fullscreen_session()

    assert waited == []
    assert console.alt_screen_calls == [True, False]


def test_normal_finish_still_pauses_before_discarding_the_frame(monkeypatch) -> None:
    from tenxgraph_api.cli.core import screen

    rendered, _stream = formatter(progress_mode=ProgressMode.TTY, color_mode=ColorMode.ALWAYS)
    console = FakeConsole()
    monkeypatch.setattr(rendered, "_console", lambda **_kwargs: console)
    waited: list[bool] = []
    monkeypatch.setattr(
        screen.AppFrame, "_wait_for_enter", staticmethod(lambda: waited.append(True))
    )

    assert rendered.start_fullscreen_session() is True
    rendered.end_fullscreen_session()

    assert waited == [True]
