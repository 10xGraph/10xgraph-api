"""Tests for the side-effect-free terminal animation showcase."""

from __future__ import annotations

import io

from tenxgraph_api.cli.commands import demo as demo_module
from tenxgraph_api.cli.commands.demo import DemoCommand
from tenxgraph_api.cli.core.output import OutputFormatter


def test_demo_renders_all_themes_without_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(demo_module.time, "sleep", lambda _seconds: None)
    stream = io.StringIO()
    command = DemoCommand(OutputFormatter(stream=stream))

    assert command.execute() == 0
    rendered = stream.getvalue()
    for theme in ("Typing", "Dev", "Init", "Build", "Eval"):
        assert theme in rendered
    assert "All preview states rendered successfully" in rendered
    assert "none" in rendered


def test_demo_rejects_unknown_style() -> None:
    stream = io.StringIO()
    command = DemoCommand(OutputFormatter(stream=stream))

    assert command.execute(style="unknown") != 0
    assert "Unknown animation style" in stream.getvalue()


def test_typing_style_selects_typing_theme(monkeypatch) -> None:
    monkeypatch.setattr(demo_module.time, "sleep", lambda _seconds: None)
    stream = io.StringIO()
    command = DemoCommand(OutputFormatter(stream=stream))

    assert command.execute(style="typing") == 0
    assert "Typing" in stream.getvalue()
