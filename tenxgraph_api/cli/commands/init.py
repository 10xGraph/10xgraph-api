"""Init command implementation."""

import json
from collections.abc import Callable
from pathlib import Path
from string import Template
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.commands.init_context import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    STORAGE_PRESETS,
    WORKERS,
    build_config,
    build_context,
    check_agent_name,
    slugify,
    strip_blocks,
    template_flags,
)
from tenxgraph_api.cli.commands.init_context import (
    template_vars as _template_vars,
)
from tenxgraph_api.cli.commands.init_prompts import ask_questions
from tenxgraph_api.cli.exceptions import FileOperationError, ValidationError
from tenxgraph_api.cli.templates.defaults import (
    GITIGNORE_SECRET_PATTERNS,
    generate_gitignore_content,
    merge_ignore_patterns,
)


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_SKIP_DIRS = {"__pycache__", ".ruff_cache"}
_AUTH_DIR = "auth"
_OPTIONAL_DIRS = ("evals", "tests")
_WORKER_DIR = "worker"
_CONFIG_HINT = "10xgraph config for more settings."


def _escape_agent_name(name: str, filename: str) -> str:
    """Make ``name`` safe inside the quoted value of the file it is substituted into."""
    if filename.endswith(".toml"):
        return name.replace("\\", "\\\\").replace('"', '\\"')
    if filename.startswith(".env"):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return escaped.replace("$", "\\$").replace("`", "\\`")
    return name


def _agent_name_problem(value: str) -> bool | str:
    """Prompt validator: True when the name is usable, else the message to show."""
    if not value.strip() or not slugify(value):
        return "Use at least one letter or number"
    try:
        check_agent_name(value)
    except ValidationError as exc:
        return exc.message
    return True


class InitCommand(BaseCommand):
    """Command to initialize a new agent project interactively."""

    def execute(  # noqa: PLR0913
        self,
        path: str = ".",
        force: bool = False,
        agent_name: str | None = None,
        template: str | None = None,
        mode: str | None = None,
        auth: str | None = None,
        storage: str | None = None,
        cache_store: str | None = None,
        rate_limit: bool | None = None,
        token_revocation: bool | None = None,
        provider: str | None = None,
        websocket: bool = False,
        evals: bool | None = None,
        tests: bool | None = None,
        ag_ui: bool | None = None,
        otel: bool | None = None,
        worker: str | None = None,
        yes: bool = False,
        non_interactive: bool = False,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> int:
        try:
            self.output.command_header(
                "init", "Create a new 10xGraph agent project", color="magenta"
            )

            context: dict[str, Any] | None
            if yes or non_interactive:
                context = self._non_interactive_context(
                    path=path,
                    agent_name=agent_name,
                    template=template,
                    mode=mode,
                    auth=auth,
                    storage=storage,
                    cache_store=cache_store,
                    rate_limit=rate_limit,
                    token_revocation=token_revocation,
                    provider=provider,
                    websocket=websocket,
                    evals=evals,
                    tests=tests,
                    ag_ui=ag_ui,
                    otel=otel,
                    worker=worker,
                )
            else:
                context = self._prompt_user()
            if context is None:
                self.output.info("Cancelled.", emoji=False)
                return 0

            self._print_summary(context)

            base_path = Path(path)
            # Capture this BEFORE any writes: a config file present now is the
            # developer's own (a prior init or a hand edit), and must not be
            # clobbered unless they passed --force.
            config_path = base_path / "10xgraph.json"
            config_pre_existed = config_path.exists()

            is_prod = context["setup_type"] == "production"
            template_dir = _TEMPLATES_DIR / ("prod" if is_prod else "dev")

            if dry_run:
                planned = [
                    str(src.relative_to(template_dir))
                    for src in sorted(template_dir.rglob("*"))
                    if src.is_file() and not self._should_skip(src, template_dir, context, is_prod)
                ]
                if "10xgraph.json" not in planned:
                    planned.append("10xgraph.json")
                planned.append(".gitignore")
                self.output.print_list(planned, title="Files that would be created", bullet="→")
                self.output.info("Dry run complete; no files were written.", emoji=False)
                return 0

            timeline = self.output.timeline(
                f'Scaffolding "{context["agent_name"]}"',
                steps=(
                    ("workspace", "Preparing the project directory"),
                    ("files", "Writing template files"),
                    ("config", "Generating 10xgraph.json"),
                ),
            )
            with timeline:
                with timeline.step("workspace") as step:
                    base_path.mkdir(parents=True, exist_ok=True)
                    step.detail(str(base_path.resolve()))

                with timeline.step("files") as step:
                    created = self._copy_template_dir(
                        template_dir,
                        base_path,
                        context,
                        force=force,
                        is_prod=is_prod,
                        on_file=step.detail,
                    )
                    step.detail(f"{len(created)} files from the {template_dir.name} template")
                    step.detail(self._write_gitignore(base_path))

                with timeline.step("config") as step:
                    # Regenerate 10xgraph.json from the built config. Force is safe only
                    # to override the copy this run just made; honor --force for a file
                    # that was already there.
                    config = build_config(context, is_prod)
                    self._write_file(
                        config_path,
                        json.dumps(config, indent=2) + "\n",
                        force=force or not config_pre_existed,
                    )
                    step.detail(str(config_path))

            self._print_completion(context, base_path, is_prod)

            return 0

        except (FileOperationError, ValidationError) as e:
            return self.handle_error(e)
        except Exception as e:
            return self.handle_error(FileOperationError(f"Failed to initialize project: {e}"))

    def _non_interactive_context(self, **options: Any) -> dict[str, Any]:
        """Resolve a complete, reproducible scaffold recipe without prompting."""
        return build_context(**options)

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _prompt_user(self) -> dict | None:
        prompts = self.output.prompts()
        prompts.require_interactive(
            field="mode",
            alternatives=(
                "Re-run with --yes to accept the defaults, or --non-interactive "
                "with --name/--mode for a reproducible recipe."
            ),
        )
        answers = ask_questions(prompts, _agent_name_problem, DEFAULT_PROVIDER)
        if answers is None:
            return None
        return self._non_interactive_context(path=".", **answers)

    def _print_completion(self, context: dict, base_path: Path, is_prod: bool) -> None:
        provider_info = PROVIDERS[context.get("provider", DEFAULT_PROVIDER)]
        storage = context.get("storage") or "memory"
        key_step = f"Copy .env.example to .env and set {provider_info['api_key_env']}"
        details: dict[str, Any] = {
            "Location": base_path.resolve(),
            "Setup": "Full" if is_prod else "Quick",
            "Auth": context["auth"],
            "AI provider": provider_info["label"],
        }
        if is_prod:
            details["Storage"] = STORAGE_PRESETS[storage]["label"]
        if context.get("websocket"):
            details["WebSocket"] = "on"

        if not is_prod:
            install = [f'pip install "{_template_vars(context)["core_requirement"]}"']
            if context.get("websocket"):
                install.append('pip install "10xgraph-api[websocket]"')
            steps = [
                f"cd {base_path}",
                *install,
                f"{key_step}.",
                "10xgraph dev",
                _CONFIG_HINT,
            ]
            before_production: list[str] = []
        else:
            worker = context.get("worker", "none")
            env_keys = ["DATABASE_URL"] if storage.startswith("postgres") else []
            if storage.startswith("postgres") or worker != "none":
                env_keys.append("REDIS_URL")
            if worker in ("rabbitmq", "kafka"):
                env_keys.append("BROKER_URL")
            if env_keys:
                key_step += f" (and {', '.join(env_keys)})"
            steps = [f"cd {base_path}", "pip install -e .", f"{key_step}.", "10xgraph dev"]
            if worker != "none":
                steps += ["10xgraph worker", "10xgraph scheduler (run exactly one)"]
            secret = " and a strong JWT_SECRET_KEY" if context["auth"] == "jwt" else ""
            before_production = [
                "10xgraph config to review auth, rate limit, CORS origins and the other "
                "settings and make the project production ready.",
                f"Set MODE=production, ORIGINS{secret} in the deploy environment.",
                "10xgraph build to generate the Dockerfile (--docker-compose / --k8s optional).",
            ]

        self.output.completion_screen(
            "Project ready",
            f'"{context["agent_name"]}" was created successfully',
            details=details,
            next_steps=steps,
        )
        if before_production:
            self.output.print_list(before_production, title="Before production", bullet="→")
        self.output.info(
            "10xgraph skills installs the 10xGraph skill for your AI coding assistant.",
            emoji=False,
        )

    def _print_summary(self, context: dict) -> None:
        is_prod = context["setup_type"] == "production"
        setup_label = "Full" if is_prod else "Quick"

        rows: list[tuple[str, str]] = [
            ("Agent name", context["agent_name"]),
            ("Package name", context["agent_name_slug"]),
            ("Setup", setup_label),
            ("AI provider", PROVIDERS[context.get("provider", DEFAULT_PROVIDER)]["label"]),
        ]
        if context.get("websocket"):
            rows.append(("WebSocket", "On"))
        if is_prod:
            auth = context["auth"]
            storage = context.get("storage") or "memory"
            rows.append(("Storage", STORAGE_PRESETS[storage]["label"]))
            rows.append(("Auth", "None" if auth == "none" else auth.upper()))

            store = context.get("cache_store") or "memory"
            rows.append(("Cache store", "Redis" if store == "redis" else "Memory"))
            rows.append(("ID generator", str(context.get("id_generator", "uuid"))))
            if context.get("rate_limit"):
                rl_by = {"global": "Global", "user": "Per user"}.get(
                    str(context.get("rl_by")), "Per IP"
                )
                proxy = " · proxy headers on" if context.get("rl_trusted_proxy") else ""
                rows.append(
                    (
                        "Rate limit",
                        f"{context.get('rl_requests', 100)} req / "
                        f"{context.get('rl_window', 60)}s · {rl_by}{proxy}",
                    )
                )
            else:
                rows.append(("Rate limit", "Off"))
            if auth != "none":
                revocation = "On" if context.get("token_revocation") else "Off"
                rows.append(("Token revocation", revocation))
            worker = context.get("worker", "none")
            rows.append(
                ("Background tasks", WORKERS[worker]["label"] if worker != "none" else "None")
            )
            rows.append(("Evaluations", "On" if context.get("evals") else "Off"))
            rows.append(("Tests", "On" if context.get("tests") else "Off"))
            for key, label in (("ag_ui", "AG-UI"), ("otel", "OpenTelemetry")):
                if context.get(key):
                    rows.append((label, "On"))

        self.output.print_table(
            ["Setting", "Value"],
            [[label, value] for label, value in rows],
            title="Project summary",
        )

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def _should_skip(self, src: Path, template_dir: Path, context: dict, is_prod: bool) -> bool:
        """Return True if this template file should not be copied."""
        if any(part in _SKIP_DIRS for part in src.parts):
            return True
        if not is_prod:
            return False
        # auth/ is only included for custom auth; evals/ and tests/ only when asked for
        top = src.relative_to(template_dir).parts[0]
        if top == _AUTH_DIR:
            return context["auth"] != "custom"
        if top == _WORKER_DIR:
            return context.get("worker", "none") == "none"
        if top in _OPTIONAL_DIRS:
            return not context.get(top)
        return False

    def _render(self, src: Path, context: dict, is_prod: bool) -> str:
        content = src.read_text(encoding="utf-8")

        # Resolve ##[IF_NAME]## blocks in every production template file
        if is_prod:
            try:
                content = strip_blocks(content, template_flags(context))
            except FileOperationError as exc:
                raise FileOperationError(f"{src.name}: {exc.message}", file_path=str(src)) from exc

        try:
            values = {**context, **_template_vars(context)}
            if "agent_name" in values:
                values["agent_name"] = _escape_agent_name(str(values["agent_name"]), src.name)
            content = Template(content).safe_substitute(values)
        except Exception as exc:
            raise FileOperationError(
                f"Could not fill in the template {src.name}: {exc}", file_path=str(src)
            ) from exc

        return content

    def _copy_template_dir(
        self,
        template_dir: Path,
        dest_dir: Path,
        context: dict,
        *,
        force: bool,
        is_prod: bool,
        on_file: Callable[[str], None] | None = None,
    ) -> set[Path]:
        created: set[Path] = set()
        for src in sorted(template_dir.rglob("*")):
            if src.is_dir():
                continue
            if self._should_skip(src, template_dir, context, is_prod):
                continue
            rel = src.relative_to(template_dir)
            dest = dest_dir / rel
            content = self._render(src, context, is_prod)
            self._write_file(dest, content, force=force)
            if on_file is not None:
                on_file(str(rel).replace("\\", "/"))
            created.add(dest)
        return created

    def _write_gitignore(self, base_path: Path) -> str:
        """Write a .gitignore, or add the secret patterns an existing one lacks.

        The next steps tell the developer to put API keys and JWT_SECRET_KEY in .env, so
        .env must be ignored. An existing .gitignore is never replaced, even with --force.
        """
        path = base_path / ".gitignore"
        try:
            if not path.exists():
                path.write_text(generate_gitignore_content(), encoding="utf-8")
                return ".gitignore"
            merged = merge_ignore_patterns(
                path.read_text(encoding="utf-8"), GITIGNORE_SECRET_PATTERNS
            )
            if merged is None:
                return ".gitignore already ignores .env and credentials"
            path.write_text(merged, encoding="utf-8")
            return ".gitignore: added missing secret patterns"
        except OSError as e:
            raise FileOperationError(
                f"Failed to write file {path}: {e}", file_path=str(path)
            ) from e

    def _write_file(self, path: Path, content: str, *, force: bool) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not force:
                raise FileOperationError(
                    f"File already exists: {path}. Use --force to overwrite.",
                    file_path=str(path),
                )
            path.write_text(content, encoding="utf-8")
            self.logger.debug("Wrote file: %s", path)
        except OSError as e:
            raise FileOperationError(
                f"Failed to write file {path}: {e}", file_path=str(path)
            ) from e
