"""Run TaskIQ's worker and scheduler on this project: ``10xgraph worker`` / ``scheduler``.

Both point TaskIQ's own commands at the project's broker and ``worker.tasks``, and pass every
other argument through to TaskIQ unchanged (``--workers``, ``--ack-type``, ...).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.constants import DEFAULT_CONFIG_FILE
from tenxgraph_api.cli.core.config import resolve_default_config
from tenxgraph_api.cli.exceptions import ConfigurationError, ValidationError


MIN_FLAG_LEN = 3  # "--" plus at least one letter
GLOB_MARKS = ("*", "?", "[", "/")
PROCESS_POOL_FLAGS = ("--use-process-pool", "--max-process-pool-processes")


def _is_glob(entry: str) -> bool:
    return entry.endswith(".py") or any(mark in entry for mark in GLOB_MARKS)


def _is_flag(arg: str, flag: str) -> bool:
    """True for ``flag``, ``flag=value`` and argparse's unambiguous prefixes (``--use-proc``)."""
    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= MIN_FLAG_LEN and flag.startswith(name)


class _TaskiqCommand(BaseCommand):
    """Shared steps: find the project, build TaskIQ's argv, run TaskIQ's command in-process."""

    target = ""

    def run_taskiq(self, argv: list[str]) -> int | None:
        raise NotImplementedError

    def check_extra(self, extra: list[str]) -> None:
        """Refuse arguments this command cannot support."""

    def default_extra(self, extra: list[str]) -> list[str]:
        return extra

    def build_argv(self, tasks: list[str], extra: list[str]) -> list[str]:
        """``<target>`` + module names + one ``--tasks-pattern`` per glob + the passed args."""
        modules = [t for t in tasks if not _is_glob(t)]
        patterns = [t for t in tasks if _is_glob(t)]
        argv = [self.target, *modules]
        if patterns:
            argv.append("--fs-discover")
            for pattern in patterns:
                argv += ["--tasks-pattern", pattern]
        return argv + self.default_extra(extra)

    def execute(
        self, config: str = DEFAULT_CONFIG_FILE, extra: list[str] | None = None, **kwargs: Any
    ) -> int:
        try:
            extra = list(extra or [])
            self.check_extra(extra)
            path = resolve_default_config(config).resolve()
            if not path.exists():
                raise ConfigurationError(f"Config file not found: {path}", config_path=str(path))
            project = path.parent
            os.chdir(project)
            from tenxgraph_api.src.app.core.config.graph_config import GraphConfig

            worker = GraphConfig(str(path)).worker
            if worker is None:
                raise ConfigurationError(
                    f'{path.name} has no "worker" key. Add one, e.g. '
                    '"worker": {"broker": {"type": "redis_stream"}, "tasks": ["myapp.tasks"]}.',
                    config_path=str(path),
                )
            os.environ["GRAPH_PATH"] = str(path)
            if str(project) not in sys.path[:1]:
                sys.path.insert(0, str(project))
            status = self.run_taskiq(self.build_argv(worker.tasks, extra))
            return status if isinstance(status, int) else 0
        except Exception as e:
            return self.handle_error(e)


class WorkerCommand(_TaskiqCommand):
    """``10xgraph worker``: TaskIQ's worker on the project's broker."""

    target = "tenxgraph_api.jobs.worker:broker"

    def check_extra(self, extra: list[str]) -> None:
        for arg in extra:
            if any(_is_flag(arg, flag) for flag in PROCESS_POOL_FLAGS):
                raise ValidationError(
                    "The process pool is not supported: the cancel and argument guard does not "
                    "reach process-pool workers. Use the default thread pool.",
                    field=arg,
                )

    def run_taskiq(self, argv: list[str]) -> int | None:
        from taskiq.cli.worker.cmd import WorkerCMD

        return WorkerCMD().exec(argv)


class SchedulerCommand(_TaskiqCommand):
    """``10xgraph scheduler``: TaskIQ's scheduler (label schedules, plus Redis ones)."""

    target = "tenxgraph_api.jobs.scheduler:scheduler"

    def default_extra(self, extra: list[str]) -> list[str]:
        if any(_is_flag(arg, "--update-interval") for arg in extra):
            return extra
        return [*extra, "--update-interval", "10"]

    def run_taskiq(self, argv: list[str]) -> int | None:
        from taskiq.cli.scheduler.cmd import SchedulerCMD

        SchedulerCMD().exec(argv)
        return None
