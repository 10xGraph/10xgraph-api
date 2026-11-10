"""Interactive questions for `10xgraph init`."""

from collections.abc import Callable
from typing import Any

from tenxgraph_api.cli.core.prompts import Choice, PromptService


_EXTRAS = (
    Choice("evals", "Evals", "eval set and runner", checked=True),
    Choice("tests", "Tests", "pytest suite for the graph", checked=True),
    Choice("websocket", "WebSocket", "streaming and realtime audio"),
    Choice("ag_ui", "AG-UI", "endpoint for CopilotKit and other AG-UI clients"),
    Choice("otel", "OpenTelemetry", "traces and metrics"),
)


def ask_questions(  # noqa: PLR0911
    prompts: PromptService, validate_name: Callable[[str], bool | str], default_provider: str
) -> dict[str, Any] | None:
    """Ask the init questions. Returns the answers as ``build_context`` arguments, or None
    when the user cancels."""
    mode = prompts.select(
        "Which setup?",
        [
            Choice("quick", "Quick", "Minimal graph, no auth: fastest path to a running agent"),
            Choice("full", "Full", "Storage, API security, evals, tests and pre-commit"),
        ],
        default="quick",
    )
    if mode is None:
        return None

    agent_name = prompts.text("What is your agent name?", default="MyAgent", validate=validate_name)
    if agent_name is None:
        return None

    provider = prompts.select(
        "Which AI provider?",
        [
            Choice("google", "Google Gemini", "Installs google-genai · GOOGLE_API_KEY"),
            Choice("openai", "OpenAI", "Installs openai · OPENAI_API_KEY"),
            Choice("anthropic", "Anthropic Claude", "Installs anthropic · ANTHROPIC_API_KEY"),
        ],
        default=default_provider,
    )
    if provider is None:
        return None

    answers: dict[str, Any] = {"mode": mode, "agent_name": agent_name, "provider": provider}
    if mode == "quick":
        return answers

    storage = prompts.select(
        "Where should state be stored?",
        [
            Choice("memory", "Memory", "InMemoryCheckpointer, nothing to run locally"),
            Choice("sqlite", "SQLite", "SqliteCheckpointer, one local file"),
            Choice("postgres", "Postgres + Redis", "PgCheckpointer, Redis cache store"),
            Choice(
                "postgres-memory",
                "Postgres + Redis + memory",
                "PgCheckpointer and PgVectorStore for long-term memory",
            ),
        ],
        default="memory",
    )
    if storage is None:
        return None

    auth = prompts.select(
        "How should the API be secured?",
        [
            Choice("jwt", "JWT", "JWT auth, rate limit and token revocation"),
            Choice("open", "Open", "No auth; rate limit still on, by IP"),
            Choice("custom", "Custom", "Your BaseAuth subclass, rate limit and token revocation"),
        ],
        default="jwt",
    )
    if auth is None:
        return None

    worker = prompts.select(
        "Background tasks?",
        [
            Choice("none", "None", "No worker"),
            Choice("redis", "Redis", "Redis Streams broker, results in Redis"),
            Choice("rabbitmq", "RabbitMQ", "AMQP broker, results in Redis"),
            Choice("kafka", "Kafka", "At-most-once delivery, results in Redis"),
        ],
        default="none",
    )
    if worker is None:
        return None

    extras = prompts.checkbox("Which extras?", _EXTRAS)
    if extras is None:
        return None

    return {
        **answers,
        "storage": storage,
        "auth": "none" if auth == "open" else auth,
        "worker": worker,
        **{choice.value: choice.value in extras for choice in _EXTRAS},
    }
