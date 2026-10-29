"""Skills command implementation."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.constants import CLI_VERSION
from tenxgraph_api.cli.core.prompts import Choice
from tenxgraph_api.cli.exceptions import FileOperationError, ValidationError


_MANIFEST_FILENAME = ".10xgraph-skill.json"


@dataclass(frozen=True)
class _InstallArtifact:
    """Describes one file or folder installed for an agent."""

    kind: Literal["folder", "file"]
    install_relpath: str
    source_relpath: str
    manifest: bool = False


@dataclass(frozen=True)
class _AgentTarget:
    """Describes how the bundled skill is materialised for one agent."""

    name: str
    artifacts: tuple[_InstallArtifact, ...]

    @property
    def kind(self) -> str:
        kinds = {artifact.kind for artifact in self.artifacts}
        if len(kinds) == 1:
            return next(iter(kinds))
        return "file+folder"


# Every agent gets the same spec-conformant skill folder (SKILL.md + references/);
# the SKILL.md uses paths relative to the skill directory, so one copy works in
# any install location.
_SKILL_SOURCE = "10xgraph"

_TARGETS: tuple[_AgentTarget, ...] = (
    _AgentTarget(
        name="Codex",
        artifacts=(
            _InstallArtifact(
                kind="folder",
                install_relpath=".agents/skills/10xgraph",
                source_relpath=_SKILL_SOURCE,
                manifest=True,
            ),
        ),
    ),
    _AgentTarget(
        name="Claude",
        artifacts=(
            _InstallArtifact(
                kind="folder",
                install_relpath=".claude/skills/10xgraph",
                source_relpath=_SKILL_SOURCE,
                manifest=True,
            ),
        ),
    ),
    _AgentTarget(
        name="GitHub",
        artifacts=(
            _InstallArtifact(
                kind="file",
                install_relpath=".github/instructions/10xgraph.instructions.md",
                source_relpath="copilot/10xgraph.instructions.md",
            ),
            _InstallArtifact(
                kind="folder",
                install_relpath=".github/skills/10xgraph",
                source_relpath=_SKILL_SOURCE,
                manifest=True,
            ),
        ),
    ),
)

_AGENT_LOOKUP: dict[str, _AgentTarget] = {
    **{t.name.lower(): t for t in _TARGETS},
    "1": _TARGETS[0],
    "2": _TARGETS[1],
    "3": _TARGETS[2],
}


class SkillsCommand(BaseCommand):
    """Install bundled 10xGraph skills, or validate skills against the spec."""

    def execute(
        self,
        agent: str | None = None,
        path: str = ".",
        force: bool = False,
        all_agents: bool = False,
        list_agents: bool = False,
        validate_paths: list[str] | None = None,
        **kwargs: Any,
    ) -> int:
        """Execute the skills command.

        Args:
            agent: Target agent name or menu number.
            path: Project directory where the agent skill should be installed.
            force: Overwrite an existing installation.
            all_agents: Install for every supported agent.
            list_agents: Print supported agents and exit.
            validate_paths: Skill directories (or folders of skills) to validate
                against the Agent Skills specification instead of installing.
            **kwargs: Additional arguments.

        Returns:
            Exit code.
        """
        if validate_paths:
            return self._run_validate(validate_paths)
        return self._run_install(
            agent, path, force=force, all_agents=all_agents, list_agents=list_agents
        )

    def _run_validate(self, validate_paths: list[str]) -> int:
        try:
            self.output.command_header(
                "skills",
                "Validate skills against the Agent Skills specification (agentskills.io).",
                color="magenta",
            )
            return self._validate(validate_paths)
        except ValidationError as e:
            return self.handle_error(e)

    def _run_install(
        self,
        agent: str | None,
        path: str,
        *,
        force: bool,
        all_agents: bool,
        list_agents: bool,
    ) -> int:
        try:
            self.output.command_header(
                "skills",
                "Install bundled 10xGraph skills for Codex, Claude, or GitHub Copilot.",
                color="magenta",
            )

            if list_agents:
                self._print_agents()
                return 0

            if all_agents and agent:
                raise ValidationError("--all cannot be combined with --agent.", field="agent")

            templates_root = self._templates_root()
            project_root = self._safe_project_root(path)

            if all_agents:
                targets: tuple[_AgentTarget, ...] = _TARGETS
            elif agent:
                targets = (self._normalize_agent(agent),)
            else:
                selected = self._choose_agents(project_root)
                if selected is None:
                    self.output.info("Cancelled.", emoji=False)
                    return 0
                targets = selected

            force = force or self._confirm_overwrite(project_root, targets, force=force)
            return self._install_targets(
                templates_root,
                project_root,
                targets,
                force=force,
                # Naming one agent is a request for that agent specifically, so a
                # collision is an error. A set the user did not enumerate — --all,
                # or a multi-select — skips what is already there instead.
                strict=bool(agent),
            )

        except (FileOperationError, ValidationError) as e:
            return self.handle_error(e)
        except OSError as e:
            file_error = FileOperationError(f"Failed to install 10xGraph skills: {e}")
            file_error.__cause__ = e
            return self.handle_error(file_error)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, paths: list[str]) -> int:
        """Validate each skill found under *paths*. Returns 1 if any is invalid."""
        try:
            # Not in any released core yet (<= 0.9.2); the ImportError branch covers that.
            from tenxgraph.core.skills import validate_skill  # type: ignore[attr-defined]
            from tenxgraph.core.skills.loader import (  # type: ignore[attr-defined]
                iter_skill_dirs,
            )
        except ImportError as exc:
            raise ValidationError(
                "Skill validation needs a 10xgraph release that ships "
                "tenxgraph.core.skills.validate_skill. Upgrade 10xgraph.",
                field="validate",
            ) from exc

        rows: list[list[str]] = []
        invalid = 0
        checked = 0
        for raw_path in paths:
            skill_dirs = iter_skill_dirs(raw_path)
            if not skill_dirs:
                self.output.error(
                    f"No skill found at {raw_path}: expected a SKILL.md in it or in its "
                    "subdirectories."
                )
                invalid += 1
                continue

            for skill_dir in skill_dirs:
                checked += 1
                issues = validate_skill(skill_dir)
                errors = [issue for issue in issues if issue.level == "error"]
                warnings = [issue for issue in issues if issue.level == "warning"]
                for issue in errors:
                    self.output.error(f"{skill_dir.name}: {issue.message}")
                for issue in warnings:
                    self.output.warning(f"{skill_dir.name}: {issue.message}")
                if errors:
                    invalid += 1
                status = "invalid" if errors else "valid with warnings" if warnings else "valid"
                rows.append([str(skill_dir), status, str(len(errors)), str(len(warnings))])

        if rows:
            self.output.print_table(
                ["Skill", "Status", "Errors", "Warnings"], rows, title="Agent Skills validation"
            )
        if invalid:
            self.output.error(f"{invalid} skill(s) failed validation.")
            return 1
        self.output.success(f"{checked} skill(s) conform to the Agent Skills specification.")
        return 0

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _choose_agents(self, project_root: Path) -> tuple[_AgentTarget, ...] | None:
        """Offer a space-to-toggle list of agents. Returns None if cancelled."""
        prompts = self.output.prompts()
        prompts.require_interactive(
            field="agent",
            alternatives="Pass --agent codex|claude|github, or --all.",
        )

        choices = [
            Choice(
                value=target.name,
                title=target.name,
                description=self._choice_description(project_root, target),
                # Pre-check what is already installed, so confirming without
                # touching anything reinstalls exactly what is already there.
                checked=bool(self._existing_paths(project_root, target)),
            )
            for target in _TARGETS
        ]
        names = prompts.checkbox(
            "Which agents should get the 10xGraph skill?",
            choices,
            validate=lambda selected: bool(selected) or "Select at least one agent.",
        )
        if names is None:
            return None
        return tuple(_AGENT_LOOKUP[name.lower()] for name in names)

    def _choice_description(self, project_root: Path, target: _AgentTarget) -> str:
        install_root = target.artifacts[0].install_relpath
        if self._existing_paths(project_root, target):
            return f"{install_root}  (installed)"
        return install_root

    @staticmethod
    def _existing_paths(project_root: Path, target: _AgentTarget) -> list[Path]:
        return [
            project_root / artifact.install_relpath
            for artifact in target.artifacts
            if (project_root / artifact.install_relpath).exists()
        ]

    def _confirm_overwrite(
        self,
        project_root: Path,
        targets: tuple[_AgentTarget, ...],
        *,
        force: bool,
    ) -> bool:
        """Offer to overwrite in place of failing, when a human is present."""
        if force:
            return False
        occupied = [t.name for t in targets if self._existing_paths(project_root, t)]
        if not occupied:
            return False

        prompts = self.output.prompts()
        if not prompts.interactive:
            # Non-interactive callers keep the previous contract: refuse and
            # point at --force rather than silently replacing files.
            return False
        return bool(
            prompts.confirm(
                f"Overwrite the existing install for {', '.join(occupied)}?",
                default=True,
            )
        )

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def _install_targets(
        self,
        templates_root: Path,
        project_root: Path,
        targets: tuple[_AgentTarget, ...],
        *,
        force: bool,
        strict: bool,
    ) -> int:
        installed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        timeline = self.output.timeline(
            "Installing 10xGraph skills",
            steps=tuple((target.name.lower(), f"{target.name} skill") for target in targets),
        )
        with timeline:
            for target in targets:
                with timeline.step(target.name.lower()) as step:
                    existing = self._existing_paths(project_root, target)
                    if existing and not force:
                        if strict:
                            paths = ", ".join(str(dest) for dest in existing)
                            raise FileOperationError(
                                f"Skill already installed at {paths}. Use --force to overwrite.",
                                file_path=str(existing[0]),
                            )
                        step.skip("already installed — pass --force to overwrite")
                        skipped.append(target.name)
                        continue
                    try:
                        written = self._install_one(
                            templates_root, project_root, target, force=force
                        )
                    except (FileOperationError, OSError, UnicodeError) as exc:
                        # Record and continue: one unwritable target should not
                        # cancel the agents the user also asked for.
                        self.logger.error("Install failed for %s: %s", target.name, exc)
                        failed.append(f"{target.name}: {exc}")
                        step.fail(str(exc))
                        continue
                    installed.append(target.name)
                    step.detail(written[0])

        for target in targets:
            if target.name in installed:
                self._print_activation_hint(target)

        if skipped:
            self.output.warning(
                "Skipped existing installs (use --force to overwrite): " + ", ".join(skipped)
            )
        if failed:
            self.output.error("Failed installs: " + "; ".join(failed))
            if not installed:
                return 1

        self.output.completion_screen(
            "Skills installed" if installed else "Nothing to install",
            f"{len(installed)} agent(s) ready" if installed else "Every selection was skipped",
            details={
                "Installed": ", ".join(installed) or "none",
                "Skipped": ", ".join(skipped) or "none",
                "Project": project_root,
            },
            next_steps=["Restart your coding agent so it loads the new skill directory."]
            if installed
            else [],
        )
        return 0

    def _install_one(
        self,
        templates_root: Path,
        project_root: Path,
        target: _AgentTarget,
        *,
        force: bool,
    ) -> list[str]:
        installs = [
            (
                artifact,
                templates_root / artifact.source_relpath,
                project_root / artifact.install_relpath,
            )
            for artifact in target.artifacts
        ]
        for _artifact, _source, dest in installs:
            self._ensure_inside_project(project_root, dest)
        for _artifact, source, _dest in installs:
            if not source.exists():
                raise FileOperationError(
                    f"Bundled skills template not found: {source}", file_path=str(source)
                )

        existing = [dest for _artifact, _source, dest in installs if dest.exists()]
        if existing and not force:
            paths = ", ".join(str(dest) for dest in existing)
            raise FileOperationError(
                f"Skill already installed at {paths}. Use --force to overwrite.",
                file_path=str(existing[0]),
            )

        # Build every artifact next to its destination first, then swap it in. Removing the
        # old install before the copy would leave no skill folder if the copy failed.
        staged: list[tuple[Path, Path]] = []
        try:
            for artifact, source, dest in installs:
                dest.parent.mkdir(parents=True, exist_ok=True)
                temp = dest.parent / f".{dest.name}.installing-{os.getpid()}"
                self._remove(temp)
                staged.append((temp, dest))
                if artifact.kind == "folder":
                    shutil.copytree(
                        source,
                        temp,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                    )
                    if artifact.manifest:
                        self._write_manifest(temp, target.name)
                else:
                    shutil.copyfile(source, temp)
        except BaseException:
            for temp, _dest in staged:
                self._remove(temp)
            raise

        installed_paths: list[str] = []
        for temp, dest in staged:
            self._remove(dest)
            temp.rename(dest)
            installed_paths.append(str(dest))

        return installed_paths

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    @staticmethod
    def _ensure_inside_project(project_root: Path, dest: Path) -> None:
        """Refuse a destination that is, or sits under, a symlink, or that leaves the project.

        A repo can ship ``.claude`` as a symlink to another directory; installing through it
        would delete and write files outside ``--path``. Every existing component between the
        project root and ``dest`` is checked, then the resolved path is checked as well.
        """
        try:
            parts = dest.relative_to(project_root).parts
        except ValueError:
            parts = None
        if parts is not None:
            current = project_root
            for part in parts:
                current = current / part
                if current.is_symlink():
                    raise FileOperationError(
                        f"Refusing to install through a symlink: {current}. "
                        "Replace it with a real directory or file, or pass a different --path.",
                        file_path=str(current),
                    )
        if parts is None or not dest.resolve().is_relative_to(project_root.resolve()):
            raise FileOperationError(
                f"Refusing to install outside the project directory: {dest}",
                file_path=str(dest),
            )

    def _print_activation_hint(self, target: _AgentTarget) -> None:
        """Tell the user how to make the freshly installed skill take effect.

        Coding agents load skills at session start. When ``10xgraph skills``
        creates the skills directory for the first time during a running session,
        the agent does not watch the new top-level directory until it is
        restarted, so the skill appears "installed but unused". This note makes
        the required restart explicit.
        """
        if target.name == "Claude":
            note = (
                "Restart Claude Code (or run /exit then `claude`) so it loads the new "
                ".claude/skills/ directory. Claude auto-invokes the skill from its "
                "description; type /10xgraph to run it manually."
            )
        elif target.name == "Codex":
            note = (
                "Restart Codex so it picks up the new .agents/skills/ directory. Codex "
                "auto-selects the skill when your task matches its description."
            )
        else:  # GitHub Copilot
            note = (
                "Restart GitHub Copilot / your editor so it loads the new "
                ".github/skills/ directory and .github/instructions/ file."
            )
        self.output.warning(f"Activate: {note}")

    def _write_manifest(self, target_dir: Path, agent_name: str) -> None:
        manifest = {
            "agent": agent_name,
            "cli_version": CLI_VERSION,
            "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (target_dir / _MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    def _print_agents(self) -> None:
        rows = [
            [t.name, t.kind, ", ".join(a.install_relpath for a in t.artifacts)] for t in _TARGETS
        ]
        self.output.print_table(
            ["Agent", "Kind", "Install path (relative to --path)"],
            rows,
            title="Supported agents",
        )

    def _safe_project_root(self, path: str) -> Path:
        project_root = Path(path).resolve()
        if project_root.parent == project_root:
            raise ValidationError(
                f"Refusing to install skills at filesystem root: {project_root}",
                field="path",
            )
        if project_root == Path.home().resolve():
            raise ValidationError(
                f"Refusing to install skills directly into the home directory: {project_root}. "
                "Pass --path pointing at a project directory.",
                field="path",
            )
        return project_root

    def _normalize_agent(self, value: str) -> _AgentTarget:
        key = value.strip().lower()
        if key in _AGENT_LOOKUP:
            return _AGENT_LOOKUP[key]

        valid = "Codex, Claude, GitHub, or 1, 2, 3"
        raise ValidationError(f"Invalid agent '{value}'. Choose {valid}.", field="agent")

    def _templates_root(self) -> Path:
        cli_dir = Path(__file__).resolve().parents[1]
        return cli_dir / "templates" / "skills"
