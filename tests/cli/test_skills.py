"""Tests for the `10xgraph skills` command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tenxgraph_api.cli.commands.skills import SkillsCommand
from tenxgraph_api.cli.constants import CLI_VERSION
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.core.prompts import Choice, PromptService


class _CapturingOutput(OutputFormatter):
    """Output formatter that records messages instead of printing."""

    def __init__(self) -> None:
        super().__init__()
        self.successes: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.tables: list[tuple[list[str], list[list[str]]]] = []
        self.lists: list[tuple[str | None, list[str]]] = []
        self.completions: list[dict[str, object]] = []

    def print_banner(self, *args, **kwargs) -> None:  # type: ignore[override]
        return

    def success(self, message: str, emoji: bool = True) -> None:  # type: ignore[override]
        self.successes.append(message)

    def error(self, message: str, emoji: bool = True) -> None:  # type: ignore[override]
        self.errors.append(message)

    def warning(self, message: str, emoji: bool = True) -> None:  # type: ignore[override]
        self.warnings.append(message)

    def info(self, message: str, emoji: bool = True) -> None:  # type: ignore[override]
        self.infos.append(message)

    def print_table(self, headers, rows, title=None) -> None:  # type: ignore[override]
        self.tables.append((headers, rows))

    def print_list(self, items, title=None, bullet="-") -> None:  # type: ignore[override]
        self.lists.append((title, list(items)))

    def completion_screen(  # type: ignore[override]
        self, title, message, *, details=None, next_steps=None
    ) -> None:
        self.completions.append({"title": title, "message": message, "details": details or {}})

    @property
    def last_completion(self) -> dict[str, object]:
        assert self.completions, "expected the command to render a completion screen"
        return self.completions[-1]


@pytest.fixture
def out() -> _CapturingOutput:
    return _CapturingOutput()


@pytest.fixture
def cmd(out: _CapturingOutput) -> SkillsCommand:
    return SkillsCommand(output=out)


# --- agent normalisation -------------------------------------------------


def test_list_flag_prints_all_three_agents(cmd: SkillsCommand, out: _CapturingOutput) -> None:
    exit_code = cmd.execute(list_agents=True)
    assert exit_code == 0
    assert out.tables, "expected --list to print a table"
    headers, rows = out.tables[0]
    assert "Agent" in headers
    names = {row[0] for row in rows}
    assert names == {"Codex", "Claude", "GitHub"}


def test_invalid_agent_name_is_rejected(cmd: SkillsCommand, out: _CapturingOutput) -> None:
    exit_code = cmd.execute(agent="not-a-real-agent", path=".")
    assert exit_code != 0
    assert any("Invalid agent" in e for e in out.errors)


def test_all_with_explicit_agent_is_rejected(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    exit_code = cmd.execute(agent="claude", all_agents=True, path=str(tmp_path))
    assert exit_code != 0
    assert any("--all cannot be combined with --agent" in e for e in out.errors)


# --- single-agent install -------------------------------------------------


def test_install_claude_creates_folder_and_manifest(cmd: SkillsCommand, tmp_path: Path) -> None:
    exit_code = cmd.execute(agent="claude", path=str(tmp_path))
    assert exit_code == 0

    skill_dir = tmp_path / ".claude" / "skills" / "10xgraph"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "references").is_dir()

    manifest = json.loads((skill_dir / ".10xgraph-skill.json").read_text(encoding="utf-8"))
    assert manifest["agent"] == "Claude"
    assert manifest["cli_version"] == CLI_VERSION
    assert "installed_at" in manifest


def test_install_codex_uses_agents_dotdir(cmd: SkillsCommand, tmp_path: Path) -> None:
    exit_code = cmd.execute(agent="codex", path=str(tmp_path))
    assert exit_code == 0
    assert (tmp_path / ".agents" / "skills" / "10xgraph" / "SKILL.md").is_file()
    # Earlier wrong paths must NOT be created
    assert not (tmp_path / ".agent").exists()
    assert not (tmp_path / ".codex").exists()


def test_install_github_writes_copilot_instructions_and_skill(
    cmd: SkillsCommand, tmp_path: Path
) -> None:
    exit_code = cmd.execute(agent="github", path=str(tmp_path))
    assert exit_code == 0

    instructions = tmp_path / ".github" / "instructions" / "10xgraph.instructions.md"
    assert instructions.is_file()
    content = instructions.read_text(encoding="utf-8")
    # Copilot frontmatter required for the file to be picked up
    assert content.startswith("---\napplyTo:")

    skill_dir = tmp_path / ".github" / "skills" / "10xgraph"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "references").is_dir()

    manifest = json.loads((skill_dir / ".10xgraph-skill.json").read_text(encoding="utf-8"))
    assert manifest["agent"] == "GitHub"
    assert manifest["cli_version"] == CLI_VERSION
    assert "installed_at" in manifest


def test_install_existing_dir_without_force_fails(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    out.errors.clear()
    exit_code = cmd.execute(agent="claude", path=str(tmp_path))
    assert exit_code != 0
    assert any("already installed" in e for e in out.errors)


def test_install_force_overwrites_existing(cmd: SkillsCommand, tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "10xgraph"
    cmd.execute(agent="claude", path=str(tmp_path))
    # mutate the install so we can detect overwrite
    sentinel = skill_dir / "SENTINEL.txt"
    sentinel.write_text("user-local content", encoding="utf-8")

    exit_code = cmd.execute(agent="claude", path=str(tmp_path), force=True)
    assert exit_code == 0
    assert not sentinel.exists(), "force install should remove old contents"
    assert (skill_dir / "SKILL.md").is_file()


def test_force_overwrites_copilot_file(cmd: SkillsCommand, tmp_path: Path) -> None:
    instructions = tmp_path / ".github" / "instructions" / "10xgraph.instructions.md"
    cmd.execute(agent="github", path=str(tmp_path))
    instructions.write_text("user-edited", encoding="utf-8")
    sentinel = tmp_path / ".github" / "skills" / "10xgraph" / "SENTINEL.txt"
    sentinel.write_text("user-local content", encoding="utf-8")

    exit_code = cmd.execute(agent="github", path=str(tmp_path), force=True)
    assert exit_code == 0
    assert instructions.read_text(encoding="utf-8").startswith("---\napplyTo:")
    assert not sentinel.exists(), "force install should remove old GitHub skill contents"


# --- --all flow -----------------------------------------------------------


def test_all_installs_every_agent(cmd: SkillsCommand, tmp_path: Path) -> None:
    exit_code = cmd.execute(all_agents=True, path=str(tmp_path))
    assert exit_code == 0

    assert (tmp_path / ".agents" / "skills" / "10xgraph" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "10xgraph" / "SKILL.md").is_file()
    assert (tmp_path / ".github" / "instructions" / "10xgraph.instructions.md").is_file()
    assert (tmp_path / ".github" / "skills" / "10xgraph" / "SKILL.md").is_file()


def test_all_skips_existing_without_force(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    cmd.execute(agent="claude", path=str(tmp_path))
    out.completions.clear()

    exit_code = cmd.execute(all_agents=True, path=str(tmp_path))
    assert exit_code == 0
    # Codex and GitHub were installed, Claude was skipped
    details = out.last_completion["details"]
    assert "Codex" in details["Installed"]
    assert "GitHub" in details["Installed"]
    assert "Claude" not in details["Installed"]
    assert details["Skipped"] == "Claude"
    assert any("Skipped existing" in w and "Claude" in w for w in out.warnings)


def test_all_with_force_reinstalls_everything(cmd: SkillsCommand, tmp_path: Path) -> None:
    cmd.execute(all_agents=True, path=str(tmp_path))
    sentinel = tmp_path / ".claude" / "skills" / "10xgraph" / "SENTINEL.txt"
    sentinel.write_text("x", encoding="utf-8")

    exit_code = cmd.execute(all_agents=True, path=str(tmp_path), force=True)
    assert exit_code == 0
    assert not sentinel.exists()


# --- path safety ----------------------------------------------------------


def test_install_at_filesystem_root_is_refused(cmd: SkillsCommand, out: _CapturingOutput) -> None:
    root = Path(Path.cwd().anchor) if Path.cwd().anchor else Path("/")
    exit_code = cmd.execute(agent="claude", path=str(root))
    assert exit_code != 0
    assert any("filesystem root" in e for e in out.errors)


def test_install_at_home_dir_is_refused(cmd: SkillsCommand, out: _CapturingOutput) -> None:
    exit_code = cmd.execute(agent="claude", path=str(Path.home()))
    assert exit_code != 0
    assert any("home directory" in e for e in out.errors)


# --- non-interactive guard ------------------------------------------------


def test_no_agent_with_non_tty_stdin_errors(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    with patch.object(sys.stdin, "isatty", return_value=False):
        exit_code = cmd.execute(path=str(tmp_path))
    assert exit_code != 0
    assert any("stdin is not interactive" in e for e in out.errors)


# --- interactive multi-select --------------------------------------------


def _answer_checkbox(monkeypatch, selection: list[str] | None) -> list[list[Choice]]:
    """Capture the offered choices and reply with a fixed selection."""
    offered: list[list[Choice]] = []

    def fake_checkbox(self, message, choices, **kwargs):
        offered.append(list(choices))
        return selection

    monkeypatch.setattr(PromptService, "checkbox", fake_checkbox)
    monkeypatch.setattr(PromptService, "require_interactive", lambda self, **kwargs: None)
    return offered


def test_multi_select_installs_every_chosen_agent(
    cmd: SkillsCommand, monkeypatch, tmp_path: Path
) -> None:
    _answer_checkbox(monkeypatch, ["Codex", "GitHub"])

    assert cmd.execute(path=str(tmp_path)) == 0
    assert (tmp_path / ".agents" / "skills" / "10xgraph" / "SKILL.md").is_file()
    assert (tmp_path / ".github" / "skills" / "10xgraph" / "SKILL.md").is_file()
    assert not (tmp_path / ".claude").exists()


def test_multi_select_marks_already_installed_agents(
    cmd: SkillsCommand, monkeypatch, tmp_path: Path
) -> None:
    cmd.execute(agent="claude", path=str(tmp_path))
    offered = _answer_checkbox(monkeypatch, ["Codex"])

    cmd.execute(path=str(tmp_path))

    by_name = {choice.value: choice for choice in offered[0]}
    # An existing install is pre-checked and labelled, so confirming as-is
    # reinstalls exactly what is already there rather than silently dropping it.
    assert by_name["Claude"].checked is True
    assert "(installed)" in by_name["Claude"].description
    assert by_name["Codex"].checked is False
    assert "(installed)" not in by_name["Codex"].description


def test_cancelling_the_multi_select_writes_nothing(
    cmd: SkillsCommand, monkeypatch, tmp_path: Path
) -> None:
    _answer_checkbox(monkeypatch, None)

    assert cmd.execute(path=str(tmp_path)) == 0
    assert list(tmp_path.iterdir()) == []


def test_declining_the_overwrite_prompt_skips_that_agent(
    cmd: SkillsCommand, out: _CapturingOutput, monkeypatch, tmp_path: Path
) -> None:
    cmd.execute(agent="claude", path=str(tmp_path))
    sentinel = tmp_path / ".claude" / "skills" / "10xgraph" / "SENTINEL.txt"
    sentinel.write_text("mine", encoding="utf-8")

    _answer_checkbox(monkeypatch, ["Claude"])
    monkeypatch.setattr(PromptService, "confirm", lambda self, message, **kwargs: False)
    monkeypatch.setattr(PromptService, "interactive", property(lambda self: True))
    out.completions.clear()

    assert cmd.execute(path=str(tmp_path)) == 0
    assert sentinel.exists(), "declining the overwrite must leave existing files alone"
    assert out.last_completion["details"]["Skipped"] == "Claude"


def test_accepting_the_overwrite_prompt_reinstalls(
    cmd: SkillsCommand, monkeypatch, tmp_path: Path
) -> None:
    cmd.execute(agent="claude", path=str(tmp_path))
    sentinel = tmp_path / ".claude" / "skills" / "10xgraph" / "SENTINEL.txt"
    sentinel.write_text("mine", encoding="utf-8")

    _answer_checkbox(monkeypatch, ["Claude"])
    monkeypatch.setattr(PromptService, "confirm", lambda self, message, **kwargs: True)
    monkeypatch.setattr(PromptService, "interactive", property(lambda self: True))

    assert cmd.execute(path=str(tmp_path)) == 0
    assert not sentinel.exists()


# --- Agent Skills spec conformance ------------------------------------------

_TEMPLATE_SKILL = (
    Path(__file__).resolve().parents[2]
    / "tenxgraph_api"
    / "cli"
    / "templates"
    / "skills"
    / "10xgraph"
)


def test_bundled_skill_conforms_to_spec() -> None:
    skills = pytest.importorskip("tenxgraph.core.skills")
    if not hasattr(skills, "validate_skill"):
        pytest.skip("installed 10xgraph predates validate_skill")
    # No errors and no warnings: this also checks every references/... path in
    # the SKILL.md body exists in the bundle.
    assert skills.validate_skill(_TEMPLATE_SKILL) == []


def test_every_agent_gets_the_same_relative_path_skill(cmd: SkillsCommand, tmp_path: Path) -> None:
    assert cmd.execute(all_agents=True, path=str(tmp_path)) == 0
    installed = [
        tmp_path / ".agents" / "skills" / "10xgraph" / "SKILL.md",
        tmp_path / ".claude" / "skills" / "10xgraph" / "SKILL.md",
        tmp_path / ".github" / "skills" / "10xgraph" / "SKILL.md",
    ]
    contents = {path.read_text(encoding="utf-8") for path in installed}
    assert len(contents) == 1
    content = contents.pop()
    # Paths inside a skill are relative to the skill directory per the spec, so
    # the same SKILL.md works wherever it is installed.
    assert "`references/architecture.md`" in content
    for prefix in (".claude/skills", ".agents/skills", ".github/skills"):
        assert prefix not in content


# --- --validate -----------------------------------------------------------


def _write_skill(root: Path, name: str, frontmatter: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nBody\n", encoding="utf-8")
    return skill_dir


@pytest.fixture
def _needs_validator() -> None:
    skills = pytest.importorskip("tenxgraph.core.skills")
    if not hasattr(skills, "validate_skill"):
        pytest.skip("installed 10xgraph predates validate_skill")


@pytest.mark.usefixtures("_needs_validator")
def test_validate_folder_of_valid_skills(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    _write_skill(tmp_path, "alpha", "name: alpha\ndescription: Does alpha things.")
    _write_skill(tmp_path, "beta", "name: beta\ndescription: Does beta things.")

    assert cmd.execute(validate_paths=[str(tmp_path)]) == 0
    headers, rows = out.tables[-1]
    assert headers == ["Skill", "Status", "Errors", "Warnings"]
    assert [row[1] for row in rows] == ["valid", "valid"]
    assert any("2 skill(s) conform" in s for s in out.successes)


@pytest.mark.usefixtures("_needs_validator")
def test_validate_reports_spec_errors(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    skill_dir = _write_skill(tmp_path, "Bad_Name", "name: Bad_Name\ndescription: d\nfoo: 1")

    assert cmd.execute(validate_paths=[str(skill_dir)]) == 1
    joined = " | ".join(out.errors)
    assert "must be lowercase" in joined
    assert "Unexpected frontmatter fields: foo" in joined
    assert out.tables[-1][1][0][1] == "invalid"


@pytest.mark.usefixtures("_needs_validator")
def test_validate_missing_path(cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path) -> None:
    assert cmd.execute(validate_paths=[str(tmp_path / "missing")]) == 1
    assert any("No skill found" in e for e in out.errors)


@pytest.mark.usefixtures("_needs_validator")
def test_validate_bundled_template(cmd: SkillsCommand, out: _CapturingOutput) -> None:
    assert cmd.execute(validate_paths=[str(_TEMPLATE_SKILL)]) == 0
    assert out.warnings == []


# --- symlinks in the target project (B35) ---------------------------------


def test_symlinked_directory_in_target_is_refused(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".claude").symlink_to(outside, target_is_directory=True)

    exit_code = cmd.execute(agent="claude", path=str(project), force=True)

    assert exit_code != 0
    assert any("symlink" in e for e in out.errors)
    assert list(outside.iterdir()) == []


def test_symlinked_skill_folder_is_not_deleted_through(cmd: SkillsCommand, tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / ".claude" / "skills").mkdir(parents=True)
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (project / ".claude" / "skills" / "10xgraph").symlink_to(outside, target_is_directory=True)

    assert cmd.execute(agent="claude", path=str(project), force=True) != 0
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert [p.name for p in outside.iterdir()] == ["keep.txt"]


def test_symlinked_file_is_refused_even_when_dangling(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    (project / ".github" / "instructions").mkdir(parents=True)
    target = tmp_path / "elsewhere.md"
    (project / ".github" / "instructions" / "10xgraph.instructions.md").symlink_to(target)

    exit_code = cmd.execute(agent="github", path=str(project), force=True)

    assert exit_code != 0
    assert any("symlink" in e for e in out.errors)
    assert not target.exists()
    assert not (project / ".github" / "skills").exists()


def test_symlinked_project_path_itself_still_installs(cmd: SkillsCommand, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert cmd.execute(agent="claude", path=str(link)) == 0
    assert (real / ".claude" / "skills" / "10xgraph" / "SKILL.md").is_file()


# --- always-on project instructions (CLAUDE.md / AGENTS.md block) ------------

_START = "<!-- 10xgraph:start -->"
_END = "<!-- 10xgraph:end -->"


def _block_of(text: str) -> str:
    return text[text.index(_START) : text.index(_END) + len(_END)]


def test_claude_install_creates_claude_md_with_only_the_block(
    cmd: SkillsCommand, tmp_path: Path
) -> None:
    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith(_START) and text.endswith(_END + "\n")
    assert ".claude/skills/10xgraph" in text
    assert "{skill_path}" not in text
    assert not (tmp_path / "AGENTS.md").exists()


def test_codex_install_writes_agents_md_with_its_skill_path(
    cmd: SkillsCommand, tmp_path: Path
) -> None:
    assert cmd.execute(agent="codex", path=str(tmp_path)) == 0
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert ".agents/skills/10xgraph" in text and ".claude/" not in text
    assert not (tmp_path / "CLAUDE.md").exists()


@pytest.mark.parametrize(
    ("before", "sep"),
    [("# Mine\n\nKeep me.\n", "\n"), ("# Mine\nno newline", "\n\n"), ("# Mine\n\n", "")],
)
def test_block_is_appended_and_user_text_kept_byte_for_byte(
    cmd: SkillsCommand, tmp_path: Path, before: str, sep: str
) -> None:
    (tmp_path / "CLAUDE.md").write_bytes(before.encode())
    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    after = (tmp_path / "CLAUDE.md").read_bytes().decode()
    assert after.startswith(before + sep + _START)
    assert after.endswith(_END + "\n")


def test_existing_block_is_replaced_and_surroundings_kept(
    cmd: SkillsCommand, tmp_path: Path
) -> None:
    head = "# Top\r\nwindows line\r\n\n"
    tail = "\n\n## After\ntrailing, no newline"
    stale = f"{_START}\nold 10xGraph text\n{_END}"
    (tmp_path / "CLAUDE.md").write_bytes((head + stale + tail).encode())

    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    after = (tmp_path / "CLAUDE.md").read_bytes().decode()
    assert after.startswith(head) and after.endswith(tail)
    assert "old 10xGraph text" not in after
    assert "load the `10xgraph` skill" in _block_of(after)
    assert after.count(_START) == 1


def test_rerun_with_force_is_idempotent(cmd: SkillsCommand, tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    first = (tmp_path / "CLAUDE.md").read_bytes()
    assert cmd.execute(agent="claude", path=str(tmp_path), force=True) == 0
    assert cmd.execute(agent="claude", path=str(tmp_path), force=True) == 0
    assert (tmp_path / "CLAUDE.md").read_bytes() == first


def test_existing_claude_md_alone_does_not_count_as_installed(
    cmd: SkillsCommand, tmp_path: Path
) -> None:
    # The user's own CLAUDE.md is not our install: no --force needed, no skip.
    (tmp_path / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Mine too\n", encoding="utf-8")
    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    assert cmd.execute(agent="codex", path=str(tmp_path)) == 0
    assert _START in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert _START in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_block_keeps_file_mode(cmd: SkillsCommand, tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Mine\n", encoding="utf-8")
    claude_md.chmod(0o640)
    assert cmd.execute(agent="claude", path=str(tmp_path)) == 0
    assert claude_md.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir() if "installing" in p.name] == []


@pytest.mark.parametrize(("agent", "name"), [("claude", "CLAUDE.md"), ("codex", "AGENTS.md")])
def test_symlinked_instructions_file_is_refused(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path, agent: str, name: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (project / name).symlink_to(outside)

    assert cmd.execute(agent=agent, path=str(project)) != 0
    assert any("symlink" in e for e in out.errors)
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not (project / (".claude" if agent == "claude" else ".agents")).exists()


def test_start_marker_without_end_is_refused(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    original = f"# Mine\n{_START}\nhalf a block\n"
    (tmp_path / "CLAUDE.md").write_text(original, encoding="utf-8")
    assert cmd.execute(agent="claude", path=str(tmp_path)) != 0
    assert any("10xgraph:end" in e for e in out.errors)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == original
    assert not (tmp_path / ".claude").exists()


def test_all_writes_both_blocks_and_reports_them(
    cmd: SkillsCommand, out: _CapturingOutput, tmp_path: Path
) -> None:
    assert cmd.execute(all_agents=True, path=str(tmp_path)) == 0
    assert ".claude/skills/10xgraph" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".agents/skills/10xgraph" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    details = out.last_completion["details"]
    assert details["Project instructions"] == "AGENTS.md, CLAUDE.md"


def test_list_shows_block_files(cmd: SkillsCommand, out: _CapturingOutput) -> None:
    assert cmd.execute(list_agents=True) == 0
    _headers, rows = out.tables[0]
    by_name = {row[0]: row for row in rows}
    assert by_name["Claude"][1] == "folder+block" and "CLAUDE.md" in by_name["Claude"][2]
    assert by_name["Codex"][1] == "folder+block" and "AGENTS.md" in by_name["Codex"][2]
    assert by_name["GitHub"][1] == "file+folder"
