"""Tests for the command intro engine and the persistent session header."""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from tenxgraph_api.cli.core import animations as anim
from tenxgraph_api.cli.core.theme import ASCII_GLYPHS, UNICODE_GLYPHS


ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@pytest.fixture(autouse=True)
def _instant_frames(monkeypatch):
    """Run the choreography without spending its wall-clock motion budget."""
    monkeypatch.setattr(anim.time, "sleep", lambda _seconds: None)


def terminal(width: int = 100, height: int = 30) -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        height=height,
        legacy_windows=False,
    )
    return console, stream


def plain_tail(rendered: str) -> str:
    """Everything the user still sees after the alternate screen is released."""
    return ANSI.sub("", rendered.split(ALT_SCREEN_OFF)[-1])


def test_intro_releases_every_alternate_screen_it_claims() -> None:
    console, stream = terminal()
    anim.render_command_intro(console, command="dev", subtitle="Serve it", unicode=True)

    rendered = stream.getvalue()
    assert rendered.count(ALT_SCREEN_ON) == 1
    assert rendered.count(ALT_SCREEN_OFF) == 1
    assert rendered.index(ALT_SCREEN_ON) < rendered.index(ALT_SCREEN_OFF)


def test_intro_leaves_a_durable_header_in_the_normal_buffer() -> None:
    """The bug this guards: an alternate screen discards whatever it held."""
    console, stream = terminal()
    anim.render_command_intro(console, command="dev", subtitle="Serve it", unicode=True)

    survives = plain_tail(stream.getvalue())
    assert "dev" in survives
    assert "Serve it" in survives
    assert UNICODE_GLYPHS.rule in survives


def test_intro_falls_back_to_inline_motion_on_a_short_terminal() -> None:
    console, stream = terminal(height=8)
    anim.render_command_intro(console, command="init", subtitle=None, unicode=True)

    rendered = stream.getvalue()
    assert ALT_SCREEN_ON not in rendered
    assert "init" in ANSI.sub("", rendered)


def test_intro_does_not_claim_a_second_screen_when_one_is_already_held() -> None:
    console, stream = terminal()
    anim.render_command_intro(
        console,
        command="build",
        subtitle=None,
        unicode=True,
        persistent_screen=True,
    )
    assert ALT_SCREEN_ON not in stream.getvalue()


def test_intro_uses_ascii_only_output_when_unicode_is_unavailable() -> None:
    console, stream = terminal()
    anim.render_command_intro(console, command="audit", subtitle="Check it", unicode=False)

    assert ANSI.sub("", stream.getvalue()).isascii()


def test_session_header_reports_the_command_and_version() -> None:
    console, stream = terminal()
    anim.render_session_header(
        console,
        command="eval",
        subtitle="Score the agent",
        glyphs=UNICODE_GLYPHS,
        version="9.9.9",
    )

    rendered = ANSI.sub("", stream.getvalue())
    assert "10xgraph" in rendered
    assert "eval" in rendered
    assert "Score the agent" in rendered
    assert "9.9.9" in rendered


def test_session_header_omits_the_subtitle_line_when_absent() -> None:
    console, stream = terminal()
    anim.render_session_header(
        console,
        command="eval",
        subtitle=None,
        glyphs=UNICODE_GLYPHS,
    )
    lines = [line for line in ANSI.sub("", stream.getvalue()).splitlines() if line.strip()]
    assert len(lines) == 3


def test_wordmark_reveal_advances_with_progress() -> None:
    hidden = "".join(line.plain for line in anim._block_wordmark(0.0, 0.0, UNICODE_GLYPHS))
    partial = "".join(line.plain for line in anim._block_wordmark(0.5, 0.0, UNICODE_GLYPHS))
    full = "".join(line.plain for line in anim._block_wordmark(1.0, 0.0, UNICODE_GLYPHS))

    assert hidden.strip() == ""
    assert 0 < partial.count(UNICODE_GLYPHS.block) < full.count(UNICODE_GLYPHS.block)


def test_wordmark_uses_the_compact_form_without_block_glyphs() -> None:
    lines = anim._block_wordmark(1.0, 0.0, ASCII_GLYPHS)
    assert len(lines) == 1
    assert lines[0].plain.replace(" ", "") == "10XGRAPH"


def test_signature_pipeline_fills_in_step_with_progress() -> None:
    glyphs = UNICODE_GLYPHS
    empty = anim._signature_nodes("dev", 0.0, glyphs).plain
    done = anim._signature_nodes("dev", 1.0, glyphs).plain

    assert empty.count(glyphs.node) == 0
    assert done.count(glyphs.node) == len(anim._SIGNATURES["dev"])


def test_unknown_commands_get_the_default_signature_and_tagline() -> None:
    assert anim._signature_for("totally-new") == anim._DEFAULT_SIGNATURE
    console, stream = terminal()
    anim.render_command_intro(console, command="totally-new", subtitle=None, unicode=True)
    assert "totally-new" in plain_tail(stream.getvalue())


def test_phase_and_easing_stay_within_their_bounds() -> None:
    assert anim._phase(0.1, 0.2, 0.8) == 0.0
    assert anim._phase(0.9, 0.2, 0.8) == 1.0
    assert anim._phase(0.5, 0.0, 1.0) == pytest.approx(0.5)
    assert anim._ease_out(0.0) == pytest.approx(0.0)
    assert anim._ease_out(1.0) == pytest.approx(1.0)
    # Ease-out front-loads the motion, so it is always ahead of linear.
    assert anim._ease_out(0.5) > 0.5


def test_every_cinematic_frame_renders_without_error() -> None:
    console, stream = terminal()
    for index in range(11):
        console.print(
            anim._cinematic_frame(
                console,
                command="dev",
                tagline="Local agent runtime.",
                glyphs=UNICODE_GLYPHS,
                progress=index / 10,
            )
        )
    assert stream.getvalue()


def test_version_line_names_the_running_cli_core_and_python() -> None:
    import platform

    from tenxgraph_api.cli.constants import CLI_VERSION

    line = anim._version_line(1.0, UNICODE_GLYPHS).plain
    assert f"10xgraph-api {CLI_VERSION}" in line
    assert f"python {platform.python_version()}" in line


def test_intro_frame_shows_versions_and_the_skip_hint() -> None:
    console, _stream = terminal()
    frame = anim._cinematic_frame(
        console,
        command="dev",
        tagline="Serving",
        glyphs=UNICODE_GLYPHS,
        progress=1.0,
        can_skip=True,
    )
    console.print(frame)
    rendered = ANSI.sub("", _stream.getvalue())
    assert "10xgraph-api" in rendered
    assert "any key to skip" in rendered


def test_keypress_ends_the_intro_early(monkeypatch) -> None:
    import contextlib
    import time

    @contextlib.contextmanager
    def pressed():
        yield lambda: True

    monkeypatch.setattr(anim, "_skip_on_keypress", pressed)
    console, _stream = terminal()
    started = time.monotonic()
    anim._play_cinematic(console, command="dev", subtitle=None, glyphs=UNICODE_GLYPHS)
    assert time.monotonic() - started < anim.INTRO_DURATION_SECONDS / 2


def test_skip_poll_is_inert_without_a_terminal(monkeypatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO())
    with anim._skip_on_keypress() as skipped:
        assert skipped() is False
