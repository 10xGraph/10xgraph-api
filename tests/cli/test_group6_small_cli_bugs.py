"""G-121: small CLI bugs."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest

from tenxgraph_api.cli.commands.eval import EvalCommand
from tenxgraph_api.cli.commands.init import InitCommand
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.exceptions import FileOperationError, ValidationError


def _out() -> OutputFormatter:
    return OutputFormatter(stream=io.StringIO(), quiet=True)


def test_agent_name_with_quotes_does_not_break_pyproject(tmp_path: Path) -> None:
    cmd = InitCommand(output=_out())
    context = cmd._non_interactive_context(
        path=str(tmp_path),
        agent_name='My "Agent" \\ x',
        template="production",
        auth="jwt",
        rate_limit="memory",
    )
    template = Path(__file__).parents[2] / "tenxgraph_api/cli/templates/prod/pyproject.toml"
    rendered = cmd._render(template, context, True)
    assert tomllib.loads(rendered)["project"]["description"].startswith('My "Agent" \\ x')
    env = cmd._render(template.with_name(".env.example"), context, True)
    assert 'APP_NAME="My \\"Agent\\" \\\\ x"' in env


@pytest.mark.parametrize("name", ["bad\nname", "tab\tname", "nul\x00"])
def test_agent_name_with_control_characters_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        InitCommand(output=_out())._non_interactive_context(
            path=".", agent_name=name, template="production", auth="jwt", rate_limit="memory"
        )


def test_render_failure_is_reported_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    from tenxgraph_api.cli.commands import init as init_mod

    def boom(_context):
        raise KeyError("provider")

    monkeypatch.setattr(init_mod, "_template_vars", boom)
    src = tmp_path / "f.txt"
    src.write_text("$agent_name", encoding="utf-8")
    with pytest.raises(FileOperationError):
        InitCommand(output=_out())._render(src, {"agent_name": "x", "auth": "none"}, False)


def test_confeval_is_not_searched_above_the_project(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside" / "evals"
    project.mkdir()
    outside.mkdir(parents=True)
    (tmp_path / "confeval.py").write_text("raise SystemExit('loaded')", encoding="utf-8")
    monkeypatch.chdir(project)
    dirs = EvalCommand(output=_out())._confeval_search_dirs(outside, project / "evals")
    assert tmp_path.resolve() not in dirs
    assert tmp_path.parent.resolve() not in dirs
    assert dirs[0] == outside.resolve()


def test_skills_force_keeps_the_old_install_when_the_copy_fails(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil

    from tenxgraph_api.cli.commands import skills as skills_mod

    cmd = skills_mod.SkillsCommand(output=_out())
    templates_root = Path(skills_mod.__file__).parents[1] / "templates" / "skills"
    target = next(t for t in skills_mod._TARGETS if any(a.kind == "folder" for a in t.artifacts))
    cmd._install_one(templates_root, tmp_path, target, force=False)
    folder = next(tmp_path / a.install_relpath for a in target.artifacts if a.kind == "folder")
    marker = folder / "keep.txt"
    marker.write_text("old", encoding="utf-8")

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", fail)
    with pytest.raises(OSError):
        cmd._install_one(templates_root, tmp_path, target, force=True)
    assert marker.read_text(encoding="utf-8") == "old"
