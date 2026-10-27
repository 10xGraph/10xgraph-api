"""Init command implementation."""

import contextlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from string import Template
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.core.prompts import Choice
from tenxgraph_api.cli.exceptions import FileOperationError, ValidationError
from tenxgraph_api.cli.templates.defaults import (
    GITIGNORE_SECRET_PATTERNS,
    generate_gitignore_content,
    merge_ignore_patterns,
)


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_SKIP_DIRS = {"__pycache__", ".ruff_cache"}

# Directories inside prod/ that are only included based on user choices
_AUTH_DIR = "auth"

# AI provider choices: the core extra that installs the SDK, the model the scaffold
# starts with, the model the eval user simulator uses, and the API key variable.
_PROVIDERS: dict[str, dict[str, str]] = {
    "google": {
        "label": "Google Gemini",
        "extra": "google-genai",
        "model": "gemini-2.5-flash",
        "sim_model": "gemini/gemini-2.5-flash",
        "api_key_env": "GOOGLE_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "extra": "openai",
        "model": "gpt-4o-mini",
        "sim_model": "openai/gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "extra": "anthropic",
        "model": "claude-sonnet-5-5",
        "sim_model": "anthropic/claude-sonnet-5-5",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
}
_DEFAULT_PROVIDER = "google"


def _slugify(name: str) -> str:
    """Convert 'WeatherBot' → 'weather-bot'."""
    s = re.sub(r"([A-Z])", r"-\1", name).lstrip("-").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _strip_env_blocks(content: str, *, keep_redis: bool, keep_jwt: bool) -> str:
    """Remove conditional marker blocks that are not needed."""
    if not keep_redis:
        content = re.sub(r"##\[IF_REDIS\]##.*?##\[/IF_REDIS\]##\n?", "", content, flags=re.DOTALL)
    else:
        content = re.sub(r"##\[IF_REDIS\]##\n?", "", content)
        content = re.sub(r"##\[/IF_REDIS\]##\n?", "", content)

    if not keep_jwt:
        content = re.sub(r"##\[IF_JWT\]##.*?##\[/IF_JWT\]##\n?", "", content, flags=re.DOTALL)
    else:
        content = re.sub(r"##\[IF_JWT\]##\n?", "", content)
        content = re.sub(r"##\[/IF_JWT\]##\n?", "", content)

    return content


def _template_vars(context: dict[str, Any]) -> dict[str, Any]:
    """Derive the template variables that depend on the user's answers.

    The production pyproject.toml lists only what the answers need: the provider SDK
    through the core extra, PyJWT for JWT auth, redis for the Redis rate limiter, and
    websockets for WebSocket streaming / realtime. The generated Dockerfile installs
    exactly this list.
    """
    provider = _PROVIDERS[context.get("provider", _DEFAULT_PROVIDER)]
    api_extras = []
    if context.get("auth") == "jwt":
        api_extras.append("jwt")
    if context.get("rate_limit") == "redis":
        api_extras.append("redis")
    if context.get("websocket"):
        api_extras.append("websocket")
    return {
        "llm_provider": context.get("provider", _DEFAULT_PROVIDER),
        "llm_model": provider["model"],
        "llm_sim_model": provider["sim_model"],
        "llm_api_key_env": provider["api_key_env"],
        "core_requirement": f"10xgraph[{provider['extra']}]",
        "api_requirement": (
            f"10xgraph-api[{','.join(api_extras)}]" if api_extras else "10xgraph-api"
        ),
    }


class InitCommand(BaseCommand):
    """Command to initialize a new agent project interactively."""

    def execute(  # noqa: PLR0913
        self,
        path: str = ".",
        force: bool = False,
        agent_name: str | None = None,
        template: str | None = None,
        auth: str | None = None,
        rate_limit: str | None = None,
        provider: str | None = None,
        websocket: bool = False,
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
                    auth=auth,
                    rate_limit=rate_limit,
                    provider=provider,
                    websocket=websocket,
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
                    config = self._build_config(context, is_prod)
                    self._write_file(
                        config_path,
                        json.dumps(config, indent=2) + "\n",
                        force=force or not config_pre_existed,
                    )
                    step.detail(str(config_path))

            agent_name = context["agent_name"]
            provider_info = _PROVIDERS[context.get("provider", _DEFAULT_PROVIDER)]
            # Production lists these in pyproject.toml; quick-start has no pyproject, so it
            # needs the install step.
            install_step = (
                []
                if is_prod
                else [f'pip install "{_template_vars(context)["core_requirement"]}"']
                + (['pip install "10xgraph-api[websocket]"'] if context.get("websocket") else [])
            )
            self.output.completion_screen(
                "Project ready",
                f'"{agent_name}" was created successfully',
                details={
                    "Location": base_path.resolve(),
                    "Template": "production" if is_prod else "quick-start",
                    "Auth": context["auth"],
                    "AI provider": provider_info["label"],
                    "WebSocket": "on" if context.get("websocket") else "off",
                },
                next_steps=[
                    f"cd {base_path}",
                    *install_step,
                    f"Copy .env.example to .env and set {provider_info['api_key_env']}.",
                    "10xgraph dev",
                ],
            )

            return 0

        except (FileOperationError, ValidationError) as e:
            return self.handle_error(e)
        except Exception as e:
            return self.handle_error(FileOperationError(f"Failed to initialize project: {e}"))

    def _non_interactive_context(
        self,
        *,
        path: str,
        agent_name: str | None,
        template: str | None,
        auth: str | None,
        rate_limit: str | None,
        provider: str | None = None,
        websocket: bool = False,
    ) -> dict[str, Any]:
        """Resolve a complete, reproducible scaffold recipe without prompting."""
        resolved_template = (template or "quick-start").strip().lower().replace("_", "-")
        if resolved_template not in {"quick-start", "production"}:
            raise ValidationError(
                f"Invalid template '{resolved_template}'. Choose quick-start or production.",
                field="template",
            )

        inferred_name = Path(path).resolve().name if path not in {"", "."} else "MyAgent"
        resolved_name = (agent_name or inferred_name).strip()
        slug = _slugify(resolved_name)
        if not resolved_name or not slug:
            raise ValidationError(
                "Agent name must contain at least one letter or number.",
                field="name",
            )

        resolved_auth = (auth or "none").strip().lower()
        if resolved_auth not in {"none", "jwt", "custom"}:
            raise ValidationError(
                f"Invalid auth mode '{resolved_auth}'. Choose none, jwt, or custom.",
                field="auth",
            )

        resolved_rate_limit = (rate_limit or "none").strip().lower()
        if resolved_rate_limit not in {"none", "memory", "redis"}:
            raise ValidationError(
                f"Invalid rate-limit mode '{resolved_rate_limit}'. "
                "Choose none, memory, or redis.",
                field="rate_limit",
            )

        resolved_provider = (provider or _DEFAULT_PROVIDER).strip().lower()
        if resolved_provider not in _PROVIDERS:
            raise ValidationError(
                f"Invalid AI provider '{resolved_provider}'. Choose {', '.join(_PROVIDERS)}.",
                field="provider",
            )

        if resolved_template == "quick-start" and (
            resolved_auth != "none" or resolved_rate_limit != "none"
        ):
            raise ValidationError(
                "--auth and --rate-limit require --template production.",
                field="template",
            )

        return {
            "agent_name": resolved_name,
            "agent_name_slug": slug,
            "setup_type": "production" if resolved_template == "production" else "quick_start",
            "provider": resolved_provider,
            "websocket": websocket,
            "auth": resolved_auth,
            "rate_limit": resolved_rate_limit,
            "rl_requests": 100,
            "rl_window": 60,
            "rl_by": "ip",
            "rl_trusted_proxy": False,
        }

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _prompt_user(self) -> dict | None:  # noqa: PLR0911, PLR0912
        prompts = self.output.prompts()
        prompts.require_interactive(
            field="template",
            alternatives=(
                "Re-run with --yes to accept the defaults, or --non-interactive "
                "with --name/--template for a reproducible recipe."
            ),
        )

        agent_name = prompts.text("What is your agent name?", default="MyAgent")
        if agent_name is None:
            return None

        template = prompts.select(
            "Which project template?",
            [
                Choice(
                    "quick_start",
                    "Quick Start",
                    "Minimal graph, no auth — fastest path to a running agent",
                ),
                Choice(
                    "production",
                    "Production",
                    "Auth, rate limiting, evals, and tests scaffolded in",
                ),
            ],
            default="quick_start",
        )
        if template is None:
            return None

        provider = prompts.select(
            "Which AI provider?",
            [
                Choice("google", "Google Gemini", "Installs google-genai · GOOGLE_API_KEY"),
                Choice("openai", "OpenAI", "Installs openai · OPENAI_API_KEY"),
                Choice("anthropic", "Anthropic Claude", "Installs anthropic · ANTHROPIC_API_KEY"),
            ],
            default=_DEFAULT_PROVIDER,
        )
        if provider is None:
            return None

        websocket = prompts.confirm(
            "Enable WebSocket streaming and realtime audio? "
            "(/v1/graph/ws, /v1/graph/live; installs websockets)",
            default=False,
        )
        if websocket is None:
            return None

        is_prod = template == "production"
        context: dict[str, Any] = {
            "agent_name": agent_name,
            "agent_name_slug": _slugify(agent_name),
            "setup_type": template,
            "provider": provider,
            "websocket": websocket,
            "auth": "none",
            "rate_limit": "none",
        }

        if not is_prod:
            return context

        # --- Production questions ---

        auth = prompts.select(
            "How should requests be authenticated?",
            [
                Choice("none", "None", "Open endpoints — development only"),
                Choice("jwt", "JWT", "Requires JWT_SECRET_KEY and JWT_ALGORITHM"),
                Choice("custom", "Custom", "Scaffolds a BaseAuth subclass under auth/"),
            ],
            default="none",
        )
        if auth is None:
            return None
        context["auth"] = auth

        if auth == "none":
            return context

        rate_limit = prompts.select(
            "Rate limiting backend?",
            [
                Choice("none", "None", "No request throttling"),
                Choice("memory", "Memory", "Per-process counters — single instance only"),
                Choice("redis", "Redis", "Shared counters — requires REDIS_URL"),
            ],
            default="none",
        )
        if rate_limit is None:
            return None
        context["rate_limit"] = rate_limit

        if rate_limit == "none":
            return context

        rl_requests = prompts.text(
            "Max requests per window?",
            default="100",
            validate=lambda v: (v.isdigit() and int(v) > 0) or "Enter a positive integer",
        )
        if rl_requests is None:
            return None
        context["rl_requests"] = int(rl_requests)

        rl_window = prompts.text(
            "Window size (seconds)?",
            default="60",
            validate=lambda v: (v.isdigit() and int(v) > 0) or "Enter a positive integer",
        )
        if rl_window is None:
            return None
        context["rl_window"] = int(rl_window)

        rl_by = prompts.select(
            "Count requests per?",
            [
                Choice("ip", "Per IP", "Recommended — each client gets its own budget"),
                Choice("global", "Global", "One shared budget across all clients"),
            ],
            default="ip",
        )
        if rl_by is None:
            return None
        context["rl_by"] = rl_by

        rl_proxy = prompts.confirm(
            "Behind a reverse proxy? (reads the real IP from forwarded headers)",
            default=False,
        )
        if rl_proxy is None:
            return None
        context["rl_trusted_proxy"] = rl_proxy

        return context

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _print_summary(self, context: dict) -> None:
        is_prod = context["setup_type"] == "production"
        setup_label = "Production" if is_prod else "Quick Start"

        rows: list[tuple[str, str]] = [
            ("Agent name", context["agent_name"]),
            ("Package name", context["agent_name_slug"]),
            ("Setup", setup_label),
            ("AI provider", _PROVIDERS[context.get("provider", _DEFAULT_PROVIDER)]["label"]),
            ("WebSocket", "On" if context.get("websocket") else "Off"),
        ]
        if is_prod:
            auth = context["auth"]
            rows.append(("Auth", "None" if auth == "none" else auth.upper()))

            rl = context["rate_limit"]
            if rl == "none":
                rows.append(("Rate limit", "None"))
            else:
                rl_by = "Global" if context.get("rl_by") == "global" else "Per IP"
                proxy = " · proxy headers on" if context.get("rl_trusted_proxy") else ""
                rows.append(
                    (
                        "Rate limit",
                        f"{rl.capitalize()} · {context.get('rl_requests', 100)} req / "
                        f"{context.get('rl_window', 60)}s · {rl_by}{proxy}",
                    )
                )

        self.output.print_table(
            ["Setting", "Value"],
            [[label, value] for label, value in rows],
            title="Project summary",
        )

    def _print_next_steps(self, context: dict, is_prod: bool) -> None:
        steps: list[tuple[str, str]] = []

        steps.append(("10xgraph skills", "Install coding agent skills"))

        if is_prod:
            steps.append(("pre-commit install", "Set up Git hooks (ruff, bandit, etc.)"))

        provider = _PROVIDERS[context.get("provider", _DEFAULT_PROVIDER)]
        if not is_prod:
            # Production lists the provider SDK in pyproject.toml already.
            steps.append(
                (f'pip install "10xgraph[{provider["extra"]}]"', "Add the AI provider library")
            )
        steps.append(
            ("cp .env.example .env", f"Copy env template, then set {provider['api_key_env']}")
        )

        if context["auth"] == "jwt":
            steps.append(("# Set JWT_SECRET_KEY in .env", "Required for JWT auth to work"))

        if context["rate_limit"] == "redis":
            steps.append(("# Set REDIS_URL in .env", "Required for Redis rate limiting"))

        steps.append(("10xgraph dev", "Launch your agent"))

        self.output.print_table(
            ["#", "Command", "Purpose"],
            [
                [str(index), command, description]
                for index, (command, description) in enumerate(steps, 1)
            ],
            title="Next steps",
        )

    # ------------------------------------------------------------------
    # Config generation
    # ------------------------------------------------------------------

    def _build_config(self, context: dict, is_prod: bool) -> dict:
        config: dict = {
            "agent": "graph.agent:app",
            "env": ".env",
            "auth": None,
            "thread_name_generator": None,
            # AG-UI endpoint for clients such as CopilotKit. Off until turned on here.
            "ag_ui": {"enabled": False},
            # WebSocket streaming (/v1/graph/ws) and realtime audio (/v1/graph/live).
            # Off: neither route is mounted and no WebSocket library is needed.
            "websocket": {"enabled": bool(context.get("websocket"))},
        }

        if not is_prod:
            return config

        auth = context["auth"]
        if auth == "jwt":
            # JWT is configured with the bare string. The dict form requires a "path"
            # (see GraphConfig.auth_config) and is only valid for method "custom".
            config["auth"] = "jwt"
        elif auth == "custom":
            config["auth"] = {"method": "custom", "path": "auth.agent_auth:AgentAuth"}

        if auth in ("jwt", "custom"):
            # Secure by default in production: a thread is accessible only to its owner.
            # Change to "allow_all" to let any authenticated user access any thread, or
            # point at a custom AuthorizationBackend ("module:attr").
            config["authorization"] = "ownership"

        config["thread_name_generator"] = "graph.thread_name_generator:MyNameGenerator"
        config["injectq"] = "graph.agent:container"

        rate_limit = context["rate_limit"]
        if rate_limit != "none":
            config["rate_limit"] = {
                "enabled": True,
                "backend": rate_limit,
                "requests": context.get("rl_requests", 100),
                "window": context.get("rl_window", 60),
                "by": context.get("rl_by", "ip"),
                "trusted_proxy_headers": context.get("rl_trusted_proxy", False),
                "exclude_paths": ["/ping", "/docs", "/redoc", "/openapi.json"],
            }
            if rate_limit == "redis":
                # Resolved from REDIS_URL in .env; the redis backend refuses to start
                # without a URL or a Redis client bound in InjectQ.
                config["rate_limit"]["redis"] = {"url": "${REDIS_URL}"}

        return config

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def _should_skip(self, src: Path, template_dir: Path, context: dict, is_prod: bool) -> bool:
        """Return True if this template file should not be copied."""
        if any(part in _SKIP_DIRS for part in src.parts):
            return True
        if not is_prod:
            return False
        # auth/ is only included for custom auth; skip for none and jwt
        rel_parts = src.relative_to(template_dir).parts
        return rel_parts[0] == _AUTH_DIR and context["auth"] != "custom"

    def _render(self, src: Path, context: dict, is_prod: bool) -> str:
        content = src.read_text(encoding="utf-8")

        # Strip conditional blocks in .env.example before substitution
        if src.name == ".env.example" and is_prod:
            content = _strip_env_blocks(
                content,
                keep_redis=context["rate_limit"] == "redis",
                keep_jwt=context["auth"] == "jwt",
            )

        with contextlib.suppress(Exception):
            content = Template(content).safe_substitute({**context, **_template_vars(context)})

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
