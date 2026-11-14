"""Answers for `10xgraph init`: provider table, storage presets, context, template blocks."""

import re
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.exceptions import FileOperationError, ValidationError


# AI provider choices: the core extra that installs the SDK, the model the scaffold
# starts with, the model the eval user simulator uses, and the API key variable.
PROVIDERS: dict[str, dict[str, str]] = {
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
DEFAULT_PROVIDER = "google"
CACHE_STORES = ("memory", "redis")
MODES = ("quick", "full")

# Storage presets: label, cache store that goes with it. The checkpointer and store are
# chosen in the graph template from the preset key.
STORAGE_PRESETS: dict[str, dict[str, str]] = {
    "memory": {"label": "Memory", "cache_store": "memory"},
    "sqlite": {"label": "SQLite", "cache_store": "memory"},
    "postgres": {"label": "Postgres + Redis", "cache_store": "redis"},
    "postgres-memory": {"label": "Postgres + Redis + pgvector memory", "cache_store": "redis"},
}
AUTH_MODES = ("none", "jwt", "custom")
# Background worker choices: label, broker type in 10xgraph.json, broker URL, API extra.
WORKERS: dict[str, dict[str, str]] = {
    "redis": {"label": "Redis", "type": "redis_stream", "url": "${REDIS_URL}", "extra": "jobs"},
    "rabbitmq": {
        "label": "RabbitMQ",
        "type": "rabbitmq",
        "url": "${BROKER_URL}",
        "extra": "jobs-rabbitmq",
    },
    "kafka": {
        "label": "Kafka",
        "type": "kafka",
        "url": "${BROKER_URL}",
        "extra": "jobs-kafka",
    },
}

_MODE_ALIASES = {"quick-start": "quick", "production": "full"}
_MARKER = re.compile(
    r"^[ \t]*(?:(?:#|//)[ \t]*)?##\[(/?)IF_([A-Z0-9_]+)\]##[ \t]*(?:\r?\n|$)", re.MULTILINE
)


def slugify(name: str) -> str:
    """Convert 'WeatherBot' -> 'weather-bot'."""
    s = re.sub(r"([A-Z])", r"-\1", name).lstrip("-").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def check_agent_name(name: str) -> None:
    """Refuse a name with control characters (a newline would add lines to pyproject.toml)."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):  # noqa: PLR2004
        raise ValidationError("Agent name cannot contain control characters.", field="name")


def _choice(value: str | None, default: str, options: Any, field: str, label: str) -> str:
    resolved = (value or default).strip().lower()
    if resolved not in options:
        raise ValidationError(
            f"Invalid {label} '{resolved}'. Choose {', '.join(options)}.", field=field
        )
    return resolved


def _resolve_mode(mode: str | None, template: str | None) -> str:
    """`--mode` wins; `--template quick-start|production` are aliases of quick|full."""
    from_template = None
    if template is not None:
        key = template.strip().lower().replace("_", "-")
        if key not in _MODE_ALIASES:
            raise ValidationError(
                f"Invalid template '{key}'. Choose quick-start or production.", field="template"
            )
        from_template = _MODE_ALIASES[key]
    resolved = _choice(mode, from_template or "quick", MODES, "mode", "mode")
    if from_template and mode and resolved != from_template:
        raise ValidationError("--mode and --template disagree; pass only --mode.", field="mode")
    return resolved


def build_context(  # noqa: PLR0913
    *,
    path: str,
    agent_name: str | None,
    mode: str | None = None,
    template: str | None = None,
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
) -> dict[str, Any]:
    """Resolve a complete, reproducible scaffold recipe without prompting."""
    resolved_mode = _resolve_mode(mode, template)
    inferred_name = Path(path).resolve().name if path not in {"", "."} else "MyAgent"
    resolved_name = (agent_name or inferred_name).strip()
    check_agent_name(resolved_name)
    slug = slugify(resolved_name)
    if not resolved_name or not slug:
        raise ValidationError(
            "Agent name must contain at least one letter or number.", field="name"
        )
    resolved_provider = _choice(provider, DEFAULT_PROVIDER, PROVIDERS, "provider", "AI provider")
    base: dict[str, Any] = {
        "agent_name": resolved_name,
        "agent_name_slug": slug,
        "provider": resolved_provider,
        "websocket": websocket,
        "id_generator": "uuid",
    }

    if resolved_mode == "quick":
        full_only = {
            "--auth": auth not in (None, "none"),
            "--storage": storage,
            "--cache-store": cache_store,
            "--rate-limit": rate_limit,
            "--token-revocation": token_revocation,
            "--evals": evals,
            "--tests": tests,
            "--ag-ui": ag_ui,
            "--otel": otel,
            "--worker": worker not in (None, "none"),
        }
        used = [flag for flag, value in full_only.items() if value not in (None, False)]
        if used:
            raise ValidationError(f"{', '.join(used)} requires --mode full.", field="mode")
        return {
            **base,
            "setup_type": "quick_start",
            "storage": None,
            "auth": "none",
            "cache_store": None,
            "rate_limit": False,
            "token_revocation": False,
            "evals": False,
            "tests": False,
            "ag_ui": False,
            "otel": False,
            "worker": "none",
        }

    resolved_storage = _choice(storage, "memory", STORAGE_PRESETS, "storage", "storage")
    resolved_auth = _choice(auth, "jwt", AUTH_MODES, "auth", "auth mode")
    resolved_worker = _choice(worker, "none", ("none", *WORKERS), "worker", "worker")
    resolved_cache = _choice(
        cache_store,
        "redis" if resolved_worker != "none" else STORAGE_PRESETS[resolved_storage]["cache_store"],
        CACHE_STORES,
        "cache_store",
        "cache store",
    )
    if resolved_worker != "none" and resolved_cache == "memory":
        raise ValidationError(
            "A background worker needs --cache-store redis: task status and cancel live in "
            "the cache store, and a memory store is private to each process.",
            field="cache_store",
        )
    auth_on = resolved_auth != "none"
    return {
        **base,
        "setup_type": "production",
        "storage": resolved_storage,
        "auth": resolved_auth,
        "cache_store": resolved_cache,
        "rate_limit": rate_limit is not False,
        # Ignored with auth off: there is no token to revoke.
        "token_revocation": auth_on and token_revocation is not False,
        "rl_requests": 100,
        "rl_window": 60,
        # With auth on, one budget per user; "ip" would share one budget behind a proxy.
        "rl_by": "user" if auth_on else "ip",
        "rl_trusted_proxy": False,
        "evals": evals is not False,
        "tests": tests is not False,
        "ag_ui": bool(ag_ui),
        "otel": bool(otel),
        "worker": resolved_worker,
    }


def build_config(context: dict, is_prod: bool) -> dict:
    """The 10xgraph.json written next to the scaffold."""
    config: dict = {
        "agent": "graph.agent:app",
        "env": ".env",
        "auth": None,
        "thread_name_generator": None,
        # AG-UI endpoint for clients such as CopilotKit.
        "ag_ui": {"enabled": bool(context.get("ag_ui"))},
        # WebSocket streaming (/v1/graph/ws) and realtime audio (/v1/graph/live).
        # Off: neither route is mounted and no WebSocket library is needed.
        "websocket": {"enabled": bool(context.get("websocket"))},
    }

    if not is_prod:
        return config

    auth = context["auth"]
    if auth == "jwt":
        config["auth"] = {"method": "jwt"}
    elif auth == "custom":
        config["auth"] = {"method": "custom", "path": "auth.agent_auth:AgentAuth"}

    # Frontend tools are declared here (name and schema fixed by the server) instead of
    # being accepted from each request. Add them under "remote_tools".
    config["ag_ui"] = {"enabled": bool(context.get("ag_ui")), "allow_client_tools": False}
    config["remote_tools"] = []

    config["thread_name_generator"] = "first_message"
    config["injectq"] = "graph.agent:container"
    if context.get("storage") == "postgres-memory":
        config["store"] = "graph.agent:store"
    config["id_generator"] = context.get("id_generator", "uuid")

    # The API's cache store: rate limit counters, run locks and caps, idempotency,
    # ownership cache and token revocation. Redis is shared by every worker (empty url:
    # the URL comes from REDIS_URL in .env); memory is per process.
    cache_store: dict[str, Any] = {"type": context.get("cache_store") or "memory"}
    if context.get("rate_limit"):
        cache_store["rate_limit"] = {
            "enabled": True,
            "requests": context.get("rl_requests", 100),
            "window": context.get("rl_window", 60),
            "by": context.get("rl_by", "user" if auth in ("jwt", "custom") else "ip"),
            "trusted_proxy_headers": context.get("rl_trusted_proxy", False),
            "fail_open": False,
            "exclude_paths": ["/ping", "/docs", "/redocs", "/openapi.json"],
        }
    if auth in ("jwt", "custom") and context.get("token_revocation"):
        # A leaked or logged-out token is rejected before it expires. See `10xgraph revoke`.
        cache_store["token_revocation"] = True
    config["cache_store"] = cache_store

    # Who accessed or changed which data, as JSON lines on stdout for the log pipeline.
    config["audit"] = {"enabled": True, "sinks": ["log"], "include_reads": True}

    worker = WORKERS.get(context.get("worker", "none"))
    if worker:
        config["worker"] = {
            "broker": {"type": worker["type"], "url": worker["url"]},
            "tasks": ["worker.tasks"],
        }

    if context.get("tests"):
        config["test"] = {"path": "tests", "coverage": True, "coverage_threshold": 70}
    if context.get("evals"):
        config["evaluation"] = {
            "directory": "evals",
            "output_dir": "eval_reports",
            "threshold": 0.75,
            "parallel": False,
            "max_concurrency": 4,
            "timestamp_files": True,
        }

    return config


def template_flags(context: dict[str, Any]) -> dict[str, bool]:
    """Flags for the ##[IF_NAME]## blocks of the production template."""
    storage = context.get("storage") or ""
    provider = context.get("provider", DEFAULT_PROVIDER)
    postgres = storage.startswith("postgres")
    store = storage == "postgres-memory"
    worker = context.get("worker", "none")
    return {
        "jwt": context.get("auth") == "jwt",
        "custom_auth": context.get("auth") == "custom",
        "redis": postgres or context.get("cache_store") == "redis",
        "postgres": postgres,
        "sqlite": storage == "sqlite",
        "inmem": storage == "memory",
        "store": store,
        "embed_google": store and provider == "google",
        "embed_openai": store and provider in ("openai", "anthropic"),
        "embed_key": store and provider == "anthropic",
        "otel": bool(context.get("otel")),
        "websocket": bool(context.get("websocket")),
        "ag_ui": bool(context.get("ag_ui")),
        "worker": worker != "none",
        "broker_url": worker in ("rabbitmq", "kafka"),
    }


def strip_blocks(content: str, flags: dict[str, bool]) -> str:
    """Resolve ``##[IF_NAME]##`` ... ``##[/IF_NAME]##`` marker lines.

    A marker may follow a ``#`` or ``//`` comment prefix so the template stays valid code.

    A block is kept (marker lines removed) when ``flags[name.lower()]`` is true and dropped
    with its markers otherwise. Blocks may nest. An unknown or unbalanced marker raises, so a
    typo in a template is loud.
    """
    out: list[str] = []
    stack: list[tuple[str, bool]] = []
    pos = 0
    for match in _MARKER.finditer(content):
        if all(keep for _, keep in stack):
            out.append(content[pos : match.start()])
        pos = match.end()
        closing, name = match.group(1) == "/", match.group(2)
        if name.lower() not in flags:
            raise FileOperationError(f"Unknown template block IF_{name}.")
        if not closing:
            stack.append((name, flags[name.lower()]))
        elif not stack or stack[-1][0] != name:
            raise FileOperationError(f"Unexpected end of template block IF_{name}.")
        else:
            stack.pop()
    if stack:
        raise FileOperationError(f"Template block IF_{stack[-1][0]} is never closed.")
    out.append(content[pos:])
    result = "".join(out)
    if "##[IF_" in result or "##[/IF_" in result:
        raise FileOperationError("A template block marker must be alone on its line.")
    return result


def template_vars(context: dict[str, Any]) -> dict[str, Any]:
    """Derive the template variables that depend on the user's answers.

    The pyproject.toml lists only what the answers need; the generated Dockerfile
    installs exactly this list. Quick start has a single core extra: the provider SDK.
    """
    provider = PROVIDERS[context.get("provider", DEFAULT_PROVIDER)]
    flags = template_flags(context)
    core_extras = [provider["extra"]]
    if flags["postgres"]:
        core_extras.append("pg_checkpoint")
    if flags["sqlite"]:
        core_extras.append("sqlite_checkpoint")
    if flags["store"]:
        core_extras.append("pgvector")
    # The generated graph/agent.py imports OpenAIEmbedding for the store.
    if flags["embed_openai"] and "openai" not in core_extras:
        core_extras.append("openai")
    api_extras = [
        extra
        for extra, wanted in (
            ("jwt", flags["jwt"]),
            ("redis", flags["redis"]),
            ("websocket", flags["websocket"]),
            ("ag-ui", flags["ag_ui"]),
            ("otel", flags["otel"]),
        )
        if wanted
    ]
    if flags["worker"]:
        api_extras.append(WORKERS[context["worker"]]["extra"])
    broker_example = (
        "amqp://guest:guest@localhost:5672/"
        if context.get("worker") == "rabbitmq"
        else "localhost:9092"
    )
    return {
        "broker_url_example": broker_example,
        "llm_provider": context.get("provider", DEFAULT_PROVIDER),
        "llm_model": provider["model"],
        "llm_sim_model": provider["sim_model"],
        "llm_api_key_env": provider["api_key_env"],
        "core_requirement": f"10xgraph[{','.join(core_extras)}]",
        "api_requirement": (
            f"10xgraph-api[{','.join(api_extras)}]" if api_extras else "10xgraph-api"
        ),
    }
