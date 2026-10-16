"""Default templates for CLI initialization."""

from __future__ import annotations

import json
from typing import Final


# How long a worker gets to finish in-flight work after SIGTERM.
#
# An agent run is not a typical web request: the LLM client alone allows up to
# 600s, and tools add more on top. Gunicorn's default graceful timeout is 30s, so
# every rolling deploy was hard-killing runs that were still in progress, ~30s in.
# Matching the LLM budget means a run in flight gets to finish rather than being
# truncated. Kubernetes needs a slightly larger window than the app, so the pod is
# not killed while the app is still draining.
GRACEFUL_TIMEOUT_SECONDS: Final[int] = 600

# Gunicorn kills a worker it considers hung. Long LLM calls must not look hung.
WORKER_TIMEOUT_SECONDS: Final[int] = 660

# terminationGracePeriodSeconds must exceed the app's graceful timeout, plus the
# preStop sleep that lets the load balancer stop sending new traffic first.
K8S_TERMINATION_GRACE_SECONDS: Final[int] = GRACEFUL_TIMEOUT_SECONDS + 60
K8S_PRESTOP_SLEEP_SECONDS: Final[int] = 15


# Default configuration template
DEFAULT_CONFIG_JSON: Final[str] = json.dumps(
    {
        "agent": "graph.react:app",
        "env": ".env",
        "auth": None,
        "checkpointer": None,
        "injectq": None,
        "store": None,
        "remote_tools": [],
        "thread_name_generator": None,
        "observability": None,
    },
    indent=2,
)

# Template for the default react agent graph
DEFAULT_REACT_PY: Final[str] = '''
"""
Graph-based React Agent Implementation

This module implements a reactive agent system using 10xGraph's StateGraph.
The agent can interact with tools (like weather checking) and maintain conversation
state through a checkpointer. The graph orchestrates the flow between the main
agent logic and tool execution.

Key Components:
- Weather tool: Demonstrates tool calling with dependency injection
- Main agent: AI-powered assistant that can use tools
- Graph flow: Conditional routing based on tool usage
- Checkpointer: Maintains conversation state across interactions

Architecture:
The system uses a state graph with two main nodes:
1. MAIN: Processes user input and generates AI responses
2. TOOL: Executes tool calls when requested by the AI

The graph conditionally routes between these nodes based on whether
the AI response contains tool calls. Conversation history is maintained
through the checkpointer, allowing for multi-turn conversations.

Tools are defined as functions with JSON schema docstrings that describe
their interface for the AI model. The ToolNode automatically extracts
these schemas for tool selection.

Dependencies:
- 10xGraph: For graph and state management
- LiteLLM: For AI model interactions
- InjectQ: For dependency injection
- Python logging: For debug and info messages
"""

import logging
from typing import Any

from tenxgraph.adapters.llm.model_response_converter import ModelResponseConverter
from tenxgraph.checkpointer import InMemoryCheckpointer
from tenxgraph.graph import StateGraph, ToolNode
from tenxgraph.state.agent_state import AgentState
from tenxgraph.utils.callbacks import CallbackManager
from tenxgraph.utils.constants import END
from tenxgraph.utils.converter import convert_messages
from dotenv import load_dotenv
from injectq import Inject
from litellm import acompletion


# Configure logging for the module
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Initialize in-memory checkpointer for maintaining conversation state
checkpointer = InMemoryCheckpointer()


"""
Note: The docstring below will be used as the tool description and it will be
passed to the AI model for tool selection, so keep it relevant and concise.
This function will be converted to a tool with the following schema:
[
        {
            'type': 'function',
            'function': {
                'name': 'get_weather',
                'description': 'Retrieve current weather information for a specified location.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'location': {'type': 'string'}
                    },
                    'required': ['location']
                }
            }
        }
    ]

Parameters like tool_call_id, state, and checkpointer are injected automatically
by InjectQ when the tool is called by the agent.
Available injected parameters:
The following parameters are automatically injected by InjectQ when the tool is called,
but need to keep them as same name and type for proper injection:
- tool_call_id: Unique ID for the tool call
- state: Current AgentState containing conversation context
- config: Configuration dictionary passed during graph invocation

Below fields need to be used with Inject[] to get the instances:
- context_manager: ContextManager instance for managing context, like trimming
- publisher: Publisher instance for publishing events and logs
- checkpointer: InMemoryCheckpointer instance for state management
- store: InMemoryStore instance for temporary data storage
- callback: CallbackManager instance for handling callbacks

"""


def get_weather(
    location: str,
    tool_call_id: str,
    state: AgentState,
    checkpointer: InMemoryCheckpointer = Inject[InMemoryCheckpointer],
) -> str:
    """Retrieve current weather information for a specified location."""
    # Demonstrate access to injected parameters
    logger.debug("***** Checkpointer instance: %s", checkpointer)
    if tool_call_id:
        logger.debug("Tool call ID: %s", tool_call_id)
    if state and hasattr(state, "context"):
        logger.debug("Number of messages in context: %d", len(state.context))

    # Mock weather response - in production, this would call a real weather API
    return f"The weather in {location} is sunny"


# Create a tool node containing all available tools
tool_node = ToolNode([get_weather])


async def main_agent(
    state: AgentState,
    config: dict,
    checkpointer: InMemoryCheckpointer = Inject[InMemoryCheckpointer],
    callback: CallbackManager = Inject[CallbackManager],
) -> Any:
    """
    Main agent logic that processes user messages and generates responses.

    This function implements the core AI agent behavior, handling both regular
    conversation and tool-augmented responses. It uses LiteLLM for AI completion
    and can access conversation history through the checkpointer.

    Args:
        state: Current agent state containing conversation context
        config: Configuration dictionary containing thread_id and other settings
        checkpointer: Checkpointer for retrieving conversation history (injected)
        callback: Callback manager for handling events (injected)

    Returns:
        dict: AI completion response containing the agent's reply

    The agent follows this logic:
    1. If the last message was a tool result, generate a final response without tools
    2. Otherwise, generate a response with available tools for potential tool usage
    """
    # System prompt defining the agent's role and capabilities
    system_prompt = """
        You are a helpful assistant.
        Your task is to assist the user in finding information and answering questions.
        You have access to various tools that can help you provide accurate information.
    """

    # Convert state messages to the format expected by the AI model
    messages = convert_messages(
        system_prompts=[{"role": "system", "content": system_prompt}],
        state=state,
    )

    # Retrieve conversation history from checkpointer
    try:
        thread_messages = await checkpointer.aget_thread({"thread_id": config["thread_id"]})
        logger.debug("Messages from checkpointer: %s", thread_messages)
    except Exception as e:
        logger.warning("Could not retrieve thread messages: %s", e)
        thread_messages = []

    # Log injected dependencies for debugging
    logger.debug("Checkpointer in main_agent: %s", checkpointer)
    logger.debug("CallbackManager in main_agent: %s", callback)

    # Placeholder for MCP (Model Context Protocol) tools
    # These would be additional tools from external sources
    mcp_tools = []
    is_stream = config.get("is_stream", False)

    # Determine response strategy based on conversation context
    if state.context and len(state.context) > 0 and state.context[-1].role == "tool":
        # Last message was a tool result - generate final response without tools
        logger.info("Generating final response after tool execution")
        response = await acompletion(
            model="gemini/gemini-2.0-flash-exp",  # Updated model name
            messages=messages,
            stream=is_stream,
        )
    else:
        # Regular response with tools available for potential usage
        logger.info("Generating response with tools available")
        tools = await tool_node.all_tools()
        response = await acompletion(
            model="gemini/gemini-2.0-flash-exp",  # Updated model name
            messages=messages,
            tools=tools + mcp_tools,
            stream=is_stream,
        )

    return ModelResponseConverter(
        response,
        converter="litellm",
    )


def should_use_tools(state: AgentState) -> str:
    """
    Determine the next step in the graph execution based on the current state.

    This routing function decides whether to continue with tool execution,
    end the conversation, or proceed with the main agent logic.

    Args:
        state: Current agent state containing the conversation context

    Returns:
        str: Next node to execute ("TOOL" or END constant)

    Routing Logic:
    - If last message is from assistant and contains tool calls -> "TOOL"
    - If last message is a tool result -> END (conversation complete)
    - Otherwise -> END (default fallback)
    """
    if not state.context or len(state.context) == 0:
        return END

    last_message = state.context[-1]
    if not last_message:
        return END

    # Check if assistant wants to use tools
    if (
        hasattr(last_message, "tools_calls")
        and last_message.tools_calls
        and len(last_message.tools_calls) > 0
        and last_message.role == "assistant"
    ):
        logger.debug("Routing to TOOL node for tool execution")
        return "TOOL"

    # Check if we just received tool results
    if last_message.role == "tool":
        logger.info("Tool execution complete, ending conversation")
        return END

    # Default case: end conversation
    logger.debug("Default routing: ending conversation")
    return END


# Initialize the state graph for orchestrating agent flow
graph = StateGraph()

# Add nodes to the graph
graph.add_node("MAIN", main_agent)  # Main agent processing node
graph.add_node("TOOL", tool_node)  # Tool execution node

# Define conditional edges from MAIN node
# Routes to TOOL if tools should be used, otherwise ends
graph.add_conditional_edges(
    "MAIN",
    should_use_tools,
    {"TOOL": "TOOL", END: END},
)

# Define edge from TOOL back to MAIN for continued conversation
graph.add_edge("TOOL", "MAIN")

# Set the entry point for graph execution
graph.set_entry_point("MAIN")

# Compile the graph with checkpointer for state management
app = graph.compile(
    checkpointer=checkpointer,
)



'''

# Production templates (mirroring root repo tooling for convenience)
DEFAULT_PRE_COMMIT: Final[str] = """repos:
    - repo: https://github.com/pre-commit/pre-commit-hooks
        rev: v6.0.0
        hooks:
            - id: check-yaml
                exclude: ^(tests|docs|examples)/
            - id: trailing-whitespace
                exclude: ^(tests|docs|examples)/
            - id: check-added-large-files
                args: [--maxkb=100]
                exclude: ^(tests|docs|examples)/
            - id: check-ast
                exclude: ^(tests|docs|examples)/
            - id: check-builtin-literals
                exclude: ^(tests|docs|examples)/
            - id: check-case-conflict
                exclude: ^(tests|docs|examples)/
            - id: check-docstring-first
                exclude: ^(tests|docs|examples)/
            - id: check-merge-conflict
                exclude: ^(tests|docs|examples)/
            - id: debug-statements
                exclude: ^(tests|docs|examples)/
            - id: detect-private-key
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/asottile/pyupgrade
        rev: v3.17.0
        hooks:
            - id: pyupgrade
                args: [--py310-plus]
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/astral-sh/ruff-pre-commit
        rev: v0.5.7
        hooks:
            - id: ruff-format
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/astral-sh/ruff-pre-commit
        rev: v0.5.7
        hooks:
            - id: ruff
                args: [--fix, --exit-non-zero-on-fix]
                exclude: ^(tests|docs|examples)/

    - repo: https://github.com/PyCQA/bandit
        rev: 1.7.9
        hooks:
            - id: bandit
                args: [-c, pyproject.toml]
                additional_dependencies: ["bandit[toml]"]
                exclude: ^(tests|docs|examples)/
"""

DEFAULT_PYPROJECT: Final[str] = """[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "10xgraph-api-app"
version = "0.1.0"
description = "10xGraph API application"
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.10"
authors = [
        {name = "Your Name", email = "you@example.com"},
]
maintainers = [
        {name = "Your Name", email = "you@example.com"},
]
keywords = ["10xgraph", "api", "fastapi", "cli"]
classifiers = [
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
]
dependencies = [
        "10xgraph-api",
]

[project.scripts]
10xgraph = "tenxgraph_api.cli.main:main"

[tool.ruff]
line-length = 100
target-version = "py312"
lint.fixable = ["ALL"]
lint.select = [
    "E", "W", "F", "PL", "I", "B", "A", "S", "ISC", "ICN", "PIE", "Q",
    "RET", "SIM", "TID", "RUF", "YTT", "UP", "C4", "PTH", "G", "INP", "T20",
]
lint.ignore = [
    "UP006", "UP007", "RUF012", "G004", "B904", "B008", "ISC001",
]
lint.dummy-variable-rgx = "^(_+|(_+[a-zA-Z0-9_]*[a-zA-Z0-9]+?))$"
exclude = [
    "venv/*",
]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.per-file-ignores]
"bin/*.py" = ["E402", "S603", "T201", "S101"]
"*/tests/*.py" = ["E402", "S603", "T201", "S101"]
"*/test/*.py" = ["E402", "S603", "T201", "S101"]
"scripts/*.py" = ["E402", "S603", "T201", "S101", "INP001"]
"*/__init__.py" = ["E402", "S603", "T201", "S101"]
"*/migrations/*.py" = ["E402", "S603", "T201", "S101"]

[tool.ruff.lint.isort]
lines-after-imports = 2

[tool.ruff.lint.pylint]
max-args = 10

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
skip-magic-trailing-comma = false
line-ending = "auto"
docstring-code-format = true

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.bandit]
exclude_dirs = ["*/tests/*", "*/tenxgraph_api/tests/*"]
skips = ["B101", "B611", "B601", "B608"]

[tool.pytest.ini_options]
env = ["ENVIRONMENT=pytest"]
testpaths = ["tests"]
pythonpath = ["."]
filterwarnings = ["ignore::DeprecationWarning"]
addopts = [
    "--cov=tenxgraph_api", "--cov-report=html", "--cov-report=term-missing",
    "--cov-report=xml", "--cov-fail-under=0", "--strict-markers", "-v"
]

[tool.coverage.run]
source = ["tenxgraph_api"]
branch = true
omit = [
    "*/__init__.py", "*/tests/*", "*/migrations/*", "*/scripts/*", "*/venv/*", "*/.venv/*",
]

[tool.coverage.report]
exclude_lines = [
    "if __name__ == '__main__':", "pragma: no cover", "@abc.abstractmethod", "@abstractmethod",
    "raise NotImplementedError",
]
show_missing = true

[tool.coverage.paths]
source = ["tenxgraph_api", "*/site-packages/tenxgraph_api"]

[tool.pytest-env]
ENVIRONMENT = "pytest"
"""


# Docker templates
# The published name on PyPI. The bare name "10xgraph-api" belongs to someone else.
CLI_PACKAGE = "10xgraph-api"


# Directories under ``tenxgraph_api/cli`` that only the CLI uses on a developer machine.
# The server imports nothing from them (it needs ``cli/constants.py`` only).
CLI_ONLY_ASSET_DIRS: Final[tuple[str, ...]] = ("templates", "config_editor")

# One-liner run in the image after install. ``find_spec`` locates the package without
# importing it, and a missing package is skipped rather than failing the build. Run with
# ``python -I`` so only the installed copy in site-packages can match, never a checkout on
# the working directory or PYTHONPATH.
_STRIP_CLI_ASSETS = (
    "import importlib.util, pathlib, shutil; "
    "spec = importlib.util.find_spec('tenxgraph_api'); "
    "cli = pathlib.Path(spec.origin).parent / 'cli' if spec and spec.origin else None; "
    f"[shutil.rmtree(cli / d, ignore_errors=True) for d in {CLI_ONLY_ASSET_DIRS!r} if cli]"
)


def generate_dockerfile_content(
    python_version: str,
    port: int,
    requirements_file: str,
    has_requirements: bool,
    omit_cmd: bool = False,
    has_pyproject: bool = False,
) -> str:
    """Generate the content for the Dockerfile.

    Dependencies come from ``requirements_file`` when there is one, otherwise from the
    ``[project].dependencies`` of ``pyproject.toml`` (what ``10xgraph init`` scaffolds), and
    only as a last resort from the published CLI package alone.

    The image is built in two stages. Dependencies are installed into a virtualenv in a
    throwaway builder stage, and only that virtualenv reaches the final image, so pip,
    compilers and install leftovers never ship.
    """
    dockerfile_lines = [
        "# Dockerfile for 10xGraph API",
        "# Generated by 10xgraph-api CLI",
        "",
        "# ---- Stage 1: install dependencies into a virtualenv ----",
        f"FROM python:{python_version}-slim AS builder",
        "",
        "ENV PIP_NO_CACHE_DIR=1",
        "ENV PIP_DISABLE_PIP_VERSION_CHECK=1",
        "WORKDIR /build",
        "RUN python -m venv /opt/venv",
        'ENV PATH="/opt/venv/bin:$PATH"',
        "",
        "# Every 10xGraph dependency ships prebuilt wheels, so no compiler is needed. If one",
        "# of your own dependencies has to build from source, uncomment this. It only lives",
        "# in the builder stage and never reaches the final image.",
        "# RUN apt-get update \\",
        "#     && apt-get install -y --no-install-recommends build-essential \\",
        "#     && rm -rf /var/lib/apt/lists/*",
        "",
    ]

    if has_requirements:
        dockerfile_lines.extend(
            [
                "# Install exactly the dependencies listed in your requirements file",
                f"COPY {requirements_file} requirements.txt",
                "RUN pip install -r requirements.txt",
                "",
            ]
        )
    elif has_pyproject:
        dockerfile_lines.extend(
            [
                "# Install exactly the [project].dependencies listed in pyproject.toml.",
                "# pip cannot read that list on its own without installing the project as a",
                "# package, so it is printed to a temporary file inside this builder stage.",
                "# The file is never in your project folder and never reaches the final image.",
                "COPY pyproject.toml .",
                "RUN python -c \"import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))"
                "['project'].get('dependencies', []); print('\\\\n'.join(deps))\" "
                "> /tmp/requirements.txt \\",
                "    && pip install -r /tmp/requirements.txt",
                "",
            ]
        )
    else:
        dockerfile_lines.extend(
            [
                "# No requirements.txt or pyproject.toml: install the 10xGraph API server only",
                "# (it brings uvicorn with it)",
                f"RUN pip install {CLI_PACKAGE}",
                "",
            ]
        )

    dockerfile_lines.extend(
        [
            "# Production process manager: gunicorn supervising uvicorn workers. These are",
            "# not dependencies of 10xgraph-api, so the image installs them here.",
            'RUN pip install "gunicorn>=23,<27" "uvicorn-worker>=0.4,<1"',
            "",
            "# Strip CLI-only assets the server never loads: project scaffolds, the bundled",
            "# agent skill, and the local config editor (HTML/JS). Only the server code and",
            "# its dependencies stay in the image.",
            f'RUN python -I -c "{_STRIP_CLI_ASSETS}"',
            "",
            "# ---- Stage 2: runtime image ----",
            f"FROM python:{python_version}-slim",
            "",
            "# Set environment variables",
            "ENV PYTHONDONTWRITEBYTECODE=1",
            "ENV PYTHONUNBUFFERED=1",
            "ENV PYTHONPATH=/app",
            'ENV PATH="/opt/venv/bin:$PATH"',
            "# Default to production; overridable via .env or runtime env.",
            "# In production the local in-memory telemetry store stays disabled",
            "# (use OTEL / a publisher for observability instead).",
            "ENV MODE=production",
            "ENV IS_DEBUG=false",
            "# Gunicorn worker count. Gunicorn reads WEB_CONCURRENCY natively; a sane default",
            "# here beats gunicorn's built-in default of 1 worker. Tune to your CPU/memory",
            "# at deploy time, e.g. `docker run -e WEB_CONCURRENCY=8 ...`.",
            "ENV WEB_CONCURRENCY=2",
            "",
            "# Create a non-root user that owns the work directory",
            "RUN groupadd -r appuser && useradd -r -g appuser appuser \\",
            "    && mkdir -p /app && chown appuser:appuser /app",
            "WORKDIR /app",
            "",
            "# Only the installed virtualenv comes over from the builder",
            "COPY --from=builder /opt/venv /opt/venv",
            "",
            "# Copy application code. --chown sets ownership while copying; a separate",
            "# `chown -R` afterwards would write a second copy of every file into a new layer.",
            "COPY --chown=appuser:appuser . .",
            "USER appuser",
            "",
            "# Expose port",
            f"EXPOSE {port}",
            "",
            "# Health check (Python stdlib, so the image needs no curl)",
            "HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \\",
            '    CMD python -c "import urllib.request; '
            f"urllib.request.urlopen('http://127.0.0.1:{port}/ping', timeout=5)\" || exit 1",
            "",
        ]
    )

    if not omit_cmd:
        dockerfile_lines.extend(
            [
                "# Run the application (production)",
                "# Use Gunicorn with Uvicorn workers for better performance and multi-core",
                "# utilization",
                "#",
                "# --graceful-timeout is critical: an agent run can legitimately take",
                "# minutes (the LLM client alone allows up to 600s). Gunicorn's default",
                "# graceful timeout is 30s, so on every rolling deploy SIGTERM would",
                "# hard-kill runs that were still mid-flight, truncating them. Give",
                "# in-flight runs time to finish draining instead.",
                (
                    'CMD ["gunicorn", "-k", "uvicorn_worker.UvicornWorker", '
                    f'"-b", "0.0.0.0:{port}", '
                    f'"--graceful-timeout", "{GRACEFUL_TIMEOUT_SECONDS}", '
                    f'"--timeout", "{WORKER_TIMEOUT_SECONDS}", '
                    '"tenxgraph_api.src.app.main:app"]'
                ),
                "",
            ]
        )

    return "\n".join(dockerfile_lines)


def generate_k8s_manifest_content(service_name: str, port: int) -> str:
    """Generate a Kubernetes Deployment + Service manifest.

    Exists because rolling deploys were the main way in-flight agent runs got
    truncated, and no manifest was generated at all -- so every user hand-rolled
    one, typically with the default 30s grace period that kills a run mid-LLM-call.

    The three settings that matter here:

    - ``terminationGracePeriodSeconds`` must exceed the app's own graceful timeout,
      or the kubelet SIGKILLs the pod while it is still draining.
    - The ``preStop`` sleep gives the load balancer time to notice the pod is
      terminating and stop routing NEW requests to it. Without it, Kubernetes sends
      SIGTERM and removes the endpoint concurrently, so requests can still arrive
      at a pod that has already begun shutting down.
    - The readiness probe is what takes the pod out of rotation; the liveness probe
      must be slack enough that a busy worker is not restarted mid-run.
    """
    return "\n".join(
        [
            "apiVersion: apps/v1",
            "kind: Deployment",
            "metadata:",
            f"  name: {service_name}",
            "spec:",
            "  replicas: 2",
            "  selector:",
            "    matchLabels:",
            f"      app: {service_name}",
            "  template:",
            "    metadata:",
            "      labels:",
            f"        app: {service_name}",
            "    spec:",
            # Must be > the app's graceful timeout, else the kubelet SIGKILLs a
            # pod that is still finishing a run.
            f"      terminationGracePeriodSeconds: {K8S_TERMINATION_GRACE_SECONDS}",
            "      containers:",
            f"        - name: {service_name}",
            "          image: 10xgraph-api:latest",
            "          ports:",
            f"            - containerPort: {port}",
            "          env:",
            "            - name: MODE",
            '              value: "production"',
            "            - name: IS_DEBUG",
            '              value: "false"',
            "            # CORS: production refuses wildcard origins with credentials.",
            "            - name: ORIGINS",
            '              value: "https://your-frontend.example.com"',
            "          lifecycle:",
            "            preStop:",
            "              exec:",
            "                # Let the load balancer stop sending new traffic before",
            "                # the app starts shutting down.",
            "                command:",
            '                  - "/bin/sh"',
            '                  - "-c"',
            f'                  - "sleep {K8S_PRESTOP_SLEEP_SECONDS}"',
            "          readinessProbe:",
            "            httpGet:",
            "              path: /health/ready",
            f"              port: {port}",
            "            initialDelaySeconds: 5",
            "            periodSeconds: 10",
            "          livenessProbe:",
            "            httpGet:",
            "              path: /ping",
            f"              port: {port}",
            "            # Deliberately slack: a worker busy with a long agent run",
            "            # must not be mistaken for a hung one and restarted.",
            "            initialDelaySeconds: 30",
            "            periodSeconds: 30",
            "            failureThreshold: 5",
            "          resources:",
            "            requests:",
            '              cpu: "500m"',
            '              memory: "512Mi"',
            "            limits:",
            '              cpu: "2"',
            '              memory: "2Gi"',
            "---",
            "apiVersion: v1",
            "kind: Service",
            "metadata:",
            f"  name: {service_name}",
            "spec:",
            "  selector:",
            f"    app: {service_name}",
            "  ports:",
            "    - protocol: TCP",
            f"      port: 80",
            f"      targetPort: {port}",
            "",
        ]
    )


def generate_docker_compose_content(service_name: str, port: int) -> str:
    """Generate a simple docker-compose.yml content for the API service."""
    return "\n".join(
        [
            "services:",
            f"  {service_name}:",
            "    build: .",
            "    image: 10xgraph-api:latest",
            "    environment:",
            "      - PYTHONUNBUFFERED=1",
            "      - PYTHONDONTWRITEBYTECODE=1",
            # Default to production; local in-memory telemetry stays disabled.
            "      - MODE=production",
            "      - IS_DEBUG=false",
            "    ports:",
            f"      - '{port}:{port}'",
            (
                f"    command: [ 'gunicorn', '-k', 'uvicorn_worker.UvicornWorker', "
                f"'-b', '0.0.0.0:{port}', "
                f"'--graceful-timeout', '{GRACEFUL_TIMEOUT_SECONDS}', "
                f"'--timeout', '{WORKER_TIMEOUT_SECONDS}', "
                "'tenxgraph_api.src.app.main:app' ]"
            ),
            # Give in-flight agent runs time to drain on `docker compose down`
            # instead of being killed after the default 10s.
            f"    stop_grace_period: {GRACEFUL_TIMEOUT_SECONDS}s",
            "    restart: unless-stopped",
            "    # Consider adding resource limits and deploy configurations in a swarm/stack",
            "    # deploy:",
            "    #   replicas: 2",
            "    #   resources:",
            "    #     limits:",
            "    #       cpus: '1.0'",
            "    #       memory: 512M",
        ]
    )


def generate_dockerignore_content() -> str:
    """Generate a standard .dockerignore file content."""
    return "\n".join(
        [
            "# Exclude environment secrets and local configs",
            ".env",
            ".env.*",
            "secrets/",
            "configs/",
            "*.env",
            "",
            "# Exclude Python artifacts and caches",
            "__pycache__/",
            "*.py[cod]",
            "*$py.class",
            ".pytest_cache/",
            ".coverage",
            "htmlcov/",
            ".mypy_cache/",
            ".ruff_cache/",
            "",
            "# Exclude virtual environments",
            ".venv/",
            "venv/",
            "env/",
            "",
            "# Exclude version control and IDE files",
            ".git/",
            ".gitignore",
            ".idea/",
            ".vscode/",
            "*.swp",
            "*.swo",
            "",
            "# Exclude built assets and distribution files",
            "build/",
            "dist/",
            "*.egg-info/",
            "",
            "# Exclude local data directories",
            "data/",
            "db.sqlite3",
            "store/",
            "uploads/",
            "eval_reports/",
            "",
            "# Exclude dev-only project folders: tests, evals, and installed agent skills",
            "tests/",
            "evals/",
            ".claude/",
            ".agents/",
            ".github/",
            "",
        ]
    )
