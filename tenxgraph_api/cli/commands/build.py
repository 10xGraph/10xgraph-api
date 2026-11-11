"""Build command implementation."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from tenxgraph_api.cli.capabilities import truthy_env
from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.commands.build_prompts import ask_questions
from tenxgraph_api.cli.config_editor.validation import production_readiness_warnings
from tenxgraph_api.cli.constants import (
    DEFAULT_CONFIG_FILE,
    DEFAULT_PORT,
    DEFAULT_PYTHON_VERSION,
    DEFAULT_SERVICE_NAME,
)
from tenxgraph_api.cli.core.config import resolve_default_config
from tenxgraph_api.cli.core.validation import Validator
from tenxgraph_api.cli.exceptions import DockerError, FileOperationError, ValidationError
from tenxgraph_api.cli.templates.defaults import (
    CLI_PACKAGE,
    SECRET_FILE_PATTERNS,
    generate_docker_compose_content,
    generate_dockerfile_content,
    generate_dockerignore_content,
    generate_k8s_manifest_content,
    merge_ignore_patterns,
)
from tenxgraph_api.jobs.config import WorkerConfig
from tenxgraph_api.src.app.core.auth.auth_config import check_auth_config


# The pip extra of 10xgraph-api that installs each broker's TaskIQ packages.
BROKER_EXTRAS = {"redis_stream": "jobs", "rabbitmq": "jobs-rabbitmq", "kafka": "jobs-kafka"}


class BuildCommand(BaseCommand):
    """Command to generate Dockerfile and docker-compose.yml for the application."""

    def _read_worker_config(self) -> WorkerConfig | None:
        """The ``worker`` key of the project config, or None when there is none to read."""
        config_path = resolve_default_config(DEFAULT_CONFIG_FILE)
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        raw = config.get("worker") if isinstance(config, dict) else None
        if raw is None:
            return None
        try:
            return WorkerConfig.model_validate(raw)
        except PydanticValidationError as exc:
            raise ValidationError(
                f'Invalid "worker" key in {config_path.name}: {exc}', field="worker"
            ) from exc

    def _warn_not_production_ready(self) -> None:
        """Warn about settings in the project config that are risky in production."""
        config_path = resolve_default_config(DEFAULT_CONFIG_FILE)
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.logger.debug("Skipping the production-readiness check: %s", exc)
            return
        if not isinstance(config, dict):
            return
        # auth findings come from the shared auth rules below
        for issue in production_readiness_warnings(config):
            if issue["path"] not in ("auth", "authorization"):
                self.output.warning(f"{issue['path']}: {issue['message']}")
        for auth_issue in check_auth_config(config, production=True):
            suffix = " (the image would not boot)" if auth_issue.level == "error" else ""
            self.output.warning(f"{auth_issue}{suffix}")

    def execute(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        output_file: str = "Dockerfile",
        force: bool = False,
        python_version: str = DEFAULT_PYTHON_VERSION,
        port: int = DEFAULT_PORT,
        docker_compose: bool | None = None,
        k8s: bool | None = None,
        service_name: str = DEFAULT_SERVICE_NAME,
        worker: bool | None = None,
        scheduler: bool | None = None,
        yes: bool = False,
        non_interactive: bool = False,
        **kwargs: Any,
    ) -> int:
        """Execute the build command.

        Args:
            output_file: Output Dockerfile path
            force: Overwrite existing files
            python_version: Python version to use
            port: Port to expose
            docker_compose: Generate docker-compose.yml (None: not passed, ask at a terminal)
            k8s: Generate a Kubernetes manifest (None: not passed, ask at a terminal)
            service_name: Service name for docker-compose
            worker: Add a worker service (None: ask, else on when the config has a worker)
            scheduler: Add a scheduler service (None: ask, else off)
            yes: Do not ask questions
            non_interactive: Do not ask questions
            **kwargs: Additional arguments

        Returns:
            Exit code
        """
        try:
            if docker_compose and k8s:
                raise ValidationError(
                    "--docker-compose and --k8s cannot be used together: the compose image has "
                    "no start command, so the pod would crash-loop. Run build once with "
                    "--k8s (Dockerfile and k8s.yaml) or once with --docker-compose.",
                    field="k8s",
                )
            self.output.command_header(
                "build",
                "Generate Dockerfile (and optional docker-compose.yml) for production image",
                color="yellow",
            )

            worker_config = self._read_worker_config()
            can_run_worker = worker_config is not None and worker_config.broker.type != "memory"
            if (worker or scheduler) and not can_run_worker:
                raise ValidationError(
                    "--worker and --scheduler need a worker key in the config with a broker "
                    "other than memory: the memory broker runs tasks inside the API process, "
                    "so there is no worker to run.",
                    field="worker",
                )

            target = None
            if docker_compose is not None or k8s is not None:
                target = "compose" if docker_compose else "k8s" if k8s else "dockerfile"
            overwrite = force
            prompts = self.output.prompts()
            if not (yes or non_interactive or truthy_env("CI")) and prompts.interactive:
                answers = ask_questions(
                    prompts,
                    target=target,
                    worker=worker,
                    scheduler=scheduler,
                    can_run_worker=can_run_worker,
                    output_file=output_file,
                    force=force,
                )
                if answers is None or not answers.overwrite:
                    self.output.info("Cancelled. No files were written.", emoji=False)
                    return 0
                target, worker, scheduler, overwrite = answers
            else:
                target = target or "dockerfile"
                if worker is None:
                    worker = can_run_worker and target != "dockerfile"
                scheduler = bool(scheduler)
            if (worker or scheduler) and target == "dockerfile":
                raise ValidationError(
                    "The worker and scheduler services need --docker-compose or --k8s.",
                    field="worker",
                )
            if scheduler and not worker:
                raise ValidationError("--scheduler needs the worker service.", field="scheduler")
            if worker_config is not None and not can_run_worker and target != "dockerfile":
                self.output.info(
                    "worker.broker is memory: tasks run inside the API process, so no worker "
                    "service is generated.",
                    emoji=False,
                )
            tasks = worker_config.tasks if worker_config else []

            steps: list[tuple[str, str]] = [
                ("validate", "Validating build options"),
                ("sources", "Discovering project sources"),
                ("dockerfile", "Generating Dockerfile"),
                ("dockerignore", "Generating .dockerignore"),
            ]
            if target == "compose":
                steps.append(("compose", "Generating docker-compose.yml"))
            if target == "k8s":
                steps.append(("k8s", "Generating k8s.yaml"))

            generated = ["Dockerfile", ".dockerignore"]
            services = [service_name, *(["worker"] if worker else [])]
            if scheduler:
                services.append("scheduler")
            timeline = self.output.timeline("Assembling deployment assets", steps=tuple(steps))
            with timeline:
                with timeline.step("validate") as step:
                    validated_port = Validator.validate_port(port)
                    validated_python_version = Validator.validate_python_version(python_version)
                    validated_service_name = Validator.validate_service_name(service_name)
                    output_path = Validator.validate_path(output_file)
                    targets = {"compose": Path("docker-compose.yml"), "k8s": Path("k8s.yaml")}
                    for path in (output_path, targets.get(target)):
                        if path is not None and path.exists() and not overwrite:
                            raise FileOperationError(
                                f"{path.name} already exists at {path}. Use --force to overwrite.",
                                file_path=str(path),
                            )
                    step.detail(
                        f"python {validated_python_version} · port {validated_port} · "
                        f"service {validated_service_name}"
                    )

                with timeline.step("sources") as step:
                    requirements_files, requirements_file = self._discover_requirements(Path.cwd())
                    has_pyproject = (Path.cwd() / "pyproject.toml").is_file()
                    if requirements_files:
                        step.detail(f"requirements: {requirements_files[0]}")
                    elif has_pyproject:
                        step.detail("dependencies: pyproject.toml")
                    else:
                        step.detail(
                            "no requirements.txt or pyproject.toml; installing "
                            "10xgraph-api from PyPI"
                        )
                    dep_source = requirements_files[0] if requirements_files else None
                    if dep_source is None and has_pyproject:
                        dep_source = Path.cwd() / "pyproject.toml"
                    if dep_source is not None and not self._lists_server_package(dep_source):
                        self.output.warning(
                            f"{dep_source.name} does not list {CLI_PACKAGE}. The image installs "
                            "only the dependencies you list, so add it or the container "
                            "cannot start the server."
                        )
                    if worker and worker_config is not None:
                        extra = BROKER_EXTRAS[worker_config.broker.type]
                        if dep_source is None or not self._lists_extra(dep_source, extra):
                            self.output.warning(
                                f"{dep_source.name if dep_source else 'The image'} does not list "
                                f"{CLI_PACKAGE}[{extra}], so the worker cannot start. Add it to "
                                "your dependencies."
                            )

                    self._warn_not_production_ready()

                with timeline.step("dockerfile") as step:
                    dockerfile_content = generate_dockerfile_content(
                        python_version=validated_python_version,
                        port=validated_port,
                        requirements_file=requirements_file,
                        has_requirements=bool(requirements_files),
                        omit_cmd=target == "compose",
                        has_pyproject=has_pyproject,
                    )
                    self._write(output_path, dockerfile_content)
                    step.detail(str(output_path))

                dockerignore_path = output_path.parent / ".dockerignore"
                with timeline.step("dockerignore") as step:
                    if dockerignore_path.exists() and not force:
                        # Keep the developer's file, but never let it ship credentials
                        merged = merge_ignore_patterns(
                            dockerignore_path.read_text(encoding="utf-8"), SECRET_FILE_PATTERNS
                        )
                        if merged is None:
                            step.skip(f"{dockerignore_path} already exists")
                        else:
                            dockerignore_path.write_text(merged, encoding="utf-8")
                            step.detail(f"{dockerignore_path}: added missing secret patterns")
                    else:
                        dockerignore_path.write_text(
                            generate_dockerignore_content(), encoding="utf-8"
                        )
                        step.detail(str(dockerignore_path))

                if target == "compose":
                    with timeline.step("compose") as step:
                        self._write(
                            targets["compose"],
                            generate_docker_compose_content(
                                validated_service_name,
                                validated_port,
                                worker=bool(worker),
                                scheduler=scheduler,
                                tasks=tasks,
                            ),
                        )
                        generated.append("docker-compose.yml")
                        step.detail("docker-compose.yml")

                if target == "k8s":
                    with timeline.step("k8s") as step:
                        self._write(
                            targets["k8s"],
                            generate_k8s_manifest_content(
                                validated_service_name,
                                validated_port,
                                worker=bool(worker),
                                scheduler=scheduler,
                                tasks=tasks,
                            ),
                        )
                        generated.append("k8s.yaml")
                        step.detail("k8s.yaml")

            self.output.completion_screen(
                "Build assets ready",
                "Deployment files generated successfully",
                details={
                    "Files": ", ".join(generated),
                    "Service": validated_service_name,
                    "Services": ", ".join(services),
                    "Port": validated_port,
                },
                next_steps=(
                    ["docker compose up --build"]
                    if target == "compose"
                    else [f"docker build -t {validated_service_name} ."]
                ),
            )

            return 0

        except (ValidationError, DockerError, FileOperationError) as e:
            return self.handle_error(e)
        except Exception as e:
            docker_error = DockerError(f"Failed to generate Docker files: {e}")
            docker_error.__cause__ = e
            return self.handle_error(docker_error)

    def _discover_requirements(self, current_dir: Path) -> tuple[list[Path], str]:
        """Discover requirements files in the project.

        Args:
            current_dir: Current directory to search in

        Returns:
            Tuple of (found_files_list, chosen_filename_str)
        """
        requirements_files = []
        requirements_paths = [
            current_dir / "requirements.txt",
            current_dir / "requirements" / "requirements.txt",
            current_dir / "requirements" / "base.txt",
            current_dir / "requirements" / "production.txt",
        ]

        for req_path in requirements_paths:
            if req_path.exists():
                requirements_files.append(req_path)

        if not requirements_files:
            self.logger.warning("No requirements.txt file found in common locations")

        requirements_file = "requirements.txt"
        if requirements_files:
            # Path relative to the build context, so `requirements/base.txt` keeps its folder
            requirements_file = requirements_files[0].relative_to(current_dir).as_posix()
            if len(requirements_files) > 1:
                self.logger.info(f"Found multiple requirements files, using: {requirements_file}")

        return requirements_files, requirements_file

    @staticmethod
    def _dependencies(dep_source: Path) -> list[str] | None:
        """The dependency lines of a requirements file or pyproject.toml; None if unreadable."""
        try:
            if dep_source.suffix == ".toml":
                with dep_source.open("rb") as f:
                    return tomllib.load(f).get("project", {}).get("dependencies", [])
            return dep_source.read_text(encoding="utf-8").splitlines()
        except (OSError, tomllib.TOMLDecodeError):
            return None

    @staticmethod
    def _lists_server_package(dep_source: Path) -> bool:
        """Return whether a requirements file or pyproject.toml lists the API server package."""
        deps = BuildCommand._dependencies(dep_source)
        if deps is None:
            return True  # Can't tell; don't warn on a file we couldn't read
        names = (re.split(r"[\s\[<>=!~;@]", dep.strip(), maxsplit=1)[0] for dep in deps)
        return any(name.lower().replace("_", "-") == CLI_PACKAGE for name in names)

    @staticmethod
    def _lists_extra(dep_source: Path, extra: str) -> bool:
        """Return whether the file asks for ``10xgraph-api[<extra>]``; True if unreadable."""
        deps = BuildCommand._dependencies(dep_source)
        if deps is None:
            return True
        for dep in deps:
            match = re.match(r"\s*([A-Za-z0-9._-]+)\s*\[([^\]]*)\]", dep)
            if not match or match.group(1).lower().replace("_", "-") != CLI_PACKAGE:
                continue
            if extra in (e.strip().lower().replace("_", "-") for e in match.group(2).split(",")):
                return True
        return False

    def _write(self, path: Path, content: str) -> None:
        """Write one generated file.

        Raises:
            FileOperationError: If writing fails
        """
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            raise FileOperationError(
                f"Failed to write {path.name}: {e}", file_path=str(path)
            ) from e
