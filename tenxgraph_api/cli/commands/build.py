"""Build command implementation."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
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


class BuildCommand(BaseCommand):
    """Command to generate Dockerfile and docker-compose.yml for the application."""

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
        for issue in production_readiness_warnings(config):
            self.output.warning(f"{issue['path']}: {issue['message']}")

    def execute(  # noqa: PLR0912, PLR0915
        self,
        output_file: str = "Dockerfile",
        force: bool = False,
        python_version: str = DEFAULT_PYTHON_VERSION,
        port: int = DEFAULT_PORT,
        docker_compose: bool = False,
        k8s: bool = False,
        service_name: str = DEFAULT_SERVICE_NAME,
        **kwargs: Any,
    ) -> int:
        """Execute the build command.

        Args:
            output_file: Output Dockerfile path
            force: Overwrite existing files
            python_version: Python version to use
            port: Port to expose
            docker_compose: Generate docker-compose.yml
            k8s: Generate a Kubernetes manifest (Deployment + Service)
            service_name: Service name for docker-compose
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

            steps: list[tuple[str, str]] = [
                ("validate", "Validating build options"),
                ("sources", "Discovering project sources"),
                ("dockerfile", "Generating Dockerfile"),
                ("dockerignore", "Generating .dockerignore"),
            ]
            if docker_compose:
                steps.append(("compose", "Generating docker-compose.yml"))
            if k8s:
                steps.append(("k8s", "Generating k8s.yaml"))

            generated = ["Dockerfile", ".dockerignore"]
            timeline = self.output.timeline("Assembling deployment assets", steps=tuple(steps))
            with timeline:
                with timeline.step("validate") as step:
                    validated_port = Validator.validate_port(port)
                    validated_python_version = Validator.validate_python_version(python_version)
                    validated_service_name = Validator.validate_service_name(service_name)
                    output_path = Validator.validate_path(output_file)
                    if output_path.exists() and not force:
                        raise FileOperationError(
                            f"Dockerfile already exists at {output_path}. "
                            "Use --force to overwrite.",
                            file_path=str(output_path),
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

                    self._warn_not_production_ready()

                with timeline.step("dockerfile") as step:
                    dockerfile_content = generate_dockerfile_content(
                        python_version=validated_python_version,
                        port=validated_port,
                        requirements_file=requirements_file,
                        has_requirements=bool(requirements_files),
                        omit_cmd=docker_compose,
                        has_pyproject=has_pyproject,
                    )
                    self._write_dockerfile(output_path, dockerfile_content)
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

                if docker_compose:
                    with timeline.step("compose") as step:
                        self._write_docker_compose(
                            force=force,
                            service_name=validated_service_name,
                            port=validated_port,
                        )
                        generated.append("docker-compose.yml")
                        step.detail("docker-compose.yml")

                if k8s:
                    with timeline.step("k8s") as step:
                        self._write_k8s_manifest(
                            force=force,
                            service_name=validated_service_name,
                            port=validated_port,
                        )
                        generated.append("k8s.yaml")
                        step.detail("k8s.yaml")

            self.output.completion_screen(
                "Build assets ready",
                "Deployment files generated successfully",
                details={
                    "Files": ", ".join(generated),
                    "Service": validated_service_name,
                    "Port": validated_port,
                },
                next_steps=(
                    ["docker compose up --build"]
                    if docker_compose
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
    def _lists_server_package(dep_source: Path) -> bool:
        """Return whether a requirements file or pyproject.toml lists the API server package."""
        if dep_source.suffix == ".toml":
            try:
                with dep_source.open("rb") as f:
                    deps = tomllib.load(f).get("project", {}).get("dependencies", [])
            except (OSError, tomllib.TOMLDecodeError):
                return True  # Can't tell; don't warn on a file we couldn't read
        else:
            try:
                deps = dep_source.read_text(encoding="utf-8").splitlines()
            except OSError:
                return True
        names = (re.split(r"[\s\[<>=!~;@]", dep.strip(), maxsplit=1)[0] for dep in deps)
        return any(name.lower().replace("_", "-") == CLI_PACKAGE for name in names)

    def _write_dockerfile(self, output_path: Path, content: str) -> None:
        """Write Dockerfile content to file.

        Args:
            output_path: Path to write to
            content: Dockerfile content

        Raises:
            FileOperationError: If writing fails
        """
        try:
            output_path.write_text(content, encoding="utf-8")
        except OSError as e:
            raise FileOperationError(
                f"Failed to write Dockerfile: {e}", file_path=str(output_path)
            ) from e

    def _write_k8s_manifest(self, force: bool, service_name: str, port: int) -> None:
        """Write k8s.yaml (Deployment + Service).

        No manifest was generated before, so users hand-rolled one -- usually with
        the default 30s termination grace period, which SIGKILLs an agent run
        mid-LLM-call on every rolling deploy. The generated one sets a grace period
        that matches how long a run can actually take, plus a preStop hook.

        Raises:
            FileOperationError: If writing fails
        """
        manifest_path = Path("k8s.yaml")

        if manifest_path.exists() and not force:
            raise FileOperationError(
                f"k8s.yaml already exists at {manifest_path}. Use --force to overwrite.",
                file_path=str(manifest_path),
            )

        content = generate_k8s_manifest_content(service_name, port)

        try:
            manifest_path.write_text(content, encoding="utf-8")
        except OSError as e:
            raise FileOperationError(
                f"Failed to write k8s.yaml: {e}", file_path=str(manifest_path)
            ) from e

    def _write_docker_compose(self, force: bool, service_name: str, port: int) -> None:
        """Write docker-compose.yml file.

        Args:
            force: Overwrite existing file
            service_name: Service name to use
            port: Port to expose

        Raises:
            FileOperationError: If writing fails
        """
        compose_path = Path("docker-compose.yml")

        if compose_path.exists() and not force:
            raise FileOperationError(
                f"docker-compose.yml already exists at {compose_path}. Use --force to overwrite.",
                file_path=str(compose_path),
            )

        compose_content = generate_docker_compose_content(service_name, port)

        try:
            compose_path.write_text(compose_content, encoding="utf-8")
        except OSError as e:
            raise FileOperationError(
                f"Failed to write docker-compose.yml: {e}", file_path=str(compose_path)
            ) from e

    def _show_next_steps(self, docker_compose: bool) -> None:
        """Show next steps to the user.

        Args:
            docker_compose: Whether docker-compose was generated
        """
        if docker_compose:
            steps = [
                "Review the generated Dockerfile and docker-compose.yml",
                "Build and run with: docker compose up --build",
                "Or build separately: docker build -t 10xgraph-api .",
                "Access your API at: http://localhost:8000",
            ]
        else:
            steps = [
                "Review the generated Dockerfile",
                "Build the image: docker build -t 10xgraph-api .",
                "Run the container: docker run -p 8000:8000 10xgraph-api",
                "Access your API at: http://localhost:8000",
            ]

        self.output.print_list(
            [f"{index}. {step}" for index, step in enumerate(steps, 1)],
            title="Next steps",
            bullet="→",
        )

        production_tips = [
            "Using a multi-stage build to reduce image size",
            "Setting up proper environment variables",
            "Configuring health checks and resource limits",
            "Using a reverse proxy like nginx",
        ]

        self.output.print_list(
            production_tips,
            title="Production considerations",
            bullet="•",
        )
