"""`10xgraph init` checks for existing files before it writes anything."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.core.output import OutputFormatter


class FakePrompts:
    interactive = True

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def confirm(self, message: str, *, default: bool = False) -> bool | None:
        self.asked.append(message)
        return self.answer


def _scaffold(path: Path) -> None:
    code = InitCommand(OutputFormatter(stream=io.StringIO())).execute(path=str(path), yes=True)
    assert code == 0


def _snapshot(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): p.read_text(encoding="utf-8")
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _interactive(monkeypatch: pytest.MonkeyPatch, answer: bool | None) -> FakePrompts:
    prompts = FakePrompts(answer)
    context = InitCommand()._non_interactive_context(path=".", mode="quick", agent_name="My Agent")
    monkeypatch.setattr(InitCommand, "_prompt_user", lambda self: context)
    monkeypatch.setattr(OutputFormatter, "prompts", lambda self, interactive=None: prompts)
    return prompts


def test_no_existing_files_asks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _interactive(monkeypatch, True)
    code = InitCommand(OutputFormatter(stream=io.StringIO())).execute(path=str(tmp_path))
    assert code == 0
    assert prompts.asked == []
    assert (tmp_path / "10xgraph.json").exists()


def test_interactive_yes_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold(tmp_path)
    (tmp_path / "graph" / "agent.py").write_text("mine", encoding="utf-8")
    (tmp_path / "10xgraph.json").write_text("{}", encoding="utf-8")
    prompts = _interactive(monkeypatch, True)

    code = InitCommand(OutputFormatter(stream=io.StringIO())).execute(path=str(tmp_path))

    assert code == 0
    assert len(prompts.asked) == 1
    assert "10xgraph.json" in prompts.asked[0]
    assert (tmp_path / "graph" / "agent.py").read_text(encoding="utf-8") != "mine"
    assert (tmp_path / "10xgraph.json").read_text(encoding="utf-8") != "{}"


@pytest.mark.parametrize("answer", [False, None])
def test_interactive_no_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: bool | None
) -> None:
    _scaffold(tmp_path)
    (tmp_path / "graph" / "agent.py").write_text("mine", encoding="utf-8")
    (tmp_path / "graph" / "agent.py").unlink()
    before = _snapshot(tmp_path)
    _interactive(monkeypatch, answer)
    stream = io.StringIO()

    code = InitCommand(OutputFormatter(stream=stream)).execute(path=str(tmp_path))

    assert code == 0
    assert "No files were written" in stream.getvalue()
    assert _snapshot(tmp_path) == before


def test_non_interactive_without_force_fails_before_writing(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    (tmp_path / "graph" / "agent.py").unlink()
    before = _snapshot(tmp_path)
    stream = io.StringIO()

    code = InitCommand(OutputFormatter(stream=stream)).execute(path=str(tmp_path), yes=True)

    assert code != 0
    assert "10xgraph.json" in stream.getvalue()
    assert "--force" in stream.getvalue()
    assert _snapshot(tmp_path) == before


def test_force_overwrites_without_asking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold(tmp_path)
    (tmp_path / "graph" / "agent.py").write_text("mine", encoding="utf-8")
    prompts = _interactive(monkeypatch, False)

    code = InitCommand(OutputFormatter(stream=io.StringIO())).execute(
        path=str(tmp_path), force=True
    )

    assert code == 0
    assert prompts.asked == []
    assert (tmp_path / "graph" / "agent.py").read_text(encoding="utf-8") != "mine"


def test_dry_run_lists_existing_files(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    stream = io.StringIO()
    code = InitCommand(OutputFormatter(stream=stream)).execute(
        path=str(tmp_path), yes=True, dry_run=True
    )
    assert code == 0
    assert "10xgraph.json (exists)" in stream.getvalue()
