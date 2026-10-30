"""Test command implementation — thin pytest wrapper."""

import subprocess  # nosec: B404
import sys
import webbrowser
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.core.config import ConfigManager


class TestCommand(BaseCommand):
    """Run the project's test suite via pytest."""

    def execute(
        self,
        path: str | None = None,
        coverage: bool = False,
        html: bool = False,
        keyword: str | None = None,
        verbose: bool = False,
        quiet: bool = False,
        extra_args: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> int:
        project_root = Path.cwd()
        self.output.command_header("test", f"Running the project test suite in {project_root}")

        timeline = self.output.timeline(
            "Preparing the test run",
            steps=(
                ("config", "Resolving test configuration"),
                ("command", "Building the pytest command"),
            ),
        )
        with timeline:
            with timeline.step("config") as step:
                # Load optional overrides from 10xgraph.json
                cfg: dict[str, Any] = {}
                config_manager = ConfigManager()
                discovered = config_manager.auto_discover_config()
                if discovered:
                    try:
                        config_manager.load_config(str(discovered))
                        cfg = config_manager.get_test_config()
                        step.detail(str(discovered))
                    except Exception:
                        self.logger.warning("Failed to load test configuration from %s", discovered)
                        step.detail(f"ignored unreadable config at {discovered}")
                else:
                    step.detail("no 10xgraph.json found — using pytest defaults")

                # Explicit CLI path wins; 10xgraph.json next; None = pytest auto-discovery
                resolved_path: str | None = path or cfg.get("path") or None
                resolved_coverage = coverage or cfg.get("coverage", False)
                coverage_threshold: int | None = cfg.get("coverage_threshold")
                location = str(project_root / resolved_path) if resolved_path else str(project_root)

            with timeline.step("command") as step:
                cmd = [sys.executable, "-m", "pytest"]
                if resolved_path:
                    cmd.append(resolved_path)

                if quiet and not verbose:
                    cmd.append("-q")
                else:
                    cmd.append("-v")

                if resolved_coverage:
                    cmd += [
                        "--cov=.",
                        "--cov-report=term-missing",
                        "--cov-report=html:htmlcov",
                    ]
                    if coverage_threshold is not None:
                        cmd.append(f"--cov-fail-under={coverage_threshold}")

                if keyword:
                    cmd += ["-k", keyword]

                cmd += list(extra_args)
                step.detail(" ".join(["pytest", *cmd[3:]]))

        self.logger.info("Running: %s", " ".join(cmd))

        # No spinner around this: pytest owns the terminal while it streams its
        # own progress, and a live region would fight it for the cursor.
        result = subprocess.run(cmd, cwd=project_root, check=False)  # nosec: B603  # noqa: S603

        if result.returncode == 0:
            self.output.completion_screen(
                "Tests passed",
                "The project test suite completed successfully",
                details={
                    "Location": location,
                    "Coverage": "enabled" if resolved_coverage else "disabled",
                },
                next_steps=(
                    ["Open htmlcov/index.html to inspect the coverage report."]
                    if resolved_coverage
                    else []
                ),
            )
        else:
            self.output.error(f"Tests finished with exit code {result.returncode}.")

        if html and resolved_coverage:
            report_path = (project_root / "htmlcov" / "index.html").as_uri()
            self.output.info(f"Opening coverage report: {report_path}", emoji=False)
            webbrowser.open(report_path)

        return result.returncode
