"""Interactive questions for `10xgraph build`."""

from pathlib import Path
from typing import NamedTuple

from tenxgraph_api.cli.core.prompts import Choice, PromptService


class BuildAnswers(NamedTuple):
    """What the questions settled: the deploy target, the jobs services and the overwrite."""

    target: str  # compose, k8s or dockerfile
    worker: bool
    scheduler: bool
    overwrite: bool


def ask_questions(  # noqa: PLR0912
    prompts: PromptService,
    *,
    target: str | None,
    worker: bool | None,
    scheduler: bool | None,
    can_run_worker: bool,
    output_file: str,
    force: bool,
) -> BuildAnswers | None:
    """Ask what the flags left open. Returns None when the user cancels.

    ``target`` is None when no deploy flag was passed. ``can_run_worker`` is whether the config
    has a worker key with a broker other than memory.
    """
    if target is None:
        target = prompts.select(
            "Where will you deploy?",
            [
                Choice("compose", "Docker Compose", "Dockerfile and docker-compose.yml"),
                Choice("k8s", "Kubernetes", "Dockerfile and k8s.yaml"),
                Choice("dockerfile", "Dockerfile only", "Build and run the image yourself"),
            ],
            default="compose",
        )
        if target is None:
            return None

    if worker is None:
        worker = False
        if can_run_worker and target != "dockerfile":
            worker = prompts.confirm("Run a background worker?", default=True)
            if worker is None:
                return None

    if scheduler is None:
        scheduler = False
        if worker:
            scheduler = prompts.confirm("Run the scheduler?", default=False)
            if scheduler is None:
                return None

    overwrite = force
    if not force:
        names = [output_file]
        if target == "compose":
            names.append("docker-compose.yml")
        if target == "k8s":
            names.append("k8s.yaml")
        existing = [name for name in names if Path(name).exists()]
        overwrite = True
        if existing:
            overwrite = prompts.confirm(f"Overwrite existing {', '.join(existing)}?", default=False)
            if overwrite is None:
                return None
    return BuildAnswers(target, worker, scheduler, overwrite)
